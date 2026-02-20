"""WebSocket endpoints for real-time chat."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from jose import JWTError, jwt
from loguru import logger

from app.core.config import settings
from app.core.websocket import connection_manager
from app.db.models.conversation import ConversationSession, MessageRole
from app.db.models.tenant import Tenant
from app.db.models.user import UserRole
from app.db.session import SessionLocal
from app.services.conversation_service import ConversationService
from app.utils.datetime import ensure_utc_aware

router = APIRouter()


def _is_subscription_inactive(tenant: Tenant | None) -> bool:
    """Return True when tenant subscription should block chat/support activity."""
    if not tenant:
        return True

    if tenant.subscription_status in {"trialing", "trial"}:
        now = datetime.now(UTC)
        trial_ends_at = ensure_utc_aware(tenant.trial_ends_at)
        if trial_ends_at and now > trial_ends_at:
            return True

    return tenant.subscription_status in {"trial_expired", "canceled", "past_due"}


@router.websocket("/chat/{session_id}")
async def customer_websocket(
    websocket: WebSocket,
    session_id: UUID,
    user_identifier: str = Query(..., description="Customer identifier"),
):
    """
    WebSocket endpoint for customer chat.

    Customers connect here to receive:
    - Agent messages in real-time
    - Agent joined/left notifications
    - Typing indicators from agents
    - Session status updates

    Query params:
        user_identifier: Customer's unique identifier (email, session token, etc.)
    """
    # Verify session exists and user_identifier matches (short-lived DB connection)
    db = SessionLocal()
    try:
        conversation_service = ConversationService(db)
        session = conversation_service.get_session(session_id)

        if not session:
            await websocket.close(code=4004, reason="Session not found")
            return

        # Verify user_identifier matches session
        if session.user_identifier != user_identifier:
            await websocket.close(code=4003, reason="Unauthorized")
            return

        tenant = db.query(Tenant).filter(Tenant.id == session.tenant_id).first()
        if _is_subscription_inactive(tenant):
            await websocket.close(code=4003, reason="Subscription inactive")
            return
    finally:
        db.close()  # Release connection immediately after verification

    # Connect customer
    await connection_manager.connect_customer(
        websocket=websocket,
        session_id=session_id,
        user_identifier=user_identifier,
    )

    # Replay recent human-agent messages so customers don't miss replies
    # sent while their widget was closed or reconnecting.
    replay_db = SessionLocal()
    try:
        replay_service = ConversationService(replay_db)
        recent_messages = replay_service.get_session_messages(session_id, limit=50)
        for msg in recent_messages:
            if msg.role != MessageRole.HUMAN_AGENT:
                continue
            meta = msg.meta or {}
            await websocket.send_json(
                {
                    "type": "agent_message",
                    "message_id": str(msg.id),
                    "content": msg.content,
                    "agent_name": meta.get("agent_name"),
                    "agent_id": meta.get("agent_id"),
                    "timestamp": msg.created_at.isoformat(),
                }
            )
    except Exception as replay_error:
        logger.warning(f"Failed to replay recent agent messages: {replay_error}")
    finally:
        replay_db.close()

    try:
        while True:
            # Receive messages from customer
            data = await websocket.receive_json()
            message_type = data.get("type")

            if message_type == "message":
                # Customer sent a message - save to DB and broadcast to agents
                content = data.get("content", "").strip()
                if not content:
                    continue

                # Get DB connection only for saving message
                db = SessionLocal()
                try:
                    active_session = (
                        db.query(ConversationSession)
                        .filter(ConversationSession.id == session_id)
                        .first()
                    )
                    tenant = (
                        db.query(Tenant).filter(Tenant.id == active_session.tenant_id).first()
                        if active_session
                        else None
                    )
                    if _is_subscription_inactive(tenant):
                        await websocket.close(code=4003, reason="Subscription inactive")
                        return

                    conversation_service = ConversationService(db)
                    message = conversation_service.add_message(
                        session_id=session_id,
                        role=MessageRole.USER,
                        content=content,
                    )
                    db.commit()  # Ensure message is committed

                    # Store message data before closing DB connection
                    message_id = str(message.id)
                    message_timestamp = message.created_at.isoformat()
                finally:
                    db.close()  # Release connection immediately

                # Broadcast to all agents watching this session
                await connection_manager.send_to_agents(
                    session_id=session_id,
                    message={
                        "type": "customer_message",
                        "message_id": message_id,
                        "content": content,
                        "user_identifier": user_identifier,
                        "timestamp": message_timestamp,
                    },
                )

                logger.info(f"Customer message in session {session_id}: {content[:50]}")

            elif message_type == "typing":
                # Customer is typing - notify agents (no DB needed)
                is_typing = data.get("is_typing", False)
                await connection_manager.send_typing_indicator(
                    session_id=session_id,
                    is_typing=is_typing,
                    user_name="Customer",
                    to_customer=False,  # Send to agents
                )
            elif message_type == "ping":
                await websocket.send_json(
                    {"type": "pong", "timestamp": datetime.now(UTC).isoformat()}
                )

    except WebSocketDisconnect:
        logger.info(f"Customer disconnected from session {session_id}")
        connection_manager.disconnect(websocket)

    except Exception as e:
        logger.error(f"Error in customer WebSocket: {e}")
        connection_manager.disconnect(websocket)


@router.websocket("/agent/{session_id}")
async def agent_websocket(
    websocket: WebSocket,
    session_id: UUID,
    token: str = Query(..., description="JWT authentication token"),
):
    """
    WebSocket endpoint for support agents.

    Agents connect here to:
    - Monitor customer conversations in real-time
    - Send messages to customers
    - Receive typing indicators from customers
    - Get notified of session updates

    Query params:
        token: JWT authentication token for agent
    """
    # Authenticate agent (short-lived DB connection)
    agent_id = None
    tenant_id = None
    agent_full_name = None

    try:
        # Decode JWT token
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        agent_id = UUID(payload.get("sub"))  # User ID is stored in 'sub' claim
        tenant_id = UUID(payload.get("tenant_id"))

        # Get agent user from database (short-lived connection)
        db = SessionLocal()
        try:
            from app.db.models.user import User

            agent = db.query(User).filter(User.id == agent_id).first()

            if not agent or not agent.is_active:
                await websocket.close(code=4003, reason="Unauthorized")
                return

            # Verify agent has appropriate role
            if agent.role not in [UserRole.TENANT_ADMIN, UserRole.SUPPORT_AGENT]:
                await websocket.close(code=4003, reason="Insufficient permissions")
                return

            # Store agent info for use in the WebSocket loop
            agent_full_name = agent.full_name
        finally:
            db.close()  # Release connection immediately after authentication

    except JWTError as e:
        logger.error(f"JWT verification failed: {e}")
        await websocket.close(code=4003, reason="Invalid token")
        return
    except Exception as e:
        logger.error(f"Agent authentication failed: {e}")
        await websocket.close(code=4003, reason="Authentication failed")
        return

    # Verify session exists and belongs to agent's tenant (short-lived DB connection)
    db = SessionLocal()
    try:
        conversation_service = ConversationService(db)
        session = conversation_service.get_session(session_id)

        if not session:
            await websocket.close(code=4004, reason="Session not found")
            return

        if session.tenant_id != tenant_id:
            await websocket.close(code=4003, reason="Access denied")
            return

        tenant = db.query(Tenant).filter(Tenant.id == session.tenant_id).first()
        if _is_subscription_inactive(tenant):
            await websocket.close(code=4003, reason="Subscription inactive")
            return
    finally:
        db.close()  # Release connection immediately after verification

    # Connect agent
    await connection_manager.connect_agent(
        websocket=websocket,
        session_id=session_id,
        agent_id=agent_id,
        agent_name=agent_full_name,
    )

    try:
        while True:
            # Receive messages from agent
            data = await websocket.receive_json()
            message_type = data.get("type")

            if message_type == "message":
                # Agent sent a message - save to DB and send to customer
                content = data.get("content", "").strip()
                if not content:
                    continue

                # Get DB connection only for saving message
                db = SessionLocal()
                try:
                    active_session = (
                        db.query(ConversationSession)
                        .filter(ConversationSession.id == session_id)
                        .first()
                    )
                    tenant = (
                        db.query(Tenant).filter(Tenant.id == active_session.tenant_id).first()
                        if active_session
                        else None
                    )
                    if _is_subscription_inactive(tenant):
                        await websocket.close(code=4003, reason="Subscription inactive")
                        return

                    conversation_service = ConversationService(db)
                    message = conversation_service.add_message(
                        session_id=session_id,
                        # DB enum currently stores this role as lower-case literal.
                        role=MessageRole.HUMAN_AGENT.value,  # type: ignore[arg-type]
                        content=content,
                        meta={
                            "agent_id": str(agent_id),
                            "agent_name": agent_full_name,
                        },
                    )
                    db.commit()  # Ensure message is committed

                    # Store message data before closing DB connection
                    message_id = str(message.id)
                    message_timestamp = message.created_at.isoformat()
                finally:
                    db.close()  # Release connection immediately

                # Send to customer
                await connection_manager.send_to_customer(
                    session_id=session_id,
                    message={
                        "type": "agent_message",
                        "message_id": message_id,
                        "content": content,
                        "agent_name": agent_full_name,
                        "timestamp": message_timestamp,
                    },
                )

                # Notify other agents watching this session
                await connection_manager.send_to_agents(
                    session_id=session_id,
                    message={
                        "type": "agent_message",
                        "message_id": message_id,
                        "content": content,
                        "agent_name": agent_full_name,
                        "timestamp": message_timestamp,
                    },
                    exclude_agent_id=agent_id,
                )

                logger.info(f"Agent {agent_full_name} sent message in session {session_id}")

            elif message_type == "typing":
                # Agent is typing - notify customer (no DB needed)
                is_typing = data.get("is_typing", False)
                await connection_manager.send_typing_indicator(
                    session_id=session_id,
                    is_typing=is_typing,
                    user_name=agent_full_name,
                    to_customer=True,  # Send to customer
                )
            elif message_type == "ping":
                await websocket.send_json(
                    {"type": "pong", "timestamp": datetime.now(UTC).isoformat()}
                )

    except WebSocketDisconnect:
        logger.info(f"Agent {agent_full_name} disconnected from session {session_id}")
        connection_manager.disconnect(websocket)

        # Notify customer that agent left
        await connection_manager.send_to_customer(
            session_id=session_id,
            message={
                "type": "agent_left",
                "agent_name": agent_full_name,
                "timestamp": None,
            },
        )

    except Exception as e:
        logger.error(f"Error in agent WebSocket: {e}")
        connection_manager.disconnect(websocket)

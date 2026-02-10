"""WebSocket endpoints for real-time chat."""

from uuid import UUID

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from jose import JWTError, jwt
from loguru import logger

from app.core.config import settings
from app.core.websocket import connection_manager
from app.db.models.conversation import MessageRole
from app.db.models.user import UserRole
from app.db.session import SessionLocal
from app.services.conversation_service import ConversationService

router = APIRouter()


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
    finally:
        db.close()  # Release connection immediately after verification

    # Connect customer
    await connection_manager.connect_customer(
        websocket=websocket,
        session_id=session_id,
        user_identifier=user_identifier,
    )

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

"""WebSocket endpoints for real-time chat."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from jose import JWTError, jwt
from loguru import logger

from app.api.deps import extract_customer_token_from_cookies, is_token_revoked
from app.core.config import settings
from app.core.websocket import connection_manager
from app.db.models.conversation import ConversationSession, MessageRole
from app.db.models.customer_user import CustomerUser
from app.db.models.tenant import Tenant
from app.db.models.user import UserRole
from app.db.session import SessionLocal
from app.services.conversation_service import ConversationService, attachment_frames
from app.services.customer_presence import persist_last_seen_factory, presence_tracker
from app.utils.datetime import ensure_utc_aware
from app.utils.staff_redaction import staff_customer_label

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


def _authenticate_customer_socket_token(token: str) -> CustomerUser | None:
    """Validate customer JWT and return active customer model."""
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        if payload.get("auth_type") != "customer":
            return None

        jti = payload.get("jti")
        if jti and is_token_revoked(jti):
            return None
        customer_id = payload.get("sub")
        if not customer_id:
            return None

        db = SessionLocal()
        try:
            customer = db.query(CustomerUser).filter(CustomerUser.id == UUID(customer_id)).first()
            if not customer or not customer.is_active:
                return None
            db.expunge(customer)
            return customer
        finally:
            db.close()
    except (JWTError, ValueError):
        return None


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
        agent_facing_label = staff_customer_label(db, session_id, session.visitor_id)
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
                    "attachments": attachment_frames(msg.attachments),
                }
            )
    except Exception as replay_error:
        logger.warning(f"Failed to replay recent agent messages: {replay_error}")
    finally:
        replay_db.close()

    presence = presence_tracker.open(session_id, persist_last_seen_factory(SessionLocal))
    await presence.start()
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

                # Broadcast to agents (local + cross-worker)
                await connection_manager.fanout_event(
                    session_id=session_id,
                    message={
                        "type": "customer_message",
                        "message_id": message_id,
                        "content": content,
                        "customer_label": agent_facing_label,
                        "timestamp": message_timestamp,
                        "attachments": [],
                    },
                    to_customer=False,
                    to_agents=True,
                )

                logger.info("Received customer message for agent fanout")

            elif message_type == "typing":
                # Customer is typing - notify agents (no DB needed)
                is_typing = data.get("is_typing", False)
                await connection_manager.send_typing_indicator(
                    session_id=session_id,
                    is_typing=is_typing,
                    user_name="Customer",
                    to_customer=False,  # Send to agents
                )
            elif message_type == "presence":
                await presence.handle_frame(data)
            elif message_type == "ping":
                await websocket.send_json(
                    {"type": "pong", "timestamp": datetime.now(UTC).isoformat()}
                )

    except WebSocketDisconnect:
        logger.info("Customer disconnected from session")
        connection_manager.disconnect(websocket)

    except Exception:
        logger.error("Error in customer WebSocket")
        connection_manager.disconnect(websocket)

    finally:
        await presence.close()


@router.websocket("/customer/{session_id}")
async def authenticated_customer_websocket(
    websocket: WebSocket,
    session_id: UUID,
    token: str | None = Query(None, description="Customer JWT (non-browser clients)"),
):
    """WebSocket endpoint for authenticated customer chat.

    The web app authenticates with its HttpOnly ``customer_access_token`` cookie, which the
    browser attaches to the handshake itself. Because that is an ambient credential and
    WebSocket handshakes bypass CORS, the cookie path is Origin-gated against
    ``BACKEND_CORS_ORIGINS`` (Cross-Site WebSocket Hijacking). The ``?token=`` bearer path is
    kept for non-browser clients and is not Origin-gated, matching the agent socket.
    """
    if token:
        effective_token = token
    else:
        origin = websocket.headers.get("origin")
        if not origin or origin not in settings.BACKEND_CORS_ORIGINS:
            await websocket.close(code=4403, reason="Origin not allowed")
            return
        effective_token = extract_customer_token_from_cookies(websocket.cookies)
    if not effective_token:
        await websocket.close(code=4003, reason="Missing authentication")
        return

    customer = _authenticate_customer_socket_token(effective_token)
    if not customer:
        await websocket.close(code=4003, reason="Invalid token")
        return

    db = SessionLocal()
    try:
        conversation_service = ConversationService(db)
        session = conversation_service.get_session(session_id)

        if not session:
            await websocket.close(code=4004, reason="Session not found")
            return

        customer_matches = session.customer_user_id == customer.id
        legacy_matches = session.user_identifier == customer.chat_user_key
        if not customer_matches and not legacy_matches:
            await websocket.close(code=4003, reason="Unauthorized")
            return

        tenant = db.query(Tenant).filter(Tenant.id == session.tenant_id).first()
        if _is_subscription_inactive(tenant):
            await websocket.close(code=4003, reason="Subscription inactive")
            return
        agent_facing_label = staff_customer_label(db, session_id, session.visitor_id)
    finally:
        db.close()

    await connection_manager.connect_customer(
        websocket=websocket,
        session_id=session_id,
        user_identifier=customer.chat_user_key,
    )

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
                    "attachments": attachment_frames(msg.attachments),
                }
            )
    except Exception as replay_error:
        logger.warning(f"Failed to replay recent authenticated customer messages: {replay_error}")
    finally:
        replay_db.close()

    presence = presence_tracker.open(session_id, persist_last_seen_factory(SessionLocal))
    await presence.start()
    try:
        while True:
            data = await websocket.receive_json()
            message_type = data.get("type")

            if message_type == "message":
                content = data.get("content", "").strip()
                if not content:
                    continue

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
                    db.commit()
                    message_id = str(message.id)
                    message_timestamp = message.created_at.isoformat()
                finally:
                    db.close()

                await connection_manager.fanout_event(
                    session_id=session_id,
                    message={
                        "type": "customer_message",
                        "message_id": message_id,
                        "content": content,
                        "customer_label": agent_facing_label,
                        "timestamp": message_timestamp,
                        "attachments": [],
                    },
                    to_customer=False,
                    to_agents=True,
                )

            elif message_type == "typing":
                is_typing = data.get("is_typing", False)
                await connection_manager.send_typing_indicator(
                    session_id=session_id,
                    is_typing=is_typing,
                    user_name=customer.full_name or "Customer",
                    to_customer=False,
                )
            elif message_type == "presence":
                await presence.handle_frame(data)
            elif message_type == "ping":
                await websocket.send_json(
                    {"type": "pong", "timestamp": datetime.now(UTC).isoformat()}
                )
    except WebSocketDisconnect:
        logger.info("Authenticated customer disconnected from session")
        connection_manager.disconnect(websocket)
    except Exception:
        logger.error("Error in authenticated customer WebSocket")
        connection_manager.disconnect(websocket)

    finally:
        await presence.close()


@router.websocket("/agent/{session_id}")
async def agent_websocket(
    websocket: WebSocket,
    session_id: UUID,
    token: str | None = Query(None, description="JWT authentication token"),
):
    """
    WebSocket endpoint for support agents.

    Agents connect here to:
    - Monitor customer conversations in real-time
    - Send messages to customers
    - Receive typing indicators from customers
    - Get notified of session updates

    Authentication accepts the agent JWT from either source, mirroring REST
    auth (get_current_user -> extract_token): the Flutter app supplies it as the
    ?token= query param, while the web dashboard opens the socket with no query
    token and relies on the browser sending the httponly ``access_token`` cookie
    automatically. Both carry the same JWT and are validated identically below.
    The cookie path is additionally Origin-gated (against ``BACKEND_CORS_ORIGINS``)
    to prevent Cross-Site WebSocket Hijacking; the query-token path is not, since
    the token is a secret and the native app sends no Origin header.

    Query params:
        token: JWT authentication token for agent (optional; the ``access_token``
            cookie is used as a fallback source of the same token)
    """
    # Authenticate agent (short-lived DB connection)
    agent_id = None
    tenant_id = None
    agent_full_name = None

    # Resolve the effective token from either source. Both carry the same JWT,
    # but they have different trust properties, so they are gated differently:
    #   * ?token= is a bearer SECRET a cross-origin page cannot read, and the
    #     Flutter app sends no Origin header -> accept as-is (CSWSH-safe).
    #   * the access_token cookie is an AMBIENT credential the browser attaches
    #     automatically, and WebSocket handshakes are exempt from CORS. Without
    #     an Origin check a malicious cross-origin page could open this socket
    #     and ride the victim agent's cookie (Cross-Site WebSocket Hijacking).
    #     So the cookie path additionally requires a trusted Origin, matched
    #     against the same allowlist REST CORS already enforces.
    origin = websocket.headers.get("origin")
    if token:
        effective_token = token
    else:
        if not origin or origin not in settings.BACKEND_CORS_ORIGINS:
            await websocket.close(code=4403, reason="Origin not allowed")
            return
        effective_token = websocket.cookies.get("access_token")

    if not effective_token:
        await websocket.close(code=4003, reason="Missing authentication")
        return

    try:
        # Decode JWT token
        payload = jwt.decode(effective_token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
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
        customer_display_label = staff_customer_label(db, session_id, session.visitor_id)
    finally:
        db.close()  # Release connection immediately after verification

    # Connect agent
    await connection_manager.connect_agent(
        websocket=websocket,
        session_id=session_id,
        agent_id=agent_id,
        agent_name=agent_full_name,
    )

    # Replay recent chat messages so agents don't miss updates sent while their
    # dashboard socket was reconnecting.
    replay_db = SessionLocal()
    try:
        replay_service = ConversationService(replay_db)
        recent_messages = replay_service.get_session_messages(session_id, limit=50)
        for msg in recent_messages:
            if msg.role == MessageRole.USER:
                await websocket.send_json(
                    {
                        "type": "customer_message",
                        "message_id": str(msg.id),
                        "content": msg.content,
                        "customer_label": customer_display_label,
                        "timestamp": msg.created_at.isoformat(),
                        "attachments": attachment_frames(msg.attachments),
                    }
                )
            elif msg.role == MessageRole.HUMAN_AGENT:
                meta = msg.meta or {}
                await websocket.send_json(
                    {
                        "type": "agent_message",
                        "message_id": str(msg.id),
                        "content": msg.content,
                        "agent_name": meta.get("agent_name"),
                        "agent_id": meta.get("agent_id"),
                        "timestamp": msg.created_at.isoformat(),
                        "attachments": attachment_frames(msg.attachments),
                    }
                )
    except Exception as replay_error:
        logger.warning(f"Failed to replay recent session messages to agent: {replay_error}")
    finally:
        replay_db.close()

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
                        # message_role stores enum NAMES — pass the member (the old
                        # `.value` was inert; see migration d1f2humanagentname).
                        role=MessageRole.HUMAN_AGENT,
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

                # Fanout to customer + other agents (local + cross-worker)
                await connection_manager.fanout_event(
                    session_id=session_id,
                    message={
                        "type": "agent_message",
                        "message_id": message_id,
                        "content": content,
                        "agent_name": agent_full_name,
                        "timestamp": message_timestamp,
                        # Text-only over this socket; always present so id-merging
                        # clients never see a frame that drops images.
                        "attachments": [],
                    },
                    to_customer=True,
                    to_agents=True,
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

        # Notify customer only when no agent remains connected to this session.
        session_info = connection_manager.get_session_info(session_id)
        if session_info.get("agents_connected", 0) == 0:
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

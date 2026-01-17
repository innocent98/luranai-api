"""WebSocket endpoints for real-time chat."""

import json
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends, Query, HTTPException, status
from sqlalchemy.orm import Session
from loguru import logger
from jose import jwt, JWTError

from app.core.websocket import connection_manager
from app.core.config import settings
from app.api.deps import get_db
from app.services.conversation_service import ConversationService
from app.services.auth_service import AuthService
from app.db.models.conversation import MessageRole
from app.db.models.user import UserRole


router = APIRouter()


@router.websocket("/chat/{session_id}")
async def customer_websocket(
    websocket: WebSocket,
    session_id: UUID,
    user_identifier: str = Query(..., description="Customer identifier"),
    db: Session = Depends(get_db),
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
    conversation_service = ConversationService(db)

    # Verify session exists
    session = conversation_service.get_session(session_id)
    if not session:
        await websocket.close(code=4004, reason="Session not found")
        return

    # Verify user_identifier matches session
    if session.user_identifier != user_identifier:
        await websocket.close(code=4003, reason="Unauthorized")
        return

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

                # Save message to database
                message = conversation_service.add_message(
                    session_id=session_id,
                    role=MessageRole.USER,
                    content=content,
                )

                # Broadcast to all agents watching this session
                await connection_manager.send_to_agents(
                    session_id=session_id,
                    message={
                        "type": "customer_message",
                        "message_id": str(message.id),
                        "content": content,
                        "user_identifier": user_identifier,
                        "timestamp": message.created_at.isoformat(),
                    }
                )

                logger.info(f"Customer message in session {session_id}: {content[:50]}")

            elif message_type == "typing":
                # Customer is typing - notify agents
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
    db: Session = Depends(get_db),
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
    auth_service = AuthService(db)
    conversation_service = ConversationService(db)

    # Authenticate agent
    try:
        # Decode JWT token
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        agent_id = UUID(payload.get("sub"))  # User ID is stored in 'sub' claim
        tenant_id = UUID(payload.get("tenant_id"))

        # Get agent user
        from app.db.models.user import User
        agent = db.query(User).filter(User.id == agent_id).first()

        if not agent or not agent.is_active:
            await websocket.close(code=4003, reason="Unauthorized")
            return

        # Verify agent has appropriate role
        if agent.role not in [UserRole.TENANT_ADMIN, UserRole.SUPPORT_AGENT]:
            await websocket.close(code=4003, reason="Insufficient permissions")
            return

    except JWTError as e:
        logger.error(f"JWT verification failed: {e}")
        await websocket.close(code=4003, reason="Invalid token")
        return
    except Exception as e:
        logger.error(f"Agent authentication failed: {e}")
        await websocket.close(code=4003, reason="Authentication failed")
        return

    # Verify session exists and belongs to agent's tenant
    session = conversation_service.get_session(session_id)
    if not session:
        await websocket.close(code=4004, reason="Session not found")
        return

    if session.tenant_id != tenant_id:
        await websocket.close(code=4003, reason="Access denied")
        return

    # Connect agent
    await connection_manager.connect_agent(
        websocket=websocket,
        session_id=session_id,
        agent_id=agent_id,
        agent_name=agent.full_name,
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

                # Save message to database
                message = conversation_service.add_message(
                    session_id=session_id,
                    role=MessageRole.HUMAN_AGENT,
                    content=content,
                    meta={
                        "agent_id": str(agent_id),
                        "agent_name": agent.full_name,
                    }
                )

                # Send to customer
                await connection_manager.send_to_customer(
                    session_id=session_id,
                    message={
                        "type": "agent_message",
                        "message_id": str(message.id),
                        "content": content,
                        "agent_name": agent.full_name,
                        "timestamp": message.created_at.isoformat(),
                    }
                )

                # Notify other agents watching this session
                await connection_manager.send_to_agents(
                    session_id=session_id,
                    message={
                        "type": "agent_message",
                        "message_id": str(message.id),
                        "content": content,
                        "agent_name": agent.full_name,
                        "timestamp": message.created_at.isoformat(),
                    },
                    exclude_agent_id=agent_id,
                )

                logger.info(f"Agent {agent.full_name} sent message in session {session_id}")

            elif message_type == "typing":
                # Agent is typing - notify customer
                is_typing = data.get("is_typing", False)
                await connection_manager.send_typing_indicator(
                    session_id=session_id,
                    is_typing=is_typing,
                    user_name=agent.full_name,
                    to_customer=True,  # Send to customer
                )

    except WebSocketDisconnect:
        logger.info(f"Agent {agent.full_name} disconnected from session {session_id}")
        connection_manager.disconnect(websocket)

        # Notify customer that agent left
        await connection_manager.send_to_customer(
            session_id=session_id,
            message={
                "type": "agent_left",
                "agent_name": agent.full_name,
                "timestamp": None,
            }
        )

    except Exception as e:
        logger.error(f"Error in agent WebSocket: {e}")
        connection_manager.disconnect(websocket)

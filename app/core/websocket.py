"""WebSocket connection manager for real-time chat."""

import asyncio
import json
import uuid
from datetime import datetime
from typing import Any
from uuid import UUID

import redis.asyncio as aioredis
from fastapi import WebSocket
from loguru import logger

from app.core.config import settings


class ConnectionManager:
    """
    Manages WebSocket connections for real-time chat between customers and agents.

    Maintains two separate connection pools:
    - customer_connections: Customer chat widget connections
    - agent_connections: Support agent dashboard connections

    Each session can have:
    - One customer connection (the person chatting)
    - Multiple agent connections (agents monitoring or responding)
    """

    def __init__(self) -> None:
        # session_id -> WebSocket
        self.customer_connections: dict[str, WebSocket] = {}

        # session_id -> Set of WebSockets (multiple agents can watch same session)
        self.agent_connections: dict[str, set[WebSocket]] = {}

        # websocket -> (session_id, user_info) for tracking who's connected
        self.connection_metadata: dict[WebSocket, dict] = {}
        self._redis_client: Any = None
        self._listener_task: asyncio.Task | None = None
        self._instance_id = str(uuid.uuid4())
        self._event_channel = "chat_ws_events"

    async def connect_customer(
        self,
        websocket: WebSocket,
        session_id: UUID,
        user_identifier: str,
    ) -> None:
        """
        Connect a customer to a chat session.

        Args:
            websocket: WebSocket connection
            session_id: Chat session ID
            user_identifier: Customer identifier
        """
        await websocket.accept()
        session_key = str(session_id)

        # Disconnect existing customer connection if any (only one per session)
        if session_key in self.customer_connections:
            old_ws = self.customer_connections[session_key]
            try:
                await old_ws.close(code=1000, reason="New connection established")
            except Exception as e:
                logger.warning(f"Error closing old customer connection: {e}")

        self.customer_connections[session_key] = websocket
        self.connection_metadata[websocket] = {
            "type": "customer",
            "session_id": session_key,
            "user_identifier": user_identifier,
            "connected_at": datetime.utcnow(),
        }

        logger.info(f"Customer connected to session {session_id}")

    async def connect_agent(
        self,
        websocket: WebSocket,
        session_id: UUID,
        agent_id: UUID,
        agent_name: str,
    ) -> None:
        """
        Connect an agent to a chat session.

        Args:
            websocket: WebSocket connection
            session_id: Chat session ID
            agent_id: Agent user ID
            agent_name: Agent's full name
        """
        await websocket.accept()
        session_key = str(session_id)

        # Initialize agent connections set for this session if needed
        if session_key not in self.agent_connections:
            self.agent_connections[session_key] = set()

        self.agent_connections[session_key].add(websocket)
        self.connection_metadata[websocket] = {
            "type": "agent",
            "session_id": session_key,
            "agent_id": str(agent_id),
            "agent_name": agent_name,
            "connected_at": datetime.utcnow(),
        }

        logger.info(f"Agent {agent_name} connected to session {session_id}")

    def disconnect(self, websocket: WebSocket) -> None:
        """
        Disconnect a WebSocket and clean up.

        Args:
            websocket: WebSocket to disconnect
        """
        if websocket not in self.connection_metadata:
            return

        metadata = self.connection_metadata[websocket]
        session_key = metadata["session_id"]
        connection_type = metadata["type"]

        if connection_type == "customer":
            if session_key in self.customer_connections:
                if self.customer_connections[session_key] == websocket:
                    del self.customer_connections[session_key]
            logger.info(f"Customer disconnected from session {session_key}")

        elif connection_type == "agent":
            if session_key in self.agent_connections:
                self.agent_connections[session_key].discard(websocket)
                if not self.agent_connections[session_key]:
                    del self.agent_connections[session_key]
            logger.info(
                f"Agent {metadata.get('agent_name')} disconnected from session {session_key}"
            )

        del self.connection_metadata[websocket]

    async def send_to_customer(
        self,
        session_id: UUID,
        message: dict,
    ) -> bool:
        """
        Send a message to the customer in a session.

        Args:
            session_id: Chat session ID
            message: Message data (will be JSON serialized)

        Returns:
            True if sent successfully, False otherwise
        """
        session_key = str(session_id)

        if session_key not in self.customer_connections:
            logger.debug(f"No customer connection for session {session_id}")
            return False

        websocket = self.customer_connections[session_key]

        try:
            await websocket.send_json(message)
            return True
        except Exception as e:
            logger.error(f"Error sending to customer: {e}")
            self.disconnect(websocket)
            return False

    async def _get_redis_client(self) -> Any:
        """Get or create Redis client for chat websocket fanout."""
        if self._redis_client is None:
            self._redis_client = await aioredis.from_url(
                settings.REDIS_URL, encoding="utf-8", decode_responses=True
            )
        return self._redis_client

    async def start(self) -> None:
        """Start cross-worker Redis listener for websocket chat events."""
        if self._listener_task and not self._listener_task.done():
            return
        self._listener_task = asyncio.create_task(self._listen_for_remote_events())
        logger.info("Chat WebSocket Redis listener started")

    async def stop(self) -> None:
        """Stop cross-worker Redis listener and close Redis client."""
        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
            self._listener_task = None

        if self._redis_client is not None:
            try:
                await self._redis_client.close()
            except Exception as close_error:
                logger.warning(f"Failed to close Redis client: {close_error}")
            self._redis_client = None

    async def _publish_event(
        self,
        session_id: UUID,
        message: dict,
        to_customer: bool,
        to_agents: bool,
        exclude_agent_id: UUID | None = None,
    ) -> None:
        """Publish chat event so other API workers can deliver to local sockets."""
        try:
            redis_client = await self._get_redis_client()
            payload = {
                "instance_id": self._instance_id,
                "session_id": str(session_id),
                "message": message,
                "to_customer": to_customer,
                "to_agents": to_agents,
                "exclude_agent_id": str(exclude_agent_id) if exclude_agent_id else None,
            }
            await redis_client.publish(self._event_channel, json.dumps(payload))
        except Exception as publish_error:
            logger.error(f"Failed to publish chat websocket event: {publish_error}")

    async def _listen_for_remote_events(self) -> None:
        """Listen for chat events from other workers and deliver to local sockets."""
        pubsub = None
        try:
            redis_client = await self._get_redis_client()
            pubsub = redis_client.pubsub()
            await pubsub.subscribe(self._event_channel)

            async for raw in pubsub.listen():
                if raw.get("type") != "message":
                    continue
                data = raw.get("data")
                if not isinstance(data, str):
                    continue
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    logger.warning("Ignoring invalid chat websocket payload from Redis")
                    continue

                if payload.get("instance_id") == self._instance_id:
                    continue

                session_raw = payload.get("session_id")
                message = payload.get("message")
                if not isinstance(session_raw, str) or not isinstance(message, dict):
                    continue

                try:
                    session_id = UUID(session_raw)
                except ValueError:
                    continue

                to_customer = bool(payload.get("to_customer"))
                to_agents = bool(payload.get("to_agents"))
                exclude_agent_raw = payload.get("exclude_agent_id")
                exclude_agent_id = None
                if isinstance(exclude_agent_raw, str):
                    try:
                        exclude_agent_id = UUID(exclude_agent_raw)
                    except ValueError:
                        exclude_agent_id = None

                await self._deliver_event_local(
                    session_id=session_id,
                    message=message,
                    to_customer=to_customer,
                    to_agents=to_agents,
                    exclude_agent_id=exclude_agent_id,
                )
        except asyncio.CancelledError:
            raise
        except Exception as listener_error:
            logger.error(f"Chat websocket Redis listener error: {listener_error}")
        finally:
            if pubsub is not None:
                try:
                    await pubsub.unsubscribe(self._event_channel)
                    await pubsub.close()
                except Exception:
                    pass

    async def _deliver_event_local(
        self,
        session_id: UUID,
        message: dict,
        to_customer: bool,
        to_agents: bool,
        exclude_agent_id: UUID | None = None,
    ) -> None:
        """Deliver a chat event only to local in-memory socket connections."""
        if to_customer:
            await self.send_to_customer(session_id=session_id, message=message)
        if to_agents:
            await self.send_to_agents(
                session_id=session_id,
                message=message,
                exclude_agent_id=exclude_agent_id,
            )

    async def fanout_event(
        self,
        session_id: UUID,
        message: dict,
        to_customer: bool,
        to_agents: bool,
        exclude_agent_id: UUID | None = None,
    ) -> None:
        """
        Deliver event to local sockets and publish to Redis so all workers fan out.
        """
        await self._deliver_event_local(
            session_id=session_id,
            message=message,
            to_customer=to_customer,
            to_agents=to_agents,
            exclude_agent_id=exclude_agent_id,
        )
        await self._publish_event(
            session_id=session_id,
            message=message,
            to_customer=to_customer,
            to_agents=to_agents,
            exclude_agent_id=exclude_agent_id,
        )

    async def send_to_agents(
        self,
        session_id: UUID,
        message: dict,
        exclude_agent_id: UUID | None = None,
    ) -> int:
        """
        Send a message to all agents watching a session.

        Args:
            session_id: Chat session ID
            message: Message data (will be JSON serialized)
            exclude_agent_id: Optional agent ID to exclude (e.g., sender)

        Returns:
            Number of agents who received the message
        """
        session_key = str(session_id)

        if session_key not in self.agent_connections:
            logger.debug(f"No agent connections for session {session_id}")
            return 0

        agents = self.agent_connections[session_key].copy()
        sent_count = 0

        for websocket in agents:
            metadata = self.connection_metadata.get(websocket, {})

            # Skip excluded agent
            if exclude_agent_id and metadata.get("agent_id") == str(exclude_agent_id):
                continue

            try:
                await websocket.send_json(message)
                sent_count += 1
            except Exception as e:
                logger.error(f"Error sending to agent: {e}")
                self.disconnect(websocket)

        return sent_count

    async def broadcast_to_session(
        self,
        session_id: UUID,
        message: dict,
        exclude_websocket: WebSocket | None = None,
    ) -> None:
        """
        Broadcast a message to everyone in a session (customer + all agents).

        Args:
            session_id: Chat session ID
            message: Message data (will be JSON serialized)
            exclude_websocket: Optional WebSocket to exclude (e.g., sender)
        """
        session_key = str(session_id)

        # Send to customer
        if session_key in self.customer_connections:
            customer_ws = self.customer_connections[session_key]
            if customer_ws != exclude_websocket:
                try:
                    await customer_ws.send_json(message)
                except Exception as e:
                    logger.error(f"Error broadcasting to customer: {e}")
                    self.disconnect(customer_ws)

        # Send to all agents
        if session_key in self.agent_connections:
            agents = self.agent_connections[session_key].copy()
            for agent_ws in agents:
                if agent_ws != exclude_websocket:
                    try:
                        await agent_ws.send_json(message)
                    except Exception as e:
                        logger.error(f"Error broadcasting to agent: {e}")
                        self.disconnect(agent_ws)

    async def send_typing_indicator(
        self,
        session_id: UUID,
        is_typing: bool,
        user_name: str,
        to_customer: bool = False,
    ) -> None:
        """
        Send typing indicator to relevant parties.

        Args:
            session_id: Chat session ID
            is_typing: Whether user is typing
            user_name: Name of person typing
            to_customer: If True, send to customer; if False, send to agents
        """
        message = {
            "type": "typing_indicator",
            "is_typing": is_typing,
            "user_name": user_name,
            "timestamp": datetime.utcnow().isoformat(),
        }

        if to_customer:
            await self.send_to_customer(session_id, message)
        else:
            await self.send_to_agents(session_id, message)

    def get_session_info(self, session_id: UUID) -> dict:
        """
        Get information about connections for a session.

        Args:
            session_id: Chat session ID

        Returns:
            Dictionary with connection info
        """
        session_key = str(session_id)

        return {
            "session_id": session_key,
            "customer_connected": session_key in self.customer_connections,
            "agents_connected": len(self.agent_connections.get(session_key, set())),
            "agent_names": [
                self.connection_metadata[ws].get("agent_name")
                for ws in self.agent_connections.get(session_key, set())
                if ws in self.connection_metadata
            ],
        }


# Global singleton instance
connection_manager = ConnectionManager()

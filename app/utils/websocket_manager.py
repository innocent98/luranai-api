"""WebSocket manager for real-time job progress updates."""

import json
from typing import Any
from uuid import UUID

import redis.asyncio as aioredis
from fastapi import WebSocket
from loguru import logger

from app.core.config import settings


class WebSocketManager:
    """
    Manages WebSocket connections for real-time job progress updates.

    Each client can subscribe to specific job IDs and receive
    real-time updates when job status/progress changes.
    """

    def __init__(self) -> None:
        # job_id -> set of connected websockets
        self.active_connections: dict[str, set[WebSocket]] = {}
        # Redis client for pub/sub (lazy initialization)
        self._redis_client = None

    async def connect(self, websocket: WebSocket, job_id: UUID) -> None:
        """
        Accept WebSocket connection and subscribe to job updates.

        Args:
            websocket: WebSocket connection
            job_id: Job ID to subscribe to
        """
        await websocket.accept()

        job_id_str = str(job_id)
        if job_id_str not in self.active_connections:
            self.active_connections[job_id_str] = set()

        self.active_connections[job_id_str].add(websocket)
        logger.info(f"WebSocket connected for job {job_id}")

    def disconnect(self, websocket: WebSocket, job_id: UUID) -> None:
        """
        Disconnect WebSocket and unsubscribe from job updates.

        Args:
            websocket: WebSocket connection
            job_id: Job ID subscribed to
        """
        job_id_str = str(job_id)

        if job_id_str in self.active_connections:
            self.active_connections[job_id_str].discard(websocket)

            # Clean up empty job subscriptions
            if not self.active_connections[job_id_str]:
                del self.active_connections[job_id_str]

        logger.info(f"WebSocket disconnected for job {job_id}")

    async def _get_redis_client(self) -> Any:
        """Get or create Redis client for pub/sub."""
        if self._redis_client is None:
            self._redis_client = await aioredis.from_url(
                settings.REDIS_URL, encoding="utf-8", decode_responses=True
            )
        return self._redis_client

    async def broadcast_job_update(self, job_id: UUID, update: dict) -> None:
        """
        Broadcast job update to all connected clients subscribed to this job.

        This method now uses Redis Pub/Sub to broadcast updates across containers:
        - Worker publishes updates to Redis channel
        - API subscribes to channel and forwards to WebSocket clients

        Args:
            job_id: Job ID
            update: Update data (status, progress, etc.)
        """
        job_id_str = str(job_id)

        # Publish to Redis channel (for cross-container communication)
        try:
            redis_client = await self._get_redis_client()
            channel = f"job_updates:{job_id_str}"
            message = json.dumps(update)
            await redis_client.publish(channel, message)
            logger.debug(f"Published job update to Redis channel {channel}")
        except Exception as e:
            logger.error(f"Error publishing to Redis: {e}")

        # Also broadcast to local WebSocket connections (if any)
        if job_id_str not in self.active_connections:
            logger.debug(f"No local WebSocket connections for job {job_id}")
            return

        # Prepare message
        message = json.dumps(update)

        # Broadcast to all connected clients
        disconnected = set()

        for websocket in self.active_connections[job_id_str]:
            try:
                await websocket.send_text(message)
                logger.debug(f"Sent update to WebSocket client for job {job_id}")
            except Exception as e:
                logger.warning(f"Error sending to WebSocket client: {e}")
                disconnected.add(websocket)

        # Clean up disconnected clients
        for websocket in disconnected:
            self.active_connections[job_id_str].discard(websocket)

    def get_connection_count(self, job_id: UUID) -> int:
        """
        Get number of active connections for a job.

        Args:
            job_id: Job ID

        Returns:
            Number of active connections
        """
        job_id_str = str(job_id)
        return len(self.active_connections.get(job_id_str, set()))

    async def subscribe_to_job_updates(self, job_id: UUID):
        """
        Subscribe to Redis pub/sub channel for a specific job.

        This is used by the API container to listen for updates from the worker
        and forward them to WebSocket clients.

        Args:
            job_id: Job ID to subscribe to

        Yields:
            dict: Job update messages
        """
        job_id_str = str(job_id)
        channel = f"job_updates:{job_id_str}"

        try:
            redis_client = await self._get_redis_client()
            pubsub = redis_client.pubsub()
            await pubsub.subscribe(channel)

            logger.info(f"Subscribed to Redis channel: {channel}")

            async for message in pubsub.listen():
                if message["type"] == "message":
                    try:
                        update = json.loads(message["data"])
                        logger.debug(f"Received update from Redis for job {job_id}")
                        yield update
                    except json.JSONDecodeError as e:
                        logger.error(f"Invalid JSON from Redis: {e}")

        except Exception as e:
            logger.error(f"Error in Redis subscription: {e}")
        finally:
            try:
                await pubsub.unsubscribe(channel)
                logger.info(f"Unsubscribed from Redis channel: {channel}")
            except Exception:
                pass


# Global WebSocket manager instance
websocket_manager = WebSocketManager()

"""WebSocket manager for real-time job progress updates."""

from typing import Dict, Set
from uuid import UUID
import json

from fastapi import WebSocket
from loguru import logger


class WebSocketManager:
    """
    Manages WebSocket connections for real-time job progress updates.

    Each client can subscribe to specific job IDs and receive
    real-time updates when job status/progress changes.
    """

    def __init__(self):
        # job_id -> set of connected websockets
        self.active_connections: Dict[str, Set[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, job_id: UUID):
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

    def disconnect(self, websocket: WebSocket, job_id: UUID):
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

    async def broadcast_job_update(self, job_id: UUID, update: dict):
        """
        Broadcast job update to all connected clients subscribed to this job.

        Args:
            job_id: Job ID
            update: Update data (status, progress, etc.)
        """
        job_id_str = str(job_id)

        if job_id_str not in self.active_connections:
            logger.debug(f"No active connections for job {job_id}")
            return

        # Prepare message
        message = json.dumps(update)

        # Broadcast to all connected clients
        disconnected = set()

        for websocket in self.active_connections[job_id_str]:
            try:
                await websocket.send_text(message)
                logger.debug(f"Sent update to client for job {job_id}")
            except Exception as e:
                logger.warning(f"Error sending to client: {e}")
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


# Global WebSocket manager instance
websocket_manager = WebSocketManager()

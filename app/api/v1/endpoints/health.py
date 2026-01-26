from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session
from datetime import datetime, timezone
from typing import Dict, Any
import redis.asyncio as aioredis
from qdrant_client import QdrantClient

from app.db.session import get_db
from app.core.config import settings
from app.core.logger import log

router = APIRouter()


async def check_redis() -> Dict[str, Any]:
    """Check Redis connectivity."""
    try:
        client = await aioredis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=5
        )
        await client.ping()
        info = await client.info("server")
        await client.close()

        return {
            "status": "healthy",
            "version": info.get("redis_version", "unknown"),
        }
    except Exception as e:
        log.error(f"Redis health check failed: {e}")
        return {"status": "unhealthy", "error": str(e)}


def check_qdrant() -> Dict[str, Any]:
    """Check Qdrant connectivity."""
    try:
        client = QdrantClient(
            url=settings.QDRANT_URL,
            api_key=settings.QDRANT_API_KEY,
            timeout=5
        )
        collections = client.get_collections()
        collection_exists = any(
            c.name == settings.QDRANT_COLLECTION_NAME
            for c in collections.collections
        )

        return {
            "status": "healthy",
            "collections_count": len(collections.collections),
            "collection_exists": collection_exists,
        }
    except Exception as e:
        log.error(f"Qdrant health check failed: {e}")
        return {"status": "unhealthy", "error": str(e)}


def check_database(db: Session) -> Dict[str, Any]:
    """Check database connectivity."""
    try:
        db.execute(text("SELECT 1")).scalar()
        version = db.execute(text("SELECT version()")).scalar()
        connections = db.execute(
            text("SELECT count(*) FROM pg_stat_activity")
        ).scalar()

        return {
            "status": "healthy",
            "version": version.split()[0] if version else "unknown",
            "active_connections": connections,
        }
    except Exception as e:
        log.error(f"Database health check failed: {e}")
        return {"status": "unhealthy", "error": str(e)}


@router.get("")
async def health_check(db: Session = Depends(get_db)):
    """
    Comprehensive health check.
    Returns 200 if all healthy, 503 if degraded.
    """
    db_status = check_database(db)
    redis_status = await check_redis()
    qdrant_status = check_qdrant()

    all_healthy = all([
        db_status["status"] == "healthy",
        redis_status["status"] == "healthy",
        qdrant_status["status"] == "healthy",
    ])

    response = {
        "status": "healthy" if all_healthy else "degraded",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "environment": settings.ENVIRONMENT,
        "version": settings.VERSION,
        "checks": {
            "database": db_status,
            "redis": redis_status,
            "qdrant": qdrant_status,
        }
    }

    if not all_healthy:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=response
        )

    return response

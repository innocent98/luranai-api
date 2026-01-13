"""ARQ background worker startup script."""

import asyncio
from arq import run_worker
from arq.connections import RedisSettings

from app.core.config import settings
from app.workers.document_processor import WorkerSettings


def main():
    """Start the ARQ worker."""
    print(f"Starting Luran background worker...")
    print(f"Redis URL: {settings.REDIS_URL}")
    print(f"Worker functions: {[f.__name__ for f in WorkerSettings.functions]}")
    print("=" * 60)

    # Run worker
    run_worker(WorkerSettings)


if __name__ == '__main__':
    main()

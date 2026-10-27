"""ARQ background worker startup script."""

import asyncio
from arq import run_worker
from arq.connections import RedisSettings

from app.core.config import settings
from app.workers.document_processor import WorkerSettings


def function_names() -> list[str]:
    """Names of the registered jobs. arq `func(...)` wrappers expose `.name`, bare coroutines `__name__`."""
    return [getattr(f, "name", None) or getattr(f, "__name__", repr(f)) for f in WorkerSettings.functions]


def main():
    """Start the ARQ worker."""
    print(f"Starting Luran background worker...")
    print(f"Redis URL: {settings.REDIS_URL}")
    print(f"Worker functions: {function_names()}")
    print("=" * 60)

    # Run worker
    run_worker(WorkerSettings)


if __name__ == '__main__':
    main()

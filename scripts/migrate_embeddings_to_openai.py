"""
Migration script to re-embed all knowledge chunks with OpenAI embeddings.

This script:
1. Fetches all knowledge chunks from PostgreSQL
2. Generates new embeddings using OpenAI (text-embedding-3-small, 1536 dimensions)
3. Recreates the Qdrant collection with new dimension
4. Uploads all vectors to Qdrant
5. Tracks progress in Redis (resume capability)
6. Respects rate limits

Usage:
    python scripts/migrate_embeddings_to_openai.py [--dry-run] [--batch-size 100] [--resume]

Options:
    --dry-run: Simulate migration without making changes
    --batch-size: Number of chunks to process per batch (default: 100)
    --resume: Resume from last checkpoint (uses Redis for state)
"""

import asyncio
import sys
from pathlib import Path

# Add project root to Python path
sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import time
from typing import List, Optional

import redis.asyncio as aioredis
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.db.models import KnowledgeChunk
from app.utils.embedding import embedding_service
from app.utils.qdrant_client import qdrant_manager


class MigrationProgress:
    """Track migration progress in Redis."""

    def __init__(self, redis_client: aioredis.Redis):
        self.redis = redis_client
        self.key = "migration:openai_embeddings:progress"

    async def get_last_processed_id(self) -> Optional[str]:
        """Get the ID of the last successfully processed chunk."""
        return await self.redis.get(self.key)

    async def set_last_processed_id(self, chunk_id: str) -> None:
        """Update the last successfully processed chunk ID."""
        await self.redis.set(self.key, chunk_id, ex=86400 * 7)  # 7 days expiry

    async def reset(self) -> None:
        """Reset migration progress."""
        await self.redis.delete(self.key)

    async def get_stats(self) -> dict:
        """Get migration statistics."""
        stats_key = "migration:openai_embeddings:stats"
        stats = await self.redis.hgetall(stats_key)
        return {
            "total_processed": int(stats.get("total_processed", 0)),
            "successful": int(stats.get("successful", 0)),
            "failed": int(stats.get("failed", 0)),
            "start_time": stats.get("start_time"),
        }

    async def update_stats(self, total: int, successful: int, failed: int) -> None:
        """Update migration statistics."""
        stats_key = "migration:openai_embeddings:stats"
        await self.redis.hset(
            stats_key,
            mapping={
                "total_processed": total,
                "successful": successful,
                "failed": failed,
            },
        )
        await self.redis.expire(stats_key, 86400 * 7)  # 7 days expiry

    async def set_start_time(self) -> None:
        """Record migration start time."""
        stats_key = "migration:openai_embeddings:stats"
        await self.redis.hset(stats_key, "start_time", str(time.time()))


class OpenAIEmbeddingMigrator:
    """Migrate embeddings from Gemini to OpenAI."""

    def __init__(self, dry_run: bool = False, batch_size: int = 100):
        self.dry_run = dry_run
        self.batch_size = batch_size
        self.redis_client: Optional[aioredis.Redis] = None
        self.progress: Optional[MigrationProgress] = None
        self.db_session: Optional[AsyncSession] = None

        # Stats
        self.total_chunks = 0
        self.processed = 0
        self.successful = 0
        self.failed = 0
        self.start_time = 0.0

    async def setup(self) -> None:
        """Initialize connections."""
        logger.info("Setting up migration environment...")

        # Connect to Redis
        self.redis_client = await aioredis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
        )
        self.progress = MigrationProgress(self.redis_client)

        # Create async database engine
        engine = create_async_engine(
            settings.DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://"),
            echo=False,
        )
        async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        self.db_session = async_session()

        logger.info("Migration environment ready")

    async def cleanup(self) -> None:
        """Close connections."""
        if self.redis_client:
            await self.redis_client.close()
        if self.db_session:
            await self.db_session.close()

    async def recreate_qdrant_collection(self) -> bool:
        """Recreate Qdrant collection with new dimension (1536)."""
        logger.info("Recreating Qdrant collection with 1536 dimensions...")

        if self.dry_run:
            logger.info("[DRY RUN] Would delete and recreate collection")
            return True

        try:
            # Delete existing collection
            try:
                qdrant_manager.client.delete_collection(qdrant_manager.collection_name)
                logger.info(f"Deleted old collection '{qdrant_manager.collection_name}'")
            except Exception as e:
                logger.warning(f"Could not delete collection (may not exist): {e}")

            # Create new collection with 1536 dimension
            success = qdrant_manager.ensure_collection()
            if success:
                logger.info(
                    f"Created new collection '{qdrant_manager.collection_name}' with 1536 dimensions"
                )
            return success

        except Exception as e:
            logger.error(f"Error recreating collection: {e}")
            return False

    async def get_chunks_to_migrate(self, resume: bool = False) -> List[KnowledgeChunk]:
        """Fetch all knowledge chunks from database."""
        logger.info("Fetching knowledge chunks from database...")

        try:
            query = select(KnowledgeChunk).order_by(KnowledgeChunk.created_at)

            # Resume from last checkpoint
            if resume and self.progress:
                last_id = await self.progress.get_last_processed_id()
                if last_id:
                    logger.info(f"Resuming from chunk ID: {last_id}")
                    query = query.where(KnowledgeChunk.id > last_id)

            result = await self.db_session.execute(query)
            chunks = result.scalars().all()

            logger.info(f"Found {len(chunks)} chunks to migrate")
            return chunks

        except Exception as e:
            logger.error(f"Error fetching chunks: {e}")
            return []

    async def migrate_batch(self, chunks: List[KnowledgeChunk]) -> tuple[int, int]:
        """
        Migrate a batch of chunks.

        Returns:
            Tuple of (successful_count, failed_count)
        """
        successful = 0
        failed = 0

        for chunk in chunks:
            try:
                # Generate new OpenAI embedding
                logger.debug(f"Generating embedding for chunk {chunk.id}")
                embedding = await embedding_service.generate_embedding(
                    chunk.content,
                    use_rate_limit=True,  # Respect rate limits
                )

                if embedding is None:
                    logger.error(f"Failed to generate embedding for chunk {chunk.id}")
                    failed += 1
                    continue

                # Verify dimension
                if len(embedding) != 1536:
                    logger.error(
                        f"Invalid embedding dimension for chunk {chunk.id}: {len(embedding)}"
                    )
                    failed += 1
                    continue

                if not self.dry_run:
                    # Upsert to Qdrant
                    success = qdrant_manager.upsert_vector(
                        vector_id=str(chunk.id),
                        embedding=embedding,
                        tenant_id=chunk.tenant_id,
                        chunk_id=chunk.id,
                        source_id=chunk.source_id,
                        content=chunk.content,
                        metadata=chunk.metadata or {},
                    )

                    if not success:
                        logger.error(f"Failed to upsert vector for chunk {chunk.id}")
                        failed += 1
                        continue

                    # Update progress checkpoint
                    if self.progress:
                        await self.progress.set_last_processed_id(str(chunk.id))

                successful += 1
                logger.debug(f"Successfully migrated chunk {chunk.id}")

            except Exception as e:
                logger.error(f"Error migrating chunk {chunk.id}: {e}")
                failed += 1

        return successful, failed

    async def run(self, resume: bool = False) -> None:
        """Run the migration."""
        try:
            await self.setup()

            # Record start time
            self.start_time = time.time()
            if self.progress:
                await self.progress.set_start_time()

            logger.info(
                f"Starting OpenAI embedding migration (dry_run={self.dry_run}, batch_size={self.batch_size})"
            )

            # Step 1: Recreate Qdrant collection
            if not await self.recreate_qdrant_collection():
                logger.error("Failed to recreate Qdrant collection. Aborting.")
                return

            # Step 2: Get chunks to migrate
            chunks = await self.get_chunks_to_migrate(resume=resume)
            self.total_chunks = len(chunks)

            if self.total_chunks == 0:
                logger.info("No chunks to migrate")
                return

            # Step 3: Process in batches
            logger.info(f"Processing {self.total_chunks} chunks in batches of {self.batch_size}")

            for i in range(0, self.total_chunks, self.batch_size):
                batch = chunks[i : i + self.batch_size]
                batch_num = (i // self.batch_size) + 1
                total_batches = (self.total_chunks + self.batch_size - 1) // self.batch_size

                logger.info(f"\n=== Batch {batch_num}/{total_batches} ({len(batch)} chunks) ===")

                # Migrate batch
                batch_successful, batch_failed = await self.migrate_batch(batch)

                self.successful += batch_successful
                self.failed += batch_failed
                self.processed += len(batch)

                # Update stats
                if self.progress:
                    await self.progress.update_stats(self.processed, self.successful, self.failed)

                # Log progress
                elapsed = time.time() - self.start_time
                rate = self.processed / elapsed if elapsed > 0 else 0
                eta = (self.total_chunks - self.processed) / rate if rate > 0 else 0

                logger.info(
                    f"Progress: {self.processed}/{self.total_chunks} "
                    f"({self.processed/self.total_chunks*100:.1f}%) | "
                    f"Success: {self.successful} | Failed: {self.failed} | "
                    f"Rate: {rate:.1f} chunks/s | ETA: {eta/60:.1f} min"
                )

            # Final summary
            elapsed = time.time() - self.start_time
            logger.info("\n" + "=" * 60)
            logger.info("Migration completed!")
            logger.info(f"Total chunks: {self.total_chunks}")
            logger.info(f"Successful: {self.successful}")
            logger.info(f"Failed: {self.failed}")
            logger.info(f"Time elapsed: {elapsed/60:.1f} minutes")
            logger.info(f"Average rate: {self.processed/elapsed:.1f} chunks/second")
            logger.info("=" * 60)

            if self.dry_run:
                logger.info("\n[DRY RUN] No changes were made to the database or Qdrant")

        except Exception as e:
            logger.error(f"Migration failed: {e}")
            raise

        finally:
            await self.cleanup()


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Migrate embeddings to OpenAI")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate migration without making changes",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Number of chunks to process per batch (default: 100)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from last checkpoint",
    )
    args = parser.parse_args()

    # Configure logger
    logger.remove()  # Remove default handler
    logger.add(
        sys.stdout,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
        level="INFO",
    )

    # Run migration
    migrator = OpenAIEmbeddingMigrator(dry_run=args.dry_run, batch_size=args.batch_size)
    await migrator.run(resume=args.resume)


if __name__ == "__main__":
    asyncio.run(main())

"""
Initialize Qdrant collection on first run.

This script ensures the Qdrant collection exists with proper configuration.
Run this before starting the application for the first time.
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from app.utils.qdrant_client import qdrant_manager
from loguru import logger


def main():
    """Initialize Qdrant collection."""
    logger.info("Initializing Qdrant collection...")

    try:
        # Ensure collection exists
        success = qdrant_manager.ensure_collection()

        if success:
            logger.success("✓ Qdrant collection initialized successfully")

            # Get collection info
            count = qdrant_manager.count_vectors()
            logger.info(f"Current vector count: {count}")

            logger.info(f"Collection name: {qdrant_manager.collection_name}")
            logger.info(f"Vector dimension: {qdrant_manager.dimension}")
            logger.info(f"Distance metric: COSINE")

        else:
            logger.error("✗ Failed to initialize Qdrant collection")
            sys.exit(1)

    except Exception as e:
        logger.error(f"Error initializing Qdrant: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()

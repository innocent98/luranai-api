"""
Test script for document ingestion pipeline.

This script demonstrates the full document processing flow:
- Text chunking
- Embedding generation
- Dual storage (PostgreSQL + Qdrant)
"""

import asyncio
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from app.utils.chunking import text_chunker
from app.utils.embedding import embedding_service
from app.utils.qdrant_client import qdrant_manager
from loguru import logger


# Sample documents for testing
SAMPLE_DOCUMENTS = {
    "insurance_faq": """
What is covered under comprehensive insurance?

Comprehensive insurance covers damage to your vehicle that is not caused by a collision. This includes:
- Theft and vandalism
- Fire and explosions
- Natural disasters (floods, earthquakes, hurricanes)
- Falling objects (tree branches, hail)
- Animal strikes (hitting a deer)
- Glass damage (windshield cracks)

Comprehensive coverage is optional but highly recommended for protecting your investment.

How do I file a claim?

Filing a claim is simple and can be done in several ways:
1. Call our 24/7 claims hotline at 1-800-INSURANCE
2. Use our mobile app to submit photos and details
3. Visit any of our local offices
4. File online through your customer portal

You'll need your policy number, date and location of incident, and any relevant documentation like police reports or photos.

What is the deductible?

A deductible is the amount you pay out-of-pocket before your insurance coverage kicks in. For example, if you have a $500 deductible and file a $3000 claim, you pay $500 and we cover the remaining $2500.

Common deductible amounts:
- $250 (higher premiums, lower out-of-pocket)
- $500 (balanced option)
- $1000 (lower premiums, higher out-of-pocket)

You can adjust your deductible during renewal to balance premium costs.

Can I add drivers to my policy?

Yes! You can add additional drivers to your policy at any time. Common scenarios:
- Adding a teenage driver (may increase premiums)
- Adding a spouse or partner
- Adding elderly parents

Each driver's record will be evaluated and may affect your premium. To add a driver, contact your agent or use the online portal.

What happens if I miss a payment?

If you miss a payment, here's what happens:
1. Grace period: You have 10 days from the due date to make payment without penalty
2. Late fee: A $25 late fee is applied after the grace period
3. Coverage lapse: After 30 days of non-payment, coverage may be suspended
4. Policy cancellation: After 60 days, the policy may be cancelled

We'll send multiple reminders via email and text. Set up autopay to avoid missing payments!
""",

    "product_help": """
Getting Started with Aegiscribe

Welcome to Aegiscribe, your AI-powered note-taking assistant!

Installation and Setup

1. Download the app from our website or app store
2. Create an account using your email or Google/Microsoft account
3. Complete the onboarding tutorial (5 minutes)
4. Start taking notes!

Creating Your First Note

To create a note:
- Click the "New Note" button in the top-right
- Or use the keyboard shortcut: Cmd+N (Mac) or Ctrl+N (Windows)
- Start typing immediately - no need to save, we auto-save every 2 seconds

Organizing Notes

Use our flexible organization system:
- Notebooks: Group related notes together
- Tags: Add multiple tags for cross-referencing
- Favorites: Star important notes for quick access
- Search: Use full-text search to find anything instantly

AI Features

Aegiscribe's AI helps you work smarter:
- Smart summaries: Generate instant summaries of long notes
- Grammar checking: Real-time grammar and spelling corrections
- Content suggestions: Get writing suggestions based on context
- Auto-tagging: AI suggests relevant tags automatically

Collaboration

Work together with your team:
- Share individual notes or entire notebooks
- Set permissions (view, comment, or edit)
- See real-time changes from collaborators
- Comment threads for discussions

Syncing Across Devices

Your notes sync automatically across all devices:
- Web app (works in any browser)
- Desktop apps (Mac, Windows, Linux)
- Mobile apps (iOS, Android)
- Offline mode: Access notes without internet

Privacy and Security

We take your data seriously:
- End-to-end encryption for sensitive notebooks
- Two-factor authentication available
- Regular security audits
- GDPR and SOC2 compliant
- Your data is never used to train AI models

Keyboard Shortcuts

Power user shortcuts:
- Cmd/Ctrl + N: New note
- Cmd/Ctrl + S: Search
- Cmd/Ctrl + K: Quick command palette
- Cmd/Ctrl + /: Toggle markdown preview
- Cmd/Ctrl + B: Bold text
- Cmd/Ctrl + I: Italic text

Troubleshooting

Common issues and solutions:

Sync not working?
- Check your internet connection
- Log out and log back in
- Clear app cache in settings

AI features not responding?
- Ensure you have an active subscription
- Check if AI services are operational on our status page
- Contact support if issue persists

Can't find a note?
- Use advanced search filters (date, tags, content type)
- Check if note is in archive or trash
- Restore from version history if accidentally modified

Contact Support

Need help? We're here for you:
- In-app chat: Click the help icon
- Email: support@aegiscribe.com
- Phone: 1-800-AEGIS-01
- Help center: help.aegiscribe.com
"""
}


async def test_chunking():
    """Test text chunking utility."""
    logger.info("=" * 60)
    logger.info("Testing Text Chunking")
    logger.info("=" * 60)

    for doc_name, doc_text in SAMPLE_DOCUMENTS.items():
        logger.info(f"\nProcessing: {doc_name}")
        logger.info(f"Document length: {len(doc_text)} characters")

        chunks = text_chunker.chunk_text(doc_text, metadata={'source': doc_name})

        logger.info(f"Created {len(chunks)} chunks")
        for i, chunk in enumerate(chunks[:3]):  # Show first 3 chunks
            logger.info(f"\nChunk {i}:")
            logger.info(f"  Tokens: {chunk['token_count']}")
            logger.info(f"  Preview: {chunk['content'][:100]}...")


async def test_embedding():
    """Test embedding generation."""
    logger.info("\n" + "=" * 60)
    logger.info("Testing Embedding Generation")
    logger.info("=" * 60)

    test_texts = [
        "What is covered under comprehensive insurance?",
        "Getting started with our product is easy and takes just 5 minutes.",
        "You can file a claim online, by phone, or through our mobile app."
    ]

    for text in test_texts:
        logger.info(f"\nText: {text[:80]}...")

        embedding = await embedding_service.generate_embedding(text)

        if embedding:
            logger.info(f"✓ Embedding generated: dimension={len(embedding)}")
            logger.info(f"  First 5 values: {embedding[:5]}")
        else:
            logger.error("✗ Embedding generation failed")


async def test_qdrant():
    """Test Qdrant vector operations."""
    logger.info("\n" + "=" * 60)
    logger.info("Testing Qdrant Operations")
    logger.info("=" * 60)

    # Ensure collection exists
    success = qdrant_manager.ensure_collection()
    logger.info(f"Collection status: {'✓ Ready' if success else '✗ Failed'}")

    # Get current vector count
    count = qdrant_manager.count_vectors()
    logger.info(f"Current vector count: {count}")


async def test_full_pipeline():
    """Test the complete ingestion pipeline."""
    logger.info("\n" + "=" * 60)
    logger.info("Testing Full Ingestion Pipeline")
    logger.info("=" * 60)

    # Take first document
    doc_name, doc_text = list(SAMPLE_DOCUMENTS.items())[0]
    logger.info(f"\nProcessing: {doc_name}")

    # Step 1: Chunk
    logger.info("\nStep 1: Chunking...")
    chunks = text_chunker.chunk_text(doc_text)
    logger.info(f"✓ Created {len(chunks)} chunks")

    # Step 2: Generate embeddings
    logger.info("\nStep 2: Generating embeddings...")
    chunk_texts = [c['content'] for c in chunks[:3]]  # Test with first 3
    embeddings = await embedding_service.generate_batch_embeddings(chunk_texts)
    success_count = sum(1 for e in embeddings if e is not None)
    logger.info(f"✓ Generated {success_count}/{len(chunk_texts)} embeddings")

    # Show pipeline statistics
    logger.info("\nPipeline Statistics:")
    logger.info(f"  Total chunks: {len(chunks)}")
    logger.info(f"  Avg tokens/chunk: {sum(c['token_count'] for c in chunks) / len(chunks):.1f}")
    logger.info(f"  Embedding dimension: {len(embeddings[0]) if embeddings[0] else 0}")


async def main():
    """Run all tests."""
    logger.info("Document Ingestion Pipeline Test")
    logger.info("=" * 60)

    try:
        await test_chunking()
        await test_embedding()
        await test_qdrant()
        await test_full_pipeline()

        logger.info("\n" + "=" * 60)
        logger.info("All tests completed successfully!")
        logger.info("=" * 60)

    except Exception as e:
        logger.error(f"Test failed: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())

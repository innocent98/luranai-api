# OpenAI Migration - Environment Variables

This document describes the environment variable changes required for the OpenAI migration.

## Required Changes

### Remove These Variables

```bash
# Remove Gemini/Google AI configuration
GOOGLE_API_KEY=...
EMBEDDING_MODEL=models/gemini-embedding-001
GEMINI_MODEL=models/gemini-2.5-flash-lite
```

### Add These Variables

```bash
# OpenAI API Configuration
OPENAI_API_KEY=sk-proj-...  # Get from https://platform.openai.com/api-keys
OPENAI_EMBEDDING_MODEL=text-embedding-3-small
OPENAI_LLM_MODEL=gpt-4o-mini
```

### Update These Variables

```bash
# Update embedding dimension from 3072 to 1536
EMBEDDING_DIMENSION=1536  # Changed from 3072
```

### Keep Unchanged

```bash
# These remain the same
QDRANT_URL=...
QDRANT_COLLECTION_NAME=luran_knowledge  # Same name, just recreated with new dimension
REDIS_URL=...
DATABASE_URL=...
# ... all other settings
```

## Complete Example .env

Here's a complete example of the updated `.env` file:

```bash
# Project Info
PROJECT_NAME=Luran
ENVIRONMENT=production
SERVER_HOST=0.0.0.0
SERVER_PORT=8000
FRONTEND_URL=https://app.luran.ai
FRONTEND_DOC_URL=https://luran.ai

# Security
SECRET_KEY=your-secure-secret-key-here-min-32-chars
ACCESS_TOKEN_EXPIRE_MINUTES=10080

# Database
DATABASE_URL=postgresql://user:password@localhost:5432/luran
DATABASE_POOL_SIZE=20
DATABASE_MAX_OVERFLOW=40

# Redis
REDIS_URL=redis://localhost:6379

# Qdrant Vector Database
QDRANT_URL=http://localhost:6333
QDRANT_COLLECTION_NAME=luran_knowledge
QDRANT_API_KEY=  # Optional, leave empty for local

# OpenAI (NEW)
OPENAI_API_KEY=sk-proj-your-api-key-here
OPENAI_EMBEDDING_MODEL=text-embedding-3-small
OPENAI_LLM_MODEL=gpt-4o-mini
EMBEDDING_DIMENSION=1536

# Email (Optional)
SMTP_TLS=true
SMTP_PORT=587
SMTP_HOST=smtp.gmail.com
SMTP_USER=your-email@gmail.com
SMTP_PASSWORD=your-app-password
EMAILS_FROM_EMAIL=your-email@gmail.com
EMAILS_FROM_NAME=Luran

# Admin
FIRST_SUPERUSER_EMAIL=admin@luran.ai
FIRST_SUPERUSER_PASSWORD=your-secure-password-here

# Cloudinary (Optional)
CLOUDINARY_CLOUD_NAME=your-cloud-name
CLOUDINARY_API_KEY=your-api-key
CLOUDINARY_API_SECRET=your-api-secret
CLOUDINARY_FOLDER=luran/widget_logos

# Paystack Payment Gateway
PAYSTACK_SECRET_KEY=sk_live_your-secret-key
PAYSTACK_PUBLIC_KEY=pk_live_your-public-key
PAYSTACK_BASE_URL=https://api.paystack.co

# Production Server
API_WORKERS=4
LOG_LEVEL=info

# CORS Origins (comma-separated)
BACKEND_CORS_ORIGINS=https://app.luran.ai,https://luran.ai
```

## Docker Compose Updates

If using Docker Compose, update the environment variables in `docker-compose.yml`:

```yaml
services:
  api:
    environment:
      # Replace these:
      - GOOGLE_API_KEY=${GOOGLE_API_KEY}
      - EMBEDDING_MODEL=${EMBEDDING_MODEL}
      - GEMINI_MODEL=${GEMINI_MODEL}

      # With these:
      - OPENAI_API_KEY=${OPENAI_API_KEY}
      - OPENAI_EMBEDDING_MODEL=${OPENAI_EMBEDDING_MODEL}
      - OPENAI_LLM_MODEL=${OPENAI_LLM_MODEL}

      # Update this:
      - EMBEDDING_DIMENSION=1536  # Changed from 3072

  worker:
    environment:
      # Same changes as api service
      - OPENAI_API_KEY=${OPENAI_API_KEY}
      - OPENAI_EMBEDDING_MODEL=${OPENAI_EMBEDDING_MODEL}
      - OPENAI_LLM_MODEL=${OPENAI_LLM_MODEL}
      - EMBEDDING_DIMENSION=1536
```

## Getting Your OpenAI API Key

1. Go to https://platform.openai.com/api-keys
2. Sign up or log in to your OpenAI account
3. Click "Create new secret key"
4. Copy the key (starts with `sk-proj-...`)
5. Add it to your `.env` file as `OPENAI_API_KEY`

## Rate Limits

The system is configured with conservative rate limits (90% of OpenAI's limits):

- **Embeddings**: 2,700 requests/minute (90% of 3,000 RPM limit)
- **LLM**: 450 requests/minute (90% of 500 RPM limit)

These limits are for Tier 1 usage. If you upgrade to higher tiers, you can adjust the rate limiters in `app/utils/rate_limiter.py`.

## Verification

After updating your environment variables, verify the configuration:

```bash
# Test embedding generation
docker-compose exec api python -c "
from app.utils.embedding import embedding_service
import asyncio
embedding = asyncio.run(embedding_service.generate_embedding('test query'))
print(f'Embedding dimension: {len(embedding)}')  # Should be 1536
"

# Check configuration
docker-compose exec api python -c "
from app.core.config import settings
print(f'OpenAI API Key: {settings.OPENAI_API_KEY[:10]}...')
print(f'Embedding Model: {settings.OPENAI_EMBEDDING_MODEL}')
print(f'LLM Model: {settings.OPENAI_LLM_MODEL}')
print(f'Embedding Dimension: {settings.EMBEDDING_DIMENSION}')
"
```

## Troubleshooting

### Invalid API Key

If you see `"OPENAI_API_KEY must start with 'sk-'"`, verify:
- Your API key is correct and copied completely
- The key starts with `sk-proj-` or `sk-`
- There are no extra spaces or quotes

### Rate Limit Errors

If you encounter rate limit errors:
- Check your OpenAI usage tier at https://platform.openai.com/settings/organization/limits
- Adjust rate limiters in `app/utils/rate_limiter.py` if needed
- Monitor usage at https://platform.openai.com/usage

### Embedding Dimension Mismatch

If you see dimension errors:
- Ensure `EMBEDDING_DIMENSION=1536` in `.env`
- Recreate the Qdrant collection (see migration script)
- Verify embeddings are 1536 dimensions in tests

## Cost Monitoring

Monitor your OpenAI costs at https://platform.openai.com/usage

Expected monthly costs for 300K conversations:
- Input tokens: ~₦162,540 (~$108 USD)
- Output tokens: ~₦117,000 (~$78 USD)
- **Total**: ~₦279,540 (~$186 USD/month)

Set up billing alerts in your OpenAI dashboard to avoid unexpected charges.

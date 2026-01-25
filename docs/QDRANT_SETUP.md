# Qdrant Collection Setup - Dev vs Prod

## Overview

The Luran platform uses separate Qdrant collections for development and production environments to ensure complete data isolation.

## Collection Strategy

| Environment | Collection Name | Purpose |
|-------------|----------------|---------|
| **Development** | `luran_knowledge` | Local testing and development |
| **Production** | `luran_knowledge_prod` | Production data, customer-facing |

## Configuration

### Development (.env)
```bash
ENVIRONMENT=development
QDRANT_COLLECTION_NAME=luran_knowledge
```

### Production (.env or environment variables)
```bash
ENVIRONMENT=production
QDRANT_COLLECTION_NAME=luran_knowledge_prod
```

## How It Works

1. **Automatic Collection Creation**
   - When the API starts, it automatically creates the collection if it doesn't exist
   - Collections are created via `qdrant_manager.ensure_collection()`
   - No manual setup required

2. **Complete Isolation**
   - Dev and prod collections are completely separate namespaces
   - No risk of mixing development test data with production data
   - Each collection has its own vectors, metadata, and configuration

3. **Same Qdrant Instance**
   - You can use the same Qdrant instance (local or cloud) for both environments
   - Collections are isolated by name, not by instance
   - Alternatively, use separate Qdrant instances for dev/prod (recommended for production)

## Migration Guide

### Current State (2026-01-25)
- Development collection: `luran_knowledge` (existing data preserved)
- Production collection: Not yet created (will be auto-created on first prod deployment)

### When Deploying to Production

1. **Update Production .env**
   ```bash
   cp .env.prod.example .env
   # Edit .env with production credentials
   QDRANT_COLLECTION_NAME=luran_knowledge_prod
   ```

2. **Deploy Application**
   - Collection `luran_knowledge_prod` will be auto-created on first API request
   - Initially empty - you'll upload production knowledge sources

3. **Upload Production Knowledge**
   - Use the admin dashboard or API to upload production documents
   - Knowledge chunks will be stored in `luran_knowledge_prod` collection

4. **Verify Separation**
   - Check Qdrant dashboard to see both collections
   - Dev: `luran_knowledge` (your test data)
   - Prod: `luran_knowledge_prod` (production data)

## Important Notes

- ✅ **Zero risk migration** - Existing dev data is untouched
- ✅ **No manual Qdrant operations needed** - Collections auto-created
- ✅ **Clear separation** - Dev experiments won't affect prod
- ⚠️ **Cannot rename collections in Qdrant** - Must create new collection and migrate data
- ⚠️ **Update QDRANT_API_KEY** for production (use different key than dev)

## Troubleshooting

### Collection not created
- Check API logs for Qdrant connection errors
- Verify `QDRANT_URL` and `QDRANT_API_KEY` are correct
- Check Qdrant is running: `curl http://localhost:6333/`

### Wrong collection being used
- Check `QDRANT_COLLECTION_NAME` in .env matches your environment
- Restart API after changing .env values

### Want to reset dev collection
```bash
# Option 1: Delete collection in Qdrant dashboard
# Option 2: Use different collection name in .env
QDRANT_COLLECTION_NAME=luran_knowledge_v2
```

## Related Files

- `.env` - Development configuration (current)
- `.env.prod.example` - Production configuration template
- `app/utils/qdrant_client.py` - Qdrant client implementation
- `app/core/config.py` - Configuration settings

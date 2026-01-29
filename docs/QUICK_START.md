# Quick Start Guide

## Prerequisites

✅ Database seeded with test data
✅ Docker services running (PostgreSQL, Redis, Qdrant)
✅ Full RAG chat endpoint implemented

## Test Credentials

### Superuser
- **Email:** admin@luranai.com
- **Password:** admin123

### Tenant Admin (Weysure Insurance)
- **Email:** admin@weysure.com
- **Password:** weysure123

### Tenant ID
- **Weysure:** `bb24bf9e-63c2-4497-b5f5-e46dd916f7ea`

## Start the Server

```bash
# Option 1: Using poetry
poetry run uvicorn app.main:app --reload

# Option 2: Using docker (if needed)
docker-compose up luran_api

# Option 3: Direct python
poetry run python -m app.main
```

The server will start on:
- **API:** http://localhost:8000
- **Docs:** http://localhost:8000/api/v1/docs
- **ReDoc:** http://localhost:8000/api/v1/redoc

## Test the Chat Endpoint

### Step 1: Login and Get Token

```bash
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{
    "email": "admin@weysure.com",
    "password": "weysure123"
  }'
```

**Response:**
```json
{
  "access_token": "eyJ...",
  "token_type": "bearer"
}
```

Save the token for next requests.

### Step 2: Upload Knowledge Document

```bash
TOKEN="your-token-here"

curl -X POST http://localhost:8000/api/v1/knowledge/upload \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id": "bb24bf9e-63c2-4497-b5f5-e46dd916f7ea",
    "document_name": "faq.txt",
    "document_text": "Q: What is covered under comprehensive insurance?\nA: Comprehensive insurance covers theft, vandalism, fire, natural disasters, falling objects, animal strikes, and glass damage.\n\nQ: How do I file a claim?\nA: You can file a claim 24/7 by calling 1-800-INSURANCE, using our mobile app, visiting a local office, or filing online through your customer portal.\n\nQ: What is a deductible?\nA: A deductible is the amount you pay before insurance coverage starts. Common amounts are $250, $500, or $1000.",
    "source_type": "faq"
  }'
```

**Note:** If Google AI API quota is exceeded, the document will be created but chunks won't be embedded. You'll need a valid API key with quota to test the full RAG flow.

### Step 3: Create Chat Session

```bash
curl -X POST http://localhost:8000/api/v1/chat/sessions \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id": "bb24bf9e-63c2-4497-b5f5-e46dd916f7ea",
    "user_identifier": "customer_001",
    "metadata": {
      "source": "web_chat",
      "user_agent": "Mozilla/5.0"
    }
  }'
```

**Response:**
```json
{
  "id": "session-uuid",
  "tenant_id": "tenant-uuid",
  "user_identifier": "customer_001",
  "status": "active",
  ...
}
```

### Step 4: Send Chat Message

```bash
SESSION_ID="session-uuid-from-step-3"

curl -X POST http://localhost:8000/api/v1/chat/message \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "message": "What is covered under comprehensive insurance?",
    "session_id": "'$SESSION_ID'"
  }'
```

**Response (with embeddings):**
```json
{
  "session_id": "session-uuid",
  "message": {
    "id": "message-uuid",
    "role": "assistant",
    "content": "Comprehensive insurance covers damage to your vehicle that is not caused by a collision. This includes theft, vandalism, fire...",
    "confidence_score": 0.85,
    "retrieved_chunk_ids": ["chunk-uuid-1", "chunk-uuid-2"],
    "token_count": 120
  },
  "should_escalate": false,
  "escalation_reason": null,
  "ticket_id": null
}
```

### Step 5: Continue Conversation (Or Create New Session)

You can either continue the existing session or start a new one:

**Continue existing:**
```bash
curl -X POST http://localhost:8000/api/v1/chat/message \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "message": "How do I file a claim?",
    "session_id": "'$SESSION_ID'"
  }'
```

**New session (auto-create):**
```bash
curl -X POST http://localhost:8000/api/v1/chat/message \
  -H "Content-Type: application/json" \
  -d '{
    "message": "What is a deductible?",
    "tenant_id": "bb24bf9e-63c2-4497-b5f5-e46dd916f7ea",
    "user_identifier": "customer_002"
  }'
```

## Test Escalation

Ask a question that triggers escalation:

```bash
curl -X POST http://localhost:8000/api/v1/chat/message \
  -H "Content-Type: application/json" \
  -d '{
    "message": "I want to speak to a supervisor about canceling my policy!",
    "session_id": "'$SESSION_ID'"
  }'
```

**Response (escalated):**
```json
{
  "session_id": "session-uuid",
  "message": {...},
  "should_escalate": true,
  "escalation_reason": "Escalation keyword detected: 'supervisor'",
  "ticket_id": "ticket-uuid"
}
```

A support ticket will be automatically created.

## API Endpoints Summary

### Authentication
- `POST /api/v1/auth/login` - Get JWT token
- `POST /api/v1/auth/register` - Register new user
- `GET /api/v1/auth/me` - Get current user info

### Knowledge Management
- `POST /api/v1/knowledge/upload` - Upload document
- `GET /api/v1/knowledge/tenants/{tenant_id}/sources` - List sources
- `GET /api/v1/knowledge/sources/{source_id}` - Get source details
- `DELETE /api/v1/knowledge/sources/{source_id}` - Delete source

### Chat
- `POST /api/v1/chat/sessions` - Create session
- `POST /api/v1/chat/message` - Send message (full RAG)
- `GET /api/v1/chat/sessions/{session_id}` - Get session details
- `GET /api/v1/chat/tenants/{tenant_id}/sessions` - List tenant sessions

### Tickets
- `GET /api/v1/tickets/tenants/{tenant_id}` - List tickets
- `GET /api/v1/tickets/{ticket_id}` - Get ticket details
- `PATCH /api/v1/tickets/{ticket_id}/assign` - Assign ticket
- `PATCH /api/v1/tickets/{ticket_id}/resolve` - Resolve ticket

### Tenants
- `GET /api/v1/tenants/{tenant_id}` - Get tenant
- `PATCH /api/v1/tenants/{tenant_id}` - Update tenant
- `GET /api/v1/tenants/{tenant_id}/ai-config` - Get AI config
- `PATCH /api/v1/tenants/{tenant_id}/ai-config` - Update AI config

## Interactive API Documentation

Visit http://localhost:8000/api/v1/docs for interactive Swagger UI where you can:
- See all endpoints
- Try requests directly
- View request/response schemas
- Authorize with JWT token

## Troubleshooting

### API Not Found (404)
- Make sure server is running: `poetry run uvicorn app.main:app --reload`
- Check the correct URL: `http://localhost:8000/api/v1/...`

### Unauthorized (401)
- Get a fresh JWT token via `/api/v1/auth/login`
- Include token in header: `Authorization: Bearer YOUR_TOKEN`

### Google AI Quota Exceeded
- The free tier has daily limits
- Use your own API key with proper quota
- Update `GOOGLE_API_KEY` in `.env`
- Or wait 24 hours for quota reset

### No Knowledge Retrieved
- Ensure documents were uploaded with embeddings
- Check `chunk_count` in upload response
- Verify Qdrant is running: `docker ps | grep qdrant`
- Check vector count: `poetry run python scripts/init_qdrant.py`

### Escalation Not Working
- Check AI config escalation keywords
- Verify confidence threshold settings
- Review session status: `GET /api/v1/chat/sessions/{session_id}`

## Next Steps

1. **Test Full Flow:** Upload document → Chat → Escalation → Ticket
2. **Configure AI:** Adjust confidence threshold, escalation keywords, brand voice
3. **Add More Knowledge:** Upload FAQs, help docs, policies
4. **Monitor:** Check logs, session history, ticket creation
5. **Production:** Update security settings, API keys, database credentials

## Complete RAG Flow Architecture

```
User Message
      ↓
Session Management (Get/Create)
      ↓
Get Tenant AI Config
      ↓
Vector Search (Qdrant) → Retrieve Relevant Chunks
      ↓
Calculate Confidence Score
      ↓
Check Escalation Rules
      ↓
Assemble Prompt (3-Layer Architecture):
  - System Prompt (Immutable)
  - Tenant Config (Brand voice, restrictions)
  - User Context (History + Retrieved chunks)
      ↓
Generate AI Response (Gemini)
      ↓
Save Assistant Message
      ↓
Create Ticket (if escalated)
      ↓
Return Response
```

## Files and Locations

- **Configuration:** `.env`
- **Database:** `postgresql://localhost:5432/luran_db`
- **Qdrant:** `http://localhost:6333`
- **API Logs:** Check terminal output
- **Scripts:** `scripts/` directory
- **Documentation:** `docs/` directory

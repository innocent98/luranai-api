# Luran - Multi-Tenant AI Support Platform

## Overview

Luran is a production-ready AI-powered customer support platform built with RAG (Retrieval-Augmented Generation). It enables multiple tenants to have their own isolated AI assistants powered by custom knowledge bases.

## Core Capabilities

- **Multi-Tenant Architecture** - Complete isolation per tenant
- **RAG-Based AI Chat** - Responses grounded in uploaded knowledge
- **Document Ingestion** - Automatic chunking, embedding, and indexing
- **Smart Escalation** - Automatic detection and ticket creation
- **Confidence Scoring** - Know when AI is uncertain
- **Conversation Management** - Track sessions and history

## Tech Stack

- **Backend:** FastAPI (Python 3.11+)
- **Database:** PostgreSQL 15
- **Vector DB:** Qdrant
- **AI/ML:** Google Gemma 3 12B & Gemini Embedding API
- **Cache:** Redis
- **Deployment:** Docker Compose

## Quick Start

### 1. Prerequisites

```bash
# Clone and navigate to project
cd luran

# Install dependencies
poetry install

# Start all services
docker-compose up -d
```

### 2. Access Points

- **API:** http://localhost:8000
- **API Docs:** http://localhost:8000/api/v1/docs
- **pgAdmin:** http://localhost:5050 (admin@luran.dev / admin123)
- **Qdrant Dashboard:** http://localhost:6333/dashboard

### 3. Test Credentials

```
Tenant Admin: admin@weysure.com / weysure123
Tenant ID: bb24bf9e-63c2-4497-b5f5-e46dd916f7ea
```

### 4. Seed Database (Already Done)

```bash
poetry run python scripts/seed_data.py
```

## Architecture Overview

### Data Flow

```
User Message
    ↓
Session Management (Create/Resume)
    ↓
Tenant AI Config (Get brand voice, rules)
    ↓
Vector Search (Find relevant knowledge)
    ↓
Confidence Calculation (Score relevance)
    ↓
Escalation Check (Keywords + confidence)
    ↓
Prompt Assembly (System + Tenant + Context)
    ↓
AI Generation (Gemini API)
    ↓
Save Message + Create Ticket (if escalated)
    ↓
Return Response
```

## Key Components

### 1. Document Ingestion Pipeline

**Purpose:** Turn documents into searchable knowledge

**Flow:**
1. Upload document text via API
2. Chunk into 400-token segments with 50-token overlap
3. Generate 768-dimensional embeddings (Google AI)
4. Store chunks in PostgreSQL
5. Store vectors in Qdrant with tenant filtering
6. Mark as active for searching

**API:**
```bash
POST /api/v1/knowledge/upload
{
  "tenant_id": "uuid",
  "document_name": "faq.txt",
  "document_text": "...",
  "source_type": "faq"
}
```

### 2. RAG Chat System

**Purpose:** Answer questions using uploaded knowledge

**3-Layer Prompt:**
- **Layer 1:** System prompt (immutable instructions)
- **Layer 2:** Tenant config (brand voice, restrictions)
- **Layer 3:** User context (history + retrieved knowledge)

**API:**
```bash
POST /api/v1/chat/message
{
  "message": "What is covered?",
  "tenant_id": "uuid",
  "user_identifier": "customer_123"
}
```

**Response:**
```json
{
  "session_id": "uuid",
  "message": {
    "content": "AI response...",
    "confidence_score": 0.85,
    "retrieved_chunk_ids": ["uuid1", "uuid2"]
  },
  "should_escalate": false
}
```

### 3. Escalation System

**Triggers:**
- Confidence score below threshold (default 0.55)
- Escalation keywords detected (e.g., "supervisor", "cancel policy")

**Actions:**
- Mark session as escalated
- Create support ticket automatically
- Return ticket ID in response

### 4. Tenant Configuration

Each tenant has customizable AI behavior:

```python
{
  "brand_voice": {
    "tone": "professional",
    "formality_level": "balanced",
    "greeting_style": "warm"
  },
  "allowed_topics": ["insurance", "claims", "billing"],
  "blocked_topics": ["medical advice", "legal advice"],
  "escalation_keywords": ["lawyer", "complaint", "supervisor"],
  "confidence_threshold": 0.55,
  "max_response_tokens": 400,
  "grounding_requirement": true  # Must use knowledge base
}
```

## Database Schema

### Core Tables

**tenants**
- Multi-tenant isolation
- API key authentication

**tenant_ai_configs**
- Brand voice and behavior settings
- Escalation rules

**knowledge_sources**
- Uploaded documents
- Version tracking

**knowledge_chunks**
- Text chunks with embeddings
- Linked to sources

**conversation_sessions**
- Chat sessions per user
- Status tracking (active/escalated/resolved)

**messages**
- Individual messages
- Confidence scores
- Retrieved chunks

**tickets**
- Escalated support requests
- Assignment and resolution tracking

## API Endpoints (37 total)

### Authentication
- `POST /api/v1/auth/login` - Get JWT token
- `POST /api/v1/auth/register` - Create user
- `GET /api/v1/auth/me` - Current user

### Knowledge Management
- `POST /api/v1/knowledge/upload` - Upload document
- `GET /api/v1/knowledge/tenants/{id}/sources` - List sources
- `GET /api/v1/knowledge/sources/{id}` - Get source details
- `DELETE /api/v1/knowledge/sources/{id}` - Delete source

### Chat
- `POST /api/v1/chat/sessions` - Create session
- `POST /api/v1/chat/message` - Send message (RAG)
- `GET /api/v1/chat/sessions/{id}` - Get session
- `GET /api/v1/chat/tenants/{id}/sessions` - List sessions

### Tickets
- `GET /api/v1/tickets/tenants/{id}` - List tickets
- `PATCH /api/v1/tickets/{id}/assign` - Assign ticket
- `PATCH /api/v1/tickets/{id}/resolve` - Resolve ticket

### Tenants (Admin)
- `GET /api/v1/tenants/{id}` - Get tenant
- `PATCH /api/v1/tenants/{id}/ai-config` - Update AI config

## Environment Variables

Key settings in `.env`:

```bash
# Database
DATABASE_URL=postgresql://user:password@localhost:5432/luran_db

# Google AI (Required for embeddings & responses)
GOOGLE_API_KEY=your-api-key-here
GEMINI_MODEL=models/gemma-3-12b-it  # Free: 30 RPM, 14.4K/day
EMBEDDING_MODEL=models/gemini-embedding-001  # Free: 90 RPM, 1K/day
EMBEDDING_DIMENSION=3072

# Qdrant
QDRANT_URL=http://localhost:6333
QDRANT_COLLECTION_NAME=luran_knowledge

# Security
SECRET_KEY=your-secret-key
ACCESS_TOKEN_EXPIRE_MINUTES=10080
```

## Development

### Run Locally

```bash
# Start services
docker-compose up -d

# Run API (development)
poetry run uvicorn app.main:app --reload

# Or use Docker
docker-compose up api
```

### Database Migrations

```bash
# Create migration
poetry run alembic revision --autogenerate -m "description"

# Apply migrations
poetry run alembic upgrade head

# Rollback
poetry run alembic downgrade -1
```

### Testing

```bash
# Run tests
poetry run pytest

# Test ingestion pipeline
poetry run python scripts/test_ingestion.py

# Initialize Qdrant
poetry run python scripts/init_qdrant.py
```

## Deployment Checklist

- [ ] Update `SECRET_KEY` in production
- [ ] Change default admin passwords
- [ ] Configure Google AI API key with quota
- [ ] Set up SSL/TLS
- [ ] Enable rate limiting
- [ ] Configure backups
- [ ] Set up monitoring (logs, metrics)
- [ ] Update CORS settings for production domains

## Troubleshooting

### API Quota Exceeded
The free Google AI tier has limits. Update `GOOGLE_API_KEY` with a paid key or wait for reset.

### No Knowledge Retrieved
- Check if documents were uploaded: `SELECT * FROM knowledge_sources;`
- Verify chunks exist: `SELECT COUNT(*) FROM knowledge_chunks WHERE is_active = true;`
- Check Qdrant: http://localhost:6333/dashboard

### Database Connection Failed
```bash
# Check if database is running
docker ps | grep luran_db

# View logs
docker logs luran_db

# Restart
docker-compose restart db
```

## Additional Resources

- **Chat Frontend Integration Guide:** `docs/CHAT_FRONTEND_INTEGRATION.md` - How to integrate the chat API with your frontend
- **Workflow Explained:** `docs/WORKFLOW_EXPLAINED.md` - Detailed explanation of document ingestion and chat workflows
- **Database Access Guide:** `docs/DATABASE_ACCESS.md` - How to access and query the database
- **Quick Start Guide:** `docs/QUICK_START.md` - Get up and running quickly
- **API Documentation:** http://localhost:8000/api/v1/docs - Interactive API documentation

## License

[Your License]

## Support

For issues and questions, please contact the development team.

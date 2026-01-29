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
- **pgAdmin:** http://localhost:5050 (admin@luranai.com / admin123)
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

- **User Roles & Tenants Explained:** `docs/USER_ROLES_AND_TENANTS_EXPLAINED.md` - Complete guide to multi-tenancy, user roles, and permissions
- **Subscription & Pricing Model:** `docs/SUBSCRIPTION_PRICING_MODEL.md` - Detailed documentation on capabilities, limits, and recommended pricing tiers
- **Chat Frontend Integration Guide:** `docs/CHAT_FRONTEND_INTEGRATION.md` - How to integrate the chat API with your frontend
- **Workflow Explained:** `docs/WORKFLOW_EXPLAINED.md` - Detailed explanation of document ingestion and chat workflows
- **Database Access Guide:** `docs/DATABASE_ACCESS.md` - How to access and query the database
- **Quick Start Guide:** `docs/QUICK_START.md` - Get up and running quickly
- **API Documentation:** http://localhost:8000/api/v1/docs - Interactive API documentation

## License

[Your License]

## Support

For issues and questions, please contact the development team.


# ==============================================================
Recommendations for Your Pricing Model:    -   Deprecated                                                                                                                          
                                                                                                                                                                          
  ## app/db/models/subscription.py or similar                                                                                                                              
                                                                                                                                                                          
  TIER_LIMITS = {                                                                                                                                                         
      "free": {                                                                                                                                                           
          "max_conversations": 100,                                                                                                                                       
          "max_response_tokens": 500,  # Shorter responses                                                                                                                
          "max_knowledge_mb": 10,                                                                                                                                         
      },                                                                                                                                                                  
      "starter": {                                                                                                                                                        
          "max_conversations": 1000,                                                                                                                                      
          "max_response_tokens": 800,                                                                                                                                     
          "max_knowledge_mb": 50,                                                                                                                                         
      },                                                                                                                                                                  
      "professional": {                                                                                                                                                   
          "max_conversations": 10000,                                                                                                                                     
          "max_response_tokens": 1500,                                                                                                                                    
          "max_knowledge_mb": 200,                                                                                                                                        
      },                                                                                                                                                                  
      "enterprise": {                                                                                                                                                     
          "max_conversations": None,  # Unlimited                                                                                                                         
          "max_response_tokens": 2000,                                                                                                                                    
          "max_knowledge_mb": 1000,                                                                                                                                       
      }                                                                                                                                                                   
  }                                                                                                                                                                       
                                                                                                                                                                          
  Protection Against Misuse:                                                                                                                                              
                                                                                                                                                                          
  You can add to your tenant config:                                                                                                                                      
  - Monthly token budget per tenant                                                                                                                                       
  - Rate limiting on AI calls                                                                                                                                             
  - Admin dashboard to monitor usage                                                                                                                                      
  - Alerts when tenant exceeds typical usage   



# =======================================================
  Step 2: Configure Google Drive                                                                                                                                          
                                                                                                                                                                          
  Now run this command in your terminal:                                                                                                                                  
                                                                                                                                                                          
  rclone config                                                                                                                                                           
                                                                                                                                                                          
  You'll see an interactive menu. Follow these steps exactly:                                                                                                             
                                                                                                                                                                          
  ---                                                                                                                                                                     
  📝 Configuration Prompts (Just copy my answers):                                                                                                                        
                                                                                                                                                                          
  1. New or existing remote?                                                                                                                                              
  Type: n                                                                                                                                                                 
                                                                                                                                                                          
  2. Name for this remote:                                                                                                                                                
  Type: gdrive                                                                                                                                                            
                                                                                                                                                                          
  3. Type of storage:                                                                                                                                                     
  Type: drive                                                                                                                                                             
  (or the number for "Google Drive" - usually 15 or 18)                                                                                                                   
                                                                                                                                                                          
  4. Google Application Client ID:                                                                                                                                        
  Press Enter (leave blank)                                                                                                                                               
                                                                                                                                                                          
  5. Google Application Client Secret:                                                                                                                                    
  Press Enter (leave blank)                                                                                                                                               
                                                                                                                                                                          
  6. Scope that rclone should use:                                                                                                                                        
  Type: 1                                                                                                                                                                 
  (Full access to all files)                                                                                                                                              
                                                                                                                                                                          
  7. Service Account Credentials JSON file path:                                                                                                                          
  Press Enter (leave blank)                                                                                                                                               
                                                                                                                                                                          
  8. Edit advanced config?                                                                                                                                                
  Type: n                                                                                                                                                                 
                                                                                                                                                                          
  9. Use web browser to automatically authenticate?                                                                                                                       
  Type: y                                                                                                                                                                 
                                                                                                                                                                          
  This will open your browser - Sign in with your Google account and click "Allow"                                                                                        
                                                                                                                                                                          
  10. Configure this as a Shared Drive?                                                                                                                                   
  Type: n                                                                                                                                                                 
                                                                                                                                                                          
  11. Keep this remote?                                                                                                                                                   
  Type: y                                                                                                                                                                 
                                                                                                                                                                          
  12. Quit config?                                                                                                                                                        
  Type: q           

                                                                                                                                                                    
# My Honest Recommendation                                                                                                                                                
                                                                                                                                                                          
  Better Approach:                                                                                                                                                        
                                                                                                                                                                          
  1. Don't Expose Token Configuration to Users                                                                                                                            
  - Users don't understand it                                                                                                                                             
  - Creates confusion and support burden                                                                                                                                  
  - They just want "good answers"                                                                                                                                         
                                                                                                                                                                          
  2. Set Smart, Generous Defaults per Tier                                                                                                                                
  - Let AI naturally determine response length                                                                                                                            
  - Don't artificially truncate                                                                                                                                           
  - Trust the system prompt to be concise when appropriate                                                                                                                
                                                                                                                                                                          
  3. Limit by What Users Understand                                                                                                                                       
  - Conversation volume (already doing this)                                                                                                                              
  - Feature access (analytics, integrations, etc.)                                                                                                                        
  - Number of knowledge documents                                                                                                                                         
  - Number of team members                                                                                                                                                
  - Support level (email vs priority)                                                                                                                                     
                                                                                                                                                                          
  Recommended Token Limits:                                                                                                                                               
  ┌──────────────┬──────────────────┬──────────────────────────┬────────────────────────────────────────┐                                                                 
  │     Tier     │ Conversations/Mo │       Token Limit        │               Rationale                │                                                                 
  ├──────────────┼──────────────────┼──────────────────────────┼────────────────────────────────────────┤                                                                 
  │ Free         │ 100              │ 800 (not configurable)   │ Showcase quality; cost is $0.02 total  │                                                                 
  ├──────────────┼──────────────────┼──────────────────────────┼────────────────────────────────────────┤                                                                 
  │ Starter      │ 1,000            │ 1,000 (not configurable) │ Good answers; cost is $0.19 total      │                                                                 
  ├──────────────┼──────────────────┼──────────────────────────┼────────────────────────────────────────┤                                                                 
  │ Professional │ 10,000           │ 1,500 (not configurable) │ Excellent answers; cost is $2.85 total │                                                                 
  ├──────────────┼──────────────────┼──────────────────────────┼────────────────────────────────────────┤                                                                 
  │ Enterprise   │ Unlimited*       │ 2,000 (configurable)     │ Premium experience; monitor outliers   │                                                                 
  └──────────────┴──────────────────┴──────────────────────────┴────────────────────────────────────────┘                                                                 
  *Unlimited with fair use policy                                                                                                                                         
                                                                                                                                                                          
  Alternative: Admin-Configurable Only                                                                                                                                    
                                                                                                                                                                          
  If you must expose it:                                                                                                                                                  
  - Make it admin/dashboard only (not user-facing)                                                                                                                        
  - Default to generous limits                                                                                                                                            
  - Let admins reduce if needed for specific tenants                                                                                                                      
  - Don't advertise it as a differentiator                                                                                                                                
                                                                                                                                                                          
  ## In tenant AI config (admin dashboard only)                                                                                                                            
  TIER_DEFAULTS = {                                                                                                                                                       
      "free": {                                                                                                                                                           
          "max_response_tokens": 800,                                                                                                                                     
          "can_override": False,  # Fixed                                                                                                                                 
      },                                                                                                                                                                  
      "starter": {                                                                                                                                                        
          "max_response_tokens": 1000,                                                                                                                                    
          "can_override": True,   # Admin can reduce if needed                                                                                                            
          "admin_note": "We don't recommend reducing this"                                                                                                                
      },                                                                                                                                                                  
      "professional": {                                                                                                                                                   
          "max_response_tokens": 1500,                                                                                                                                    
          "can_override": True,                                                                                                                                           
      },                                                                                                                                                                  
      "enterprise": {                                                                                                                                                     
          "max_response_tokens": 2000,                                                                                                                                    
          "can_override": True,                                                                                                                                           
          "can_increase": True,   # Can go above 2000 if needed                                                                                                           
      }                                                                                                                                                                   
  }                                                                                                                                                                       
                                                                                                                                                                          
  ---                                                                                                                                                                     
  What Successful SaaS Companies Do                                                                                                                                       
                                                                                                                                                                          
  Intercom, Zendesk, HubSpot:                                                                                                                                             
  - Don't limit response length by tier                                                                                                                                   
  - Limit by conversation volume                                                                                                                                          
  - Limit by features (integrations, analytics, etc.)                                                                                                                     
  - Users don't even know there's a token limit                                                                                                                           
                                                                                                                                                                          
  Why they do this:                                                                                                                                                       
  - Better user experience = higher retention                                                                                                                             
  - Cost is negligible vs subscription price                                                                                                                              
  - Quality perception matters for brand                                                                                                                                  
                                                                                                                                                                          
  What they DO limit by tier:                                                                                                                                             
  - Conversation volume ✅                                                                                                                                                
  - Team size ✅                                                                                                                                                          
  - Knowledge base size ✅                                                                                                                                                
  - Features (analytics, API, integrations) ✅                                                                                                                            
  - Support level ✅                                                                                                                                                      
                                                                                                                                                                          
  ---                                                                                                                                                                     
  Suggested Pricing Model Structure                                                                                                                                       
                                                                                                                                                                          
  PRICING_TIERS = {                                                                                                                                                       
      "free": {                                                                                                                                                           
          "price": 0,                                                                                                                                                     
          "conversations_monthly": 100,           # ✅ Main limiter                                                                                                       
          "max_response_tokens": 800,             # Generous for quality                                                                                                  
          "max_knowledge_mb": 10,                 # ✅ Limit                                                                                                              
          "team_members": 1,                      # ✅ Limit                                                                                                              
          "features": {                                                                                                                                                   
              "analytics": False,                 # ✅ Feature gate                                                                                                       
              "api_access": False,                                                                                                                                        
              "custom_branding": False,                                                                                                                                   
              "priority_support": False,                                                                                                                                  
          }                                                                                                                                                               
      },                                                                                                                                                                  
      "starter": {                                                                                                                                                        
          "price": 29,                                                                                                                                                    
          "conversations_monthly": 1000,          # ✅ Main limiter                                                                                                       
          "max_response_tokens": 1000,            # Quality preserved                                                                                                     
          "max_knowledge_mb": 100,                                                                                                                                        
          "team_members": 3,                                                                                                                                              
          "features": {                                                                                                                                                   
              "analytics": True,                  # ✅ Feature gate                                                                                                       
              "api_access": False,                                                                                                                                        
              "custom_branding": True,                                                                                                                                    
              "priority_support": False,                                                                                                                                  
          }                                                                                                                                                               
      },                                                                                                                                                                  
      "professional": {                                                                                                                                                   
          "price": 99,                                                                                                                                                    
          "conversations_monthly": 10000,         # ✅ Main limiter                                                                                                       
          "max_response_tokens": 1500,            # Excellent quality                                                                                                     
          "max_knowledge_mb": 500,                                                                                                                                        
          "team_members": 10,                                                                                                                                             
          "features": {                                                                                                                                                   
              "analytics": True,                                                                                                                                          
              "api_access": True,                 # ✅ Feature gate                                                                                                       
              "custom_branding": True,                                                                                                                                    
              "priority_support": True,                                                                                                                                   
              "white_label": False,                                                                                                                                       
          }                                                                                                                                                               
      },                                                                                                                                                                  
      "enterprise": {                                                                                                                                                     
          "price": "custom",                                                                                                                                              
          "conversations_monthly": None,           # Unlimited*                                                                                                           
          "max_response_tokens": 2000,                                                                                                                                    
          "max_knowledge_mb": None,                # Unlimited*                                                                                                           
          "team_members": None,                    # Unlimited                                                                                                            
          "features": {                                                                                                                                                   
              "analytics": True,                                                                                                                                          
              "api_access": True,                                                                                                                                         
              "custom_branding": True,                                                                                                                                    
              "priority_support": True,                                                                                                                                   
              "white_label": True,                                                                                                                                        
              "dedicated_support": True,                                                                                                                                  
              "sla": "99.9%",                                                                                                                                             
          }                                                                                                                                                               
      }                                                                                                                                                                   
  }                                                                                                                                                                       
                                                                                                                                                                          
  ---                                                                                                                                                                     
  Bottom Line Recommendations                                                                                                                                             
                                                                                                                                                                          
  What to Do:                                                                                                                                                             
                                                                                                                                                                          
  1. Set default to 800 tokens (not 400)                                                                                                                                  
    - Prevents frustrating truncations                                                                                                                                    
    - Still concise enough for most use cases                                                                                                                             
  2. Set tier limits generously:                                                                                                                                          
    - Free: 800                                                                                                                                                           
    - Starter: 1000                                                                                                                                                       
    - Professional: 1500                                                                                                                                                  
    - Enterprise: 2000                                                                                                                                                    
  3. Don't expose to end users                                                                                                                                            
    - Keep it admin-configurable only                                                                                                                                     
    - Most users won't need to touch it                                                                                                                                   
  4. Focus pricing differentiation on:                                                                                                                                    
    - Conversation volume (main limiter)                                                                                                                                  
    - Feature access (analytics, API, branding)                                                                                                                           
    - Team size                                                                                                                                                           
    - Support level                                                                                                                                                       
    - Knowledge base size                                                                                                                                                 
  5. Monitor actual usage:                                                                                                                                                
    - Track average tokens per conversation                                                                                                                               
    - Identify outliers                                                                                                                                                   
    - Adjust defaults based on data                                                                                                                                       
                                                                                                                                                                          
  Why This Works:                                                                                                                                                         
                                                                                                                                                                          
  ✅ Better UX - Answers aren't artificially cut off                                                                                                                      
  ✅ Higher retention - Users feel they're getting quality                                                                                                                
  ✅ Minimal cost - Extra $1-3/year per tenant is negligible                                                                                                              
  ✅ Competitive - Matches what successful SaaS companies do                                                                                                              
  ✅ Simpler - Users understand "1000 conversations/month" better than "800 tokens/response"                                                                              
                                                                                                                                                                          
  The cost difference between 400 and 1500 tokens is literally $1-2 per tenant per YEAR, while you're charging hundreds to thousands per year. Don't sacrifice user       
  experience to save pennies.   


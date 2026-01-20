# Luran AI - Complete Documentation
**Version:** 1.0.0
**Last Updated:** 2026-01-19
**Status:** ✅ Production Ready

---

## 📚 Table of Contents

1. [Overview](#overview)
2. [Quick Start](#quick-start)
3. [Authentication](#authentication)
4. [API Reference](#api-reference)
5. [Widget Integration](#widget-integration)
6. [Chat System](#chat-system)
7. [Knowledge Base](#knowledge-base)
8. [Support Tickets](#support-tickets)
9. [Analytics](#analytics)
10. [WebSocket Integration](#websocket-integration)
11. [Rate Limits & Quotas](#rate-limits--quotas)
12. [Security](#security)
13. [Error Handling](#error-handling)
14. [Best Practices](#best-practices)
15. [FAQ](#faq)

---

## Overview

### What is Luran?

Luran is an **intelligent customer support platform** that combines AI-powered chatbots with human agent escalation. It uses Retrieval-Augmented Generation (RAG) to provide accurate, context-aware responses from your knowledge base.

### Key Features

- ✨ **AI-Powered Chat** - Gemini-based responses with your knowledge
- 📚 **Knowledge Base Management** - Upload docs, auto-chunking, vector search
- 🎫 **Smart Ticket System** - Auto-escalation with priority detection
- 💬 **Real-Time Chat** - WebSocket for live agent conversations
- 📊 **Analytics Dashboard** - Conversation metrics, agent performance
- 🎨 **Customizable Widget** - Embeddable chat for your website
- 🔐 **Multi-Tenant** - Secure tenant isolation
- ⚡ **Background Processing** - Async document processing with progress tracking

### Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Customer's Website                        │
│  ┌────────────────────────────────────────────────────┐    │
│  │  Widget (Vanilla JS)                               │    │
│  │  • Floating button                                 │    │
│  │  • Chat interface (iframe)                         │    │
│  └────────────────────────────────────────────────────┘    │
└──────────────────────────┬──────────────────────────────────┘
                           │
                ┌──────────▼──────────┐
                │   Luran Backend     │
                ├─────────────────────┤
                │ • FastAPI           │
                │ • PostgreSQL        │
                │ • Redis             │
                │ • Qdrant (vectors)  │
                └──────────┬──────────┘
                           │
          ┌────────────────┼────────────────┐
          │                │                │
    ┌─────▼─────┐   ┌─────▼─────┐   ┌─────▼─────┐
    │  Google   │   │  ARQ      │   │  Email    │
    │  Gemini   │   │  Worker   │   │  Service  │
    │  AI       │   │  (Jobs)   │   │           │
    └───────────┘   └───────────┘   └───────────┘
```

---

## Quick Start

### 1. Register Your Company

```bash
POST /api/v1/company/register-company
```

```json
{
  "company_name": "Acme Corp",
  "admin_email": "admin@acme.com",
  "admin_name": "John Doe",
  "company_website": "https://acme.com"
}
```

**Response:**
```json
{
  "message": "Company registered successfully",
  "tenant_id": "550e8400-e29b-41d4-a716-446655440000",
  "user_id": "user-uuid",
  "temporary_password": "temp_abc123"
}
```

**Next:** Check your email for verification link.

---

### 2. Verify Email

```bash
POST /api/v1/company/verify-email
```

```json
{
  "token": "verification_token_from_email"
}
```

---

### 3. Login

```bash
POST /api/v1/auth/login
```

```json
{
  "email": "admin@acme.com",
  "password": "your_new_password"
}
```

**Response:**
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer",
  "user": {
    "id": "user-uuid",
    "email": "admin@acme.com",
    "full_name": "John Doe",
    "role": "TENANT_ADMIN",
    "tenant_id": "tenant-uuid"
  }
}
```

**Store this token!** Include it in all API requests:
```
Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

---

### 4. Upload Knowledge Base

```bash
POST /api/v1/knowledge/upload
Content-Type: multipart/form-data
Authorization: Bearer {token}
```

```
file: faq.pdf
source_type: FAQ
language: en
```

**Response:**
```json
{
  "job_id": "job-uuid",
  "source_id": "source-uuid",
  "status": "QUEUED",
  "message": "Document processing started"
}
```

**Track progress via WebSocket:**
```javascript
ws = new WebSocket('wss://api.luranai.com/api/v1/ws/jobs/{job_id}');
```

---

### 5. Create API Key for Widget

```bash
POST /api/v1/api-keys
Authorization: Bearer {token}
```

```json
{
  "name": "Website Widget",
  "key_type": "PUBLIC"
}
```

**Response:**
```json
{
  "id": "key-uuid",
  "key_type": "PUBLIC",
  "key_prefix": "pk_live_abc...",
  "full_key": "pk_live_abcdefghijk123456789",
  "name": "Website Widget",
  "created_at": "2026-01-19T10:00:00Z"
}
```

⚠️ **Save `full_key` immediately!** It's shown only once.

---

### 6. Install Widget on Your Website

```html
<!-- Add before </body> tag -->
<script>
  window.luranSettings = {
    tenantId: '550e8400-e29b-41d4-a716-446655440000',
    apiKey: 'pk_live_abcdefghijk123456789'
  };
</script>
<script src="https://cdn.luranai.com/widget/v1/widget.js" async defer></script>
```

**That's it!** Chat widget appears on your site.

---

## Authentication

### JWT-Based Authentication

Luran uses JWT (JSON Web Tokens) for API authentication.

#### Token Structure

```json
{
  "sub": "user_id",
  "tenant_id": "tenant_id",
  "exp": 1737302400,
  "iat": 1736697600
}
```

#### Token Lifetime

- **Default:** 7 days
- **Configurable:** Via `ACCESS_TOKEN_EXPIRE_DAYS` environment variable

#### How to Use

**1. Login to get token:**
```bash
POST /api/v1/auth/login
{
  "email": "user@example.com",
  "password": "secure_password"
}
```

**2. Include token in requests:**
```bash
GET /api/v1/tenant
Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

**3. Refresh when expired:**
- Re-login when token expires
- Monitor `exp` claim to refresh proactively

---

### API Key Authentication

Used for widget and server-to-server communication.

#### Key Types

| Type | Prefix | Use Case | Safe for Browser? |
|------|--------|----------|-------------------|
| PUBLIC | `pk_live_` | Widget configuration | ✅ Yes |
| SECRET | `sk_live_` | Server-to-server API | ❌ No |

#### Using API Keys

**Include in header:**
```
X-API-Key: pk_live_abcdefghijk123456789
```

**Example:**
```bash
GET /api/v1/widget/public/config/{tenant_id}
X-API-Key: pk_live_abcdefghijk123456789
```

---

### Role-Based Access Control (RBAC)

#### Tenant User Roles

| Role | Permissions | Use Case |
|------|-------------|----------|
| **TENANT_ADMIN** | Full access | Company owner/manager |
| **SUPPORT_AGENT** | Handle conversations, resolve tickets | Customer support team |
| **VIEWER** | Read-only access | Observers, analysts |

#### What Each Role Can Do

**TENANT_ADMIN:**
- ✅ Manage team members (invite, update, remove)
- ✅ Configure AI behavior
- ✅ Upload knowledge base
- ✅ View all analytics
- ✅ Manage API keys
- ✅ Configure widget
- ✅ Handle tickets

**SUPPORT_AGENT:**
- ✅ View conversations
- ✅ Take over from AI
- ✅ Send messages to customers
- ✅ Resolve assigned tickets
- ✅ View own performance metrics
- ❌ Cannot manage users or settings

**VIEWER:**
- ✅ View conversations
- ✅ View tickets
- ✅ View analytics
- ❌ Cannot modify anything

---

## API Reference

### Base URL

```
https://api.luranai.com/api/v1
```

---

### Health Check

#### Check API Status

```bash
GET /health
```

**Response:**
```json
{
  "status": "healthy",
  "timestamp": "2026-01-19T10:00:00Z"
}
```

---

### Authentication Endpoints

#### 1. Login

```bash
POST /auth/login
```

**Request:**
```json
{
  "email": "user@example.com",
  "password": "secure_password"
}
```

**Response:**
```json
{
  "access_token": "eyJhbGci...",
  "token_type": "bearer",
  "user": {
    "id": "user-uuid",
    "email": "user@example.com",
    "full_name": "John Doe",
    "role": "TENANT_ADMIN",
    "tenant_id": "tenant-uuid",
    "is_active": true
  }
}
```

**Errors:**
- `401 Unauthorized` - Invalid credentials
- `403 Forbidden` - Account deactivated

---

#### 2. Get Current User

```bash
GET /auth/me
Authorization: Bearer {token}
```

**Response:**
```json
{
  "id": "user-uuid",
  "email": "user@example.com",
  "full_name": "John Doe",
  "role": "TENANT_ADMIN",
  "tenant_id": "tenant-uuid",
  "is_active": true,
  "last_login_at": "2026-01-19T09:00:00Z",
  "created_at": "2026-01-01T00:00:00Z"
}
```

---

#### 3. Update Password

```bash
PUT /auth/me/password
Authorization: Bearer {token}
```

**Request:**
```json
{
  "current_password": "old_password",
  "new_password": "new_secure_password"
}
```

**Response:**
```json
{
  "message": "Password updated successfully"
}
```

---

### Tenant Management

#### 1. Get Tenant Details

```bash
GET /tenant
Authorization: Bearer {token}
```

**Response:**
```json
{
  "id": "tenant-uuid",
  "name": "acme_corp",
  "display_name": "Acme Corporation",
  "description": "AI-powered customer support",
  "status": "ACTIVE",
  "created_at": "2026-01-01T00:00:00Z",
  "updated_at": "2026-01-15T12:00:00Z",
  "ai_config": {
    "brand_voice": {
      "tone": "professional",
      "formality_level": "balanced"
    },
    "confidence_threshold": 0.55,
    "max_response_tokens": 400,
    "grounding_requirement": true
  },
  "capabilities": {
    "can_create_tickets": true,
    "can_access_analytics": true,
    "can_upload_knowledge": true,
    "max_knowledge_mb": 100
  }
}
```

---

#### 2. Update Tenant Information

```bash
PUT /tenant
Authorization: Bearer {token}
```

**Request:**
```json
{
  "display_name": "Acme Corporation Ltd.",
  "description": "We provide excellent support"
}
```

**Response:**
```json
{
  "id": "tenant-uuid",
  "name": "acme_corp",
  "display_name": "Acme Corporation Ltd.",
  "description": "We provide excellent support",
  "status": "ACTIVE"
}
```

---

#### 3. Get AI Configuration

```bash
GET /tenant/ai-config
Authorization: Bearer {token}
```

**Response:**
```json
{
  "id": "config-uuid",
  "tenant_id": "tenant-uuid",
  "brand_voice": {
    "tone": "professional",
    "formality_level": "balanced",
    "greeting_style": "warm",
    "emoji_usage": false
  },
  "allowed_topics": [
    "product features",
    "pricing",
    "technical support"
  ],
  "blocked_topics": [
    "competitor comparisons",
    "personal medical advice"
  ],
  "escalation_keywords": [
    "speak to human",
    "talk to manager",
    "cancel subscription"
  ],
  "confidence_threshold": 0.55,
  "max_response_tokens": 400,
  "grounding_requirement": true,
  "custom_instructions": "Always mention our 24/7 support hotline."
}
```

---

#### 4. Update AI Configuration

```bash
PUT /tenant/ai-config
Authorization: Bearer {token}
```

**Request:**
```json
{
  "brand_voice": {
    "tone": "friendly",
    "formality_level": "casual",
    "emoji_usage": true
  },
  "confidence_threshold": 0.6,
  "escalation_keywords": [
    "speak to human",
    "urgent",
    "manager"
  ]
}
```

**Response:**
```json
{
  "id": "config-uuid",
  "tenant_id": "tenant-uuid",
  "brand_voice": {
    "tone": "friendly",
    "formality_level": "casual",
    "emoji_usage": true
  },
  "confidence_threshold": 0.6,
  "escalation_keywords": [
    "speak to human",
    "urgent",
    "manager"
  ]
}
```

---

### User Management

*Requires TENANT_ADMIN role*

#### 1. Invite Team Member

```bash
POST /users
Authorization: Bearer {token}
```

**Request:**
```json
{
  "email": "agent@example.com",
  "full_name": "Jane Smith",
  "role": "SUPPORT_AGENT"
}
```

**Response:**
```json
{
  "id": "user-uuid",
  "email": "agent@example.com",
  "full_name": "Jane Smith",
  "role": "SUPPORT_AGENT",
  "tenant_id": "tenant-uuid",
  "is_active": true,
  "temporary_password": "temp_xyz789",
  "created_at": "2026-01-19T10:00:00Z"
}
```

📧 **Email sent to agent@example.com** with temp password and login link.

---

#### 2. List Team Members

```bash
GET /users
Authorization: Bearer {token}
```

**Response:**
```json
{
  "users": [
    {
      "id": "user-1",
      "email": "admin@example.com",
      "full_name": "John Doe",
      "role": "TENANT_ADMIN",
      "is_active": true
    },
    {
      "id": "user-2",
      "email": "agent@example.com",
      "full_name": "Jane Smith",
      "role": "SUPPORT_AGENT",
      "is_active": true
    }
  ],
  "total": 2
}
```

---

#### 3. Update Team Member

```bash
PATCH /users/{user_id}
Authorization: Bearer {token}
```

**Request:**
```json
{
  "full_name": "Jane Smith-Johnson",
  "role": "TENANT_ADMIN"
}
```

---

#### 4. Deactivate Team Member

```bash
DELETE /users/{user_id}
Authorization: Bearer {token}
```

**Response:**
```json
{
  "message": "User deactivated successfully"
}
```

---

### API Key Management

*Requires TENANT_ADMIN role*

#### 1. Create API Key

```bash
POST /api-keys
Authorization: Bearer {token}
```

**Request:**
```json
{
  "name": "Production Widget",
  "key_type": "PUBLIC"
}
```

**Response:**
```json
{
  "id": "key-uuid",
  "tenant_id": "tenant-uuid",
  "key_type": "PUBLIC",
  "key_prefix": "pk_live_abc...",
  "full_key": "pk_live_abcdefghijk123456789",
  "name": "Production Widget",
  "is_active": true,
  "created_at": "2026-01-19T10:00:00Z"
}
```

⚠️ **Save `full_key`!** Never shown again.

---

#### 2. List API Keys

```bash
GET /api-keys
Authorization: Bearer {token}
```

**Response:**
```json
{
  "keys": [
    {
      "id": "key-uuid-1",
      "key_type": "PUBLIC",
      "key_prefix": "pk_live_abc...",
      "name": "Production Widget",
      "is_active": true,
      "last_used_at": "2026-01-19T09:30:00Z",
      "created_at": "2026-01-01T00:00:00Z"
    },
    {
      "id": "key-uuid-2",
      "key_type": "SECRET",
      "key_prefix": "sk_live_xyz...",
      "name": "Server Integration",
      "is_active": true,
      "last_used_at": null,
      "created_at": "2026-01-10T00:00:00Z"
    }
  ]
}
```

---

#### 3. Revoke API Key

```bash
DELETE /api-keys/{key_id}
Authorization: Bearer {token}
```

**Response:**
```json
{
  "message": "API key revoked successfully"
}
```

---

### Knowledge Base

#### 1. Upload Document

```bash
POST /knowledge/upload
Content-Type: multipart/form-data
Authorization: Bearer {token}
```

**Form Data:**
```
file: document.pdf (required)
source_type: FAQ (required) - Options: FAQ, HELP_DOC, POLICY, SOP, OTHER
language: en (optional, default: en)
metadata: {"author": "John", "category": "billing"} (optional JSON)
```

**Response:**
```json
{
  "job_id": "job-uuid",
  "source_id": "source-uuid",
  "source_name": "document.pdf",
  "status": "QUEUED",
  "message": "Document processing started. Connect to WebSocket for progress updates."
}
```

**Processing Steps:**
1. Document uploaded to storage
2. Text extracted
3. Text chunked (300-500 tokens)
4. Embeddings generated (may take 5-30 minutes)
5. Vectors stored in Qdrant
6. Source marked as ACTIVE

**Track Progress:**
```javascript
const ws = new WebSocket('wss://api.luranai.com/api/v1/ws/jobs/{job_id}');
ws.onmessage = (event) => {
  const progress = JSON.parse(event.data);
  console.log(`${progress.processed_items}/${progress.total_items} chunks processed`);
};
```

---

#### 2. List Knowledge Sources

```bash
GET /knowledge/tenants/{tenant_id}/sources
Authorization: Bearer {token}
```

**Response:**
```json
{
  "sources": [
    {
      "id": "source-uuid",
      "tenant_id": "tenant-uuid",
      "name": "FAQ.pdf",
      "source_type": "FAQ",
      "status": "ACTIVE",
      "chunk_count": 45,
      "file_url": "https://storage.../faq.pdf",
      "uploaded_by": "user-uuid",
      "created_at": "2026-01-15T10:00:00Z"
    }
  ],
  "total": 1
}
```

---

#### 3. Get Source Details

```bash
GET /knowledge/sources/{source_id}
Authorization: Bearer {token}
```

**Response:**
```json
{
  "id": "source-uuid",
  "tenant_id": "tenant-uuid",
  "name": "FAQ.pdf",
  "source_type": "FAQ",
  "status": "ACTIVE",
  "chunk_count": 45,
  "file_url": "https://storage.../faq.pdf",
  "meta": {
    "language": "en",
    "author": "John",
    "category": "billing"
  },
  "chunks": [
    {
      "id": "chunk-uuid-1",
      "content": "To file a claim, first log into your account...",
      "chunk_index": 0,
      "is_active": true
    },
    {
      "id": "chunk-uuid-2",
      "content": "Our business hours are Monday-Friday, 9 AM to 5 PM EST...",
      "chunk_index": 1,
      "is_active": true
    }
  ]
}
```

---

#### 4. Delete Knowledge Source

```bash
DELETE /knowledge/sources/{source_id}
Authorization: Bearer {token}
```

**Response:**
```json
{
  "message": "Knowledge source and 45 chunks deleted successfully"
}
```

⚠️ **This also deletes all embeddings from Qdrant.**

---

### Chat & Conversations

#### 1. Create Conversation Session

```bash
POST /chat/sessions
```

**Request:**
```json
{
  "tenant_id": "tenant-uuid",
  "user_identifier": "customer@example.com",
  "metadata": {
    "source": "web_widget",
    "page_url": "https://example.com/support",
    "user_agent": "Mozilla/5.0...",
    "ip_address": "192.168.1.1"
  }
}
```

**Response:**
```json
{
  "id": "session-uuid",
  "tenant_id": "tenant-uuid",
  "user_identifier": "customer@example.com",
  "status": "ACTIVE",
  "meta": {
    "source": "web_widget",
    "page_url": "https://example.com/support"
  },
  "last_message_at": "2026-01-19T10:00:00Z",
  "created_at": "2026-01-19T10:00:00Z"
}
```

---

#### 2. Send Message to AI

```bash
POST /chat/message
```

**Request:**
```json
{
  "message": "How do I file a claim?",
  "session_id": null,
  "tenant_id": "tenant-uuid",
  "user_identifier": "customer@example.com",
  "metadata": {
    "source": "web_widget"
  }
}
```

**Response:**
```json
{
  "session_id": "session-uuid",
  "message": {
    "id": "message-uuid",
    "session_id": "session-uuid",
    "role": "ASSISTANT",
    "content": "To file a claim, first log into your account at...",
    "confidence_score": 0.73,
    "token_count": 156,
    "retrieved_chunk_ids": ["chunk-1", "chunk-2"],
    "meta": {
      "chunks_used": 2,
      "similarity_scores": [0.82, 0.75]
    },
    "created_at": "2026-01-19T10:01:00Z"
  },
  "should_escalate": false,
  "escalation_reason": null,
  "ticket_id": null
}
```

**If Escalated:**
```json
{
  "session_id": "session-uuid",
  "message": {
    "id": "message-uuid",
    "content": "I don't have information about that...",
    "confidence_score": 0.0,
    ...
  },
  "should_escalate": true,
  "escalation_reason": "Low confidence score: 0.00",
  "ticket_id": "ticket-uuid"
}
```

---

#### 3. Agent Takes Over Conversation

```bash
POST /chat/sessions/{session_id}/takeover
Authorization: Bearer {token}
```

**Response:**
```json
{
  "id": "session-uuid",
  "tenant_id": "tenant-uuid",
  "user_identifier": "customer@example.com",
  "status": "ESCALATED",
  "agent_id": "agent-uuid",
  "agent_joined_at": "2026-01-19T10:05:00Z"
}
```

**What Happens:**
1. Session marked as ESCALATED
2. Agent assigned to session
3. Associated ticket assigned to agent
4. System message added to conversation
5. Customer notified via WebSocket (if connected)

---

#### 4. Send Agent Message (REST)

```bash
POST /chat/sessions/{session_id}/agent-message
Authorization: Bearer {token}
```

**Request:**
```json
{
  "message": "Hi! I'm here to help. Can you provide more details?"
}
```

**Response:**
```json
{
  "id": "message-uuid",
  "session_id": "session-uuid",
  "role": "HUMAN_AGENT",
  "content": "Hi! I'm here to help. Can you provide more details?",
  "meta": {
    "agent_id": "agent-uuid",
    "agent_name": "Jane Smith"
  },
  "created_at": "2026-01-19T10:06:00Z"
}
```

**Broadcasted via WebSocket to customer.**

---

#### 5. List Tenant Sessions

```bash
GET /chat/tenants/{tenant_id}/sessions?status=ACTIVE&limit=50
Authorization: Bearer {token}
```

**Query Parameters:**
- `status` - Filter by status: ACTIVE, ESCALATED, RESOLVED, ABANDONED
- `limit` - Max sessions to return (1-500, default: 100)

**Response:**
```json
{
  "sessions": [
    {
      "id": "session-uuid-1",
      "user_identifier": "customer1@example.com",
      "status": "ACTIVE",
      "last_message_at": "2026-01-19T10:00:00Z"
    },
    {
      "id": "session-uuid-2",
      "user_identifier": "customer2@example.com",
      "status": "ESCALATED",
      "agent_id": "agent-uuid",
      "last_message_at": "2026-01-19T09:45:00Z"
    }
  ],
  "total": 2
}
```

---

#### 6. Get Session with Messages

```bash
GET /chat/sessions/{session_id}
Authorization: Bearer {token}
```

**Response:**
```json
{
  "id": "session-uuid",
  "tenant_id": "tenant-uuid",
  "user_identifier": "customer@example.com",
  "status": "ESCALATED",
  "agent_id": "agent-uuid",
  "agent_joined_at": "2026-01-19T10:05:00Z",
  "messages": [
    {
      "id": "msg-1",
      "role": "USER",
      "content": "How do I file a claim?",
      "created_at": "2026-01-19T10:00:00Z"
    },
    {
      "id": "msg-2",
      "role": "ASSISTANT",
      "content": "To file a claim...",
      "confidence_score": 0.73,
      "created_at": "2026-01-19T10:01:00Z"
    },
    {
      "id": "msg-3",
      "role": "USER",
      "content": "What about urgent cases?",
      "created_at": "2026-01-19T10:02:00Z"
    },
    {
      "id": "msg-4",
      "role": "SYSTEM",
      "content": "Jane Smith joined the conversation",
      "created_at": "2026-01-19T10:05:00Z"
    },
    {
      "id": "msg-5",
      "role": "HUMAN_AGENT",
      "content": "Hi! I'm here to help...",
      "meta": {"agent_name": "Jane Smith"},
      "created_at": "2026-01-19T10:06:00Z"
    }
  ]
}
```

---

### Support Tickets

#### 1. List Tickets

```bash
GET /tickets?status=OPEN&limit=50
Authorization: Bearer {token}
```

**Query Parameters:**
- `status` - Filter: OPEN, IN_PROGRESS, RESOLVED, CLOSED
- `assigned_to_me` - Show only my tickets: `true`/`false`
- `limit` - Max tickets (1-500, default: 100)
- `offset` - Pagination offset (default: 0)

**Response:**
```json
{
  "tickets": [
    {
      "id": "ticket-uuid",
      "tenant_id": "tenant-uuid",
      "session_id": "session-uuid",
      "ticket_number": "TKT-2026-0001",
      "status": "OPEN",
      "priority": "HIGH",
      "summary": "Customer unable to access billing dashboard",
      "assigned_to": null,
      "assigned_agent": null,
      "created_at": "2026-01-19T10:00:00Z"
    }
  ],
  "total": 1,
  "page": 1,
  "page_size": 100
}
```

---

#### 2. Get Ticket Details

```bash
GET /tickets/{ticket_id}
Authorization: Bearer {token}
```

**Response:**
```json
{
  "id": "ticket-uuid",
  "tenant_id": "tenant-uuid",
  "session_id": "session-uuid",
  "ticket_number": "TKT-2026-0001",
  "status": "IN_PROGRESS",
  "priority": "HIGH",
  "summary": "Customer unable to access billing dashboard - 403 error",
  "assigned_to": "agent-uuid",
  "assigned_agent": {
    "id": "agent-uuid",
    "email": "jane@example.com",
    "full_name": "Jane Smith",
    "role": "SUPPORT_AGENT"
  },
  "resolution_notes": null,
  "resolved_at": null,
  "created_at": "2026-01-19T10:00:00Z",
  "updated_at": "2026-01-19T10:05:00Z"
}
```

---

#### 3. Create Manual Ticket

```bash
POST /tickets
Authorization: Bearer {token}
```

**Request:**
```json
{
  "session_id": "session-uuid",
  "summary": "Customer reports billing issue",
  "priority": "MEDIUM"
}
```

**Response:**
```json
{
  "id": "ticket-uuid",
  "ticket_number": "TKT-2026-0042",
  "status": "OPEN",
  "priority": "MEDIUM",
  "summary": "Customer reports billing issue"
}
```

---

#### 4. Assign Ticket

```bash
POST /tickets/{ticket_id}/assign
Authorization: Bearer {token}
```

**Request:**
```json
{
  "assigned_to": "agent-uuid"
}
```

**Response:**
```json
{
  "id": "ticket-uuid",
  "ticket_number": "TKT-2026-0042",
  "status": "IN_PROGRESS",
  "assigned_to": "agent-uuid",
  "assigned_agent": {
    "id": "agent-uuid",
    "full_name": "Jane Smith"
  }
}
```

---

#### 5. Resolve Ticket

```bash
POST /tickets/{ticket_id}/resolve
Authorization: Bearer {token}
```

**Request:**
```json
{
  "resolution_notes": "Issue resolved. Customer can now access billing dashboard after permission fix."
}
```

**Response:**
```json
{
  "id": "ticket-uuid",
  "ticket_number": "TKT-2026-0042",
  "status": "RESOLVED",
  "resolution_notes": "Issue resolved. Customer can now...",
  "resolved_at": "2026-01-19T11:00:00Z"
}
```

**What Happens:**
1. Ticket marked as RESOLVED
2. Associated conversation session marked as RESOLVED
3. Resolution timestamp recorded

---

#### 6. Close Ticket

```bash
POST /tickets/{ticket_id}/close
Authorization: Bearer {token}
```

**Response:**
```json
{
  "id": "ticket-uuid",
  "ticket_number": "TKT-2026-0042",
  "status": "CLOSED"
}
```

---

#### 7. Get Unassigned Tickets

```bash
GET /tickets/unassigned
Authorization: Bearer {token}
```

**Response:**
```json
{
  "tickets": [
    {
      "id": "ticket-uuid-1",
      "ticket_number": "TKT-2026-0045",
      "status": "OPEN",
      "priority": "URGENT",
      "summary": "System down - urgent",
      "created_at": "2026-01-19T12:00:00Z"
    },
    {
      "id": "ticket-uuid-2",
      "ticket_number": "TKT-2026-0043",
      "status": "OPEN",
      "priority": "HIGH",
      "summary": "Customer query about refund",
      "created_at": "2026-01-19T11:30:00Z"
    }
  ]
}
```

---

### Analytics

*Requires `can_access_analytics` permission*

#### 1. Overview Dashboard

```bash
GET /analytics/overview?start_date=2026-01-01&end_date=2026-01-31
Authorization: Bearer {token}
```

**Query Parameters:**
- `start_date` - ISO 8601 date (optional, defaults to 30 days ago)
- `end_date` - ISO 8601 date (optional, defaults to now)

**Response:**
```json
{
  "tenant_id": "tenant-uuid",
  "period_start": "2026-01-01T00:00:00Z",
  "period_end": "2026-01-31T23:59:59Z",

  "total_conversations": 1247,
  "active_conversations": 23,
  "escalated_conversations": 189,
  "resolved_conversations": 1035,
  "abandoned_conversations": 0,
  "escalation_rate": 15.2,
  "avg_confidence_score": 0.68,

  "total_tickets": 189,
  "open_tickets": 12,
  "in_progress_tickets": 8,
  "resolved_tickets": 169,
  "closed_tickets": 0,
  "unassigned_tickets": 4,
  "avg_resolution_time_hours": 4.5,

  "total_agents": 5,
  "active_agents": 3
}
```

---

#### 2. Conversation Analytics

```bash
GET /analytics/conversations?start_date=2026-01-01&end_date=2026-01-31
Authorization: Bearer {token}
```

**Response:**
```json
{
  "tenant_id": "tenant-uuid",
  "period_start": "2026-01-01T00:00:00Z",
  "period_end": "2026-01-31T23:59:59Z",

  "total_conversations": 1247,
  "conversations_by_status": {
    "ACTIVE": 23,
    "ESCALATED": 189,
    "RESOLVED": 1035,
    "ABANDONED": 0
  },
  "escalation_rate": 15.2,
  "avg_session_duration_minutes": 8.5,
  "avg_messages_per_session": 6.2,
  "avg_confidence_score": 0.68,
  "confidence_distribution": {
    "0-0.3": 89,
    "0.3-0.5": 156,
    "0.5-0.7": 423,
    "0.7-1.0": 579
  },
  "top_escalation_reasons": [
    {"reason": "Low confidence score", "count": 89},
    {"reason": "Escalation keyword detected", "count": 56},
    {"reason": "No knowledge found", "count": 44}
  ]
}
```

---

#### 3. Ticket Analytics

```bash
GET /analytics/tickets?start_date=2026-01-01&end_date=2026-01-31
Authorization: Bearer {token}
```

**Response:**
```json
{
  "tenant_id": "tenant-uuid",
  "period_start": "2026-01-01T00:00:00Z",
  "period_end": "2026-01-31T23:59:59Z",

  "total_tickets": 189,
  "tickets_by_status": {
    "OPEN": 12,
    "IN_PROGRESS": 8,
    "RESOLVED": 169,
    "CLOSED": 0
  },
  "tickets_by_priority": {
    "LOW": 45,
    "MEDIUM": 89,
    "HIGH": 42,
    "URGENT": 13
  },
  "avg_resolution_time_hours": 4.5,
  "resolution_time_by_priority": {
    "LOW": 8.2,
    "MEDIUM": 5.1,
    "HIGH": 2.8,
    "URGENT": 1.2
  },
  "unassigned_tickets": 4,
  "tickets_created": 189,
  "tickets_resolved": 169
}
```

---

#### 4. Agent Performance

```bash
GET /analytics/agents/{agent_id}?start_date=2026-01-01&end_date=2026-01-31
Authorization: Bearer {token}
```

**Permissions:**
- Agents can view their own metrics
- TENANT_ADMIN can view any agent's metrics

**Response:**
```json
{
  "agent_id": "agent-uuid",
  "agent_name": "Jane Smith",
  "period_start": "2026-01-01T00:00:00Z",
  "period_end": "2026-01-31T23:59:59Z",

  "total_tickets_assigned": 45,
  "tickets_resolved": 42,
  "tickets_in_progress": 3,
  "avg_resolution_time_hours": 3.2,
  "conversations_handled": 45,
  "avg_response_time_minutes": 2.5
}
```

---

#### 5. All Agents Performance

```bash
GET /analytics/agents?start_date=2026-01-01&end_date=2026-01-31
Authorization: Bearer {token}
```

*TENANT_ADMIN only*

**Response:**
```json
{
  "agents": [
    {
      "agent_id": "agent-1",
      "agent_name": "Jane Smith",
      "email": "jane@example.com",
      "current_assigned_tickets": 3,
      "tickets_by_status": {
        "IN_PROGRESS": 3
      },
      "total_tickets_resolved": 42
    },
    {
      "agent_id": "agent-2",
      "agent_name": "John Doe",
      "email": "john@example.com",
      "current_assigned_tickets": 5,
      "tickets_by_status": {
        "OPEN": 2,
        "IN_PROGRESS": 3
      },
      "total_tickets_resolved": 38
    }
  ]
}
```

---

#### 6. Conversation Trends

```bash
GET /analytics/trends/conversations?start_date=2026-01-01&end_date=2026-01-31&granularity=daily
Authorization: Bearer {token}
```

**Query Parameters:**
- `granularity` - `daily`, `weekly`, or `monthly`

**Response:**
```json
{
  "tenant_id": "tenant-uuid",
  "metric_name": "conversation_volume",
  "granularity": "daily",
  "period_start": "2026-01-01T00:00:00Z",
  "period_end": "2026-01-31T23:59:59Z",
  "data_points": [
    {
      "timestamp": "2026-01-01T00:00:00Z",
      "value": 42,
      "meta": {"escalated": 8}
    },
    {
      "timestamp": "2026-01-02T00:00:00Z",
      "value": 38,
      "meta": {"escalated": 5}
    },
    {
      "timestamp": "2026-01-03T00:00:00Z",
      "value": 45,
      "meta": {"escalated": 7}
    }
  ]
}
```

---

#### 7. Knowledge Usage Statistics

```bash
GET /analytics/knowledge?start_date=2026-01-01&end_date=2026-01-31
Authorization: Bearer {token}
```

**Response:**
```json
{
  "tenant_id": "tenant-uuid",
  "period_start": "2026-01-01T00:00:00Z",
  "period_end": "2026-01-31T23:59:59Z",

  "total_knowledge_chunks": 245,
  "chunks_used_in_conversations": 189,
  "usage_rate": 77.1,
  "top_used_chunks": [
    {
      "chunk_id": "chunk-1",
      "source_id": "source-uuid",
      "usage_count": 156,
      "content_preview": "To file a claim, first log into..."
    },
    {
      "chunk_id": "chunk-2",
      "source_id": "source-uuid",
      "usage_count": 98,
      "content_preview": "Our business hours are Monday..."
    }
  ],
  "avg_chunks_per_conversation": 2.3,
  "knowledge_effectiveness_score": 0.82
}
```

---

### Widget Configuration

#### 1. Get Widget Config (Admin)

```bash
GET /widget/config
Authorization: Bearer {token}
```

*TENANT_ADMIN only*

**Response:**
```json
{
  "tenant_id": "tenant-uuid",
  "primary_color": "#2563eb",
  "accent_color": "#10b981",
  "logo_url": "https://cdn.example.com/logo.png",
  "greeting_message": "Hi! How can we help you today?",
  "position": "bottom-right",
  "show_unread_badge": true,
  "allow_file_upload": false
}
```

---

#### 2. Update Widget Config

```bash
PATCH /widget/config
Authorization: Bearer {token}
```

*TENANT_ADMIN only*

**Request:**
```json
{
  "primary_color": "#3b82f6",
  "greeting_message": "👋 Welcome to Acme Support!",
  "position": "bottom-left",
  "allow_file_upload": true
}
```

**Response:**
```json
{
  "tenant_id": "tenant-uuid",
  "primary_color": "#3b82f6",
  "accent_color": "#10b981",
  "greeting_message": "👋 Welcome to Acme Support!",
  "position": "bottom-left",
  "allow_file_upload": true
}
```

---

#### 3. Get Installation Code

```bash
GET /widget/install-code
Authorization: Bearer {token}
```

*TENANT_ADMIN only*

**Response:**
```json
{
  "html_snippet": "<script>window.luranSettings = {tenantId: 'tenant-uuid', apiKey: 'pk_live_...'};</script><script src=\"https://cdn.luranai.com/widget/v1/widget.js\" async defer></script>",
  "instructions": "1. Copy the code snippet\n2. Paste before closing </body> tag\n3. Save and publish\n4. Visit your website\n5. Click the floating chat button"
}
```

---

#### 4. Get Public Widget Config

```bash
GET /widget/public/config/{tenant_id}
X-API-Key: pk_live_abcdefghijk123456789
```

**No authorization token required - uses API key instead**

**Response:**
```json
{
  "tenant_id": "tenant-uuid",
  "company_name": "Acme Corporation",
  "primary_color": "#3b82f6",
  "accent_color": "#10b981",
  "logo_url": "https://cdn.example.com/logo.png",
  "greeting_message": "👋 Welcome to Acme Support!",
  "position": "bottom-left",
  "show_unread_badge": true,
  "allow_file_upload": true
}
```

**Used by widget.js to fetch configuration and styling.**

---

## Widget Integration

### Installation

Add this code snippet before closing `</body>` tag:

```html
<script>
  window.luranSettings = {
    tenantId: '550e8400-e29b-41d4-a716-446655440000',
    apiKey: 'pk_live_abcdefghijk123456789'
  };
</script>
<script src="https://cdn.luranai.com/widget/v1/widget.js" async defer></script>
```

### Configuration Options

```javascript
window.luranSettings = {
  // Required
  tenantId: 'your-tenant-uuid',
  apiKey: 'pk_live_...',

  // Optional overrides
  apiUrl: 'https://api.luranai.com/api/v1',
  chatUrl: 'https://chat.luranai.com/widget',

  // Custom user identifier (optional)
  userIdentifier: 'customer@example.com',

  // Custom metadata (optional)
  metadata: {
    userId: '12345',
    plan: 'premium',
    page: 'pricing'
  }
};
```

### Widget Features

- ✨ Floating chat button (customizable position and colors)
- 💬 Real-time messaging with AI
- 👤 Seamless agent takeover
- 🔔 Unread message badge
- 📱 Mobile responsive
- 🎨 Customizable branding
- 💾 Conversation persistence

### Widget API (JavaScript)

```javascript
// Open chat programmatically
LuranWidget.open();

// Close chat
LuranWidget.close();

// Toggle chat
LuranWidget.toggle();

// Get configuration
const config = LuranWidget.getConfig();

// Check if open
const isOpen = LuranWidget.isOpen();

// Get version
console.log(LuranWidget.version);
```

### Events

```javascript
// Chat opened
window.addEventListener('luran:chat_opened', (event) => {
  console.log('Chat opened', event.detail);
});

// Chat closed
window.addEventListener('luran:chat_closed', (event) => {
  console.log('Chat closed', event.detail);
});

// Message sent
window.addEventListener('luran:message_sent', (event) => {
  console.log('Message sent', event.detail);
});

// Agent joined
window.addEventListener('luran:agent_joined', (event) => {
  console.log('Agent joined', event.detail);
});
```

---

## Chat System

### Message Flow

```
1. Customer types message in widget
2. Widget calls POST /chat/message
3. Backend:
   a. Embed user query (Google Gemini)
   b. Search knowledge base (Qdrant)
   c. Calculate confidence score
   d. Check escalation conditions
   e. Generate AI response (Google Gemma)
   f. Save message to database
   g. Create ticket if escalated
   h. Send email to admins/agents if escalated
4. Backend returns response
5. Widget displays AI message
6. If escalated, widget shows escalation notice
```

### Escalation Triggers

AI conversation escalates to human agent when:

1. **Low Confidence** - AI confidence score < 0.55 (configurable)
2. **No Knowledge Found** - No relevant knowledge chunks retrieved
3. **Escalation Keywords** - User says "talk to human", "speak to manager", etc.
4. **Blocked Topics** - User asks about restricted topics

### Agent Takeover

After escalation:

1. **Ticket Created** - Auto-generated with priority based on keywords
2. **Email Sent** - Admins and agents notified
3. **Agent Reviews** - Views conversation history
4. **Agent Takes Over** - Calls POST `/chat/sessions/{session_id}/takeover`
5. **Real-Time Chat** - Agent connects via WebSocket
6. **Customer Notified** - "Agent has joined" message
7. **Live Conversation** - Agent and customer chat in real-time

### Message Roles

| Role | Who | Description |
|------|-----|-------------|
| **USER** | Customer | Customer message |
| **ASSISTANT** | AI | AI-generated response |
| **HUMAN_AGENT** | Agent | Agent message after takeover |
| **SYSTEM** | System | Join/leave notifications, status updates |

---

## Knowledge Base

### Supported Document Formats

- ✅ PDF (.pdf)
- ✅ Text (.txt)
- ✅ Markdown (.md)
- ✅ HTML (.html)
- ✅ Word (.docx)

### Processing Pipeline

**1. Upload** (Instant)
- Document uploaded to storage
- Processing job queued

**2. Text Extraction** (1-5 seconds)
- Extract text from document
- Preserve structure and formatting

**3. Chunking** (5-10 seconds)
- Split into 300-500 token chunks
- 50-100 token overlap for context continuity
- Preserve sentence boundaries

**4. Embedding Generation** (5-30 minutes)
- Generate vector embeddings via Google Gemini
- 3072 dimensions per chunk
- Rate limited: 90 embeddings/minute
- Circuit breaker at 95% daily quota

**5. Vector Storage** (1-2 seconds)
- Store in Qdrant vector database
- Tenant-isolated collections
- HNSW indexing for fast search

**6. Activation** (Instant)
- Source marked as ACTIVE
- Chunks available for retrieval
- Email notification sent

### Best Practices

#### Document Preparation

✅ **DO:**
- Write in clear, simple language
- Structure with headers and sections
- Include common questions and answers
- Use consistent terminology
- Add examples and use cases
- Keep documents focused (one topic per doc)

❌ **DON'T:**
- Upload scanned images (extract text first)
- Include irrelevant content
- Use unclear abbreviations
- Mix multiple unrelated topics
- Forget to update when info changes

#### Chunk Optimization

**Good Chunk Example:**
```
To file a claim, follow these steps:

1. Log into your account at https://example.com/login
2. Navigate to "Claims" in the dashboard
3. Click "File New Claim"
4. Fill in the required information:
   - Date of incident
   - Description of damage
   - Photos (optional)
5. Submit the form

You'll receive a confirmation email within 24 hours.
Our team reviews claims within 3-5 business days.
```

**Why Good:**
- Complete information (300-400 tokens)
- Clear step-by-step instructions
- Includes context
- Self-contained

**Bad Chunk Example:**
```
Click the button.
```

**Why Bad:**
- Too short (no context)
- Ambiguous (which button?)
- Not self-contained

---

## Support Tickets

### Ticket Lifecycle

```
OPEN → IN_PROGRESS → RESOLVED → CLOSED
  ↓         ↓
  └─────────┴─────→ (Can be reassigned)
```

### Ticket Statuses

| Status | Description | Who Can Set |
|--------|-------------|-------------|
| **OPEN** | Newly created, unassigned | System, Admin |
| **IN_PROGRESS** | Agent working on it | Agent, Admin |
| **RESOLVED** | Issue solved | Agent, Admin |
| **CLOSED** | Ticket archived | Admin |

### Ticket Priorities

| Priority | Response Time | Use When |
|----------|--------------|----------|
| **URGENT** | < 1 hour | System down, critical issue, keywords: "urgent", "emergency" |
| **HIGH** | < 4 hours | Important issue, escalation keyword detected |
| **MEDIUM** | < 24 hours | Standard issue, moderate urgency |
| **LOW** | < 48 hours | Low-priority question, minor issue |

### Auto-Assignment

Tickets are automatically prioritized based on:

1. **Keywords** - "urgent", "emergency", "broken", "critical"
2. **Confidence Score** - Lower score = higher priority
3. **Escalation Reason** - Explicit request = high priority

### Auto-Resolution

Stale tickets (7+ days no activity) auto-resolve with:

- **Daily Cron Job** - Runs at 2 AM UTC
- **Email Notification** - Admins notified
- **Status Update** - OPEN/IN_PROGRESS → RESOLVED
- **Resolution Notes** - "Auto-resolved due to inactivity"

To prevent auto-resolution:
- Update ticket
- Add comment
- Change status

---

## Analytics

### Metrics Explained

#### Escalation Rate

```
Escalation Rate = (Escalated Conversations / Total Conversations) × 100
```

**Good:** < 20%
**Acceptable:** 20-30%
**Needs Improvement:** > 30%

**How to Improve:**
- Add more knowledge base content
- Update outdated information
- Train AI with better custom instructions
- Review escalation keywords (remove too-sensitive triggers)

---

#### Confidence Score

```
Confidence Score = weighted_average(chunk_similarity_scores)
```

**Range:** 0.0 - 1.0

**Interpretation:**
- **0.9-1.0** - Very high confidence (AI very certain)
- **0.7-0.9** - High confidence (AI confident)
- **0.5-0.7** - Medium confidence (AI somewhat confident)
- **0.3-0.5** - Low confidence (AI unsure)
- **0.0-0.3** - Very low confidence (AI has no idea)

**Default Threshold:** 0.55 (escalates below this)

---

#### Average Resolution Time

```
Avg Resolution Time = SUM(resolved_at - created_at) / COUNT(resolved_tickets)
```

**Good:** < 4 hours
**Acceptable:** 4-24 hours
**Needs Improvement:** > 24 hours

**Factors Affecting:**
- Agent availability
- Ticket complexity
- Workload distribution
- Time of day

---

#### Knowledge Effectiveness Score

```
Effectiveness = (Resolved with Knowledge / Total with Knowledge) × 100
```

**Good:** > 80%
**Acceptable:** 60-80%
**Needs Improvement:** < 60%

**Measures:**
- How often knowledge base helps resolve issues
- Quality of knowledge content
- Relevance of retrieved chunks

---

## WebSocket Integration

### Customer WebSocket

**Endpoint:** `wss://api.luranai.com/api/v1/ws/chat/{session_id}`

**Authentication:** Query parameter `user_identifier`

**Example:**
```javascript
const ws = new WebSocket(
  `wss://api.luranai.com/api/v1/ws/chat/${sessionId}?user_identifier=${userId}`
);

ws.onopen = () => {
  console.log('Connected');
};

ws.onmessage = (event) => {
  const message = JSON.parse(event.data);

  switch (message.type) {
    case 'agent_joined':
      console.log(`${message.agent_name} joined`);
      break;

    case 'agent_message':
      console.log(`Agent: ${message.content}`);
      break;

    case 'agent_left':
      console.log(`${message.agent_name} left`);
      break;

    case 'typing':
      console.log(`${message.agent_name} is typing...`);
      break;
  }
};

// Send customer message
ws.send(JSON.stringify({
  type: 'message',
  content: 'Can you help me with this?'
}));

// Send typing indicator
ws.send(JSON.stringify({
  type: 'typing',
  is_typing: true
}));
```

---

### Agent WebSocket

**Endpoint:** `wss://api.luranai.com/api/v1/ws/agent/{session_id}`

**Authentication:** Query parameter `token` (JWT)

**Example:**
```javascript
const ws = new WebSocket(
  `wss://api.luranai.com/api/v1/ws/agent/${sessionId}?token=${jwtToken}`
);

ws.onmessage = (event) => {
  const message = JSON.parse(event.data);

  switch (message.type) {
    case 'customer_message':
      console.log(`Customer: ${message.content}`);
      break;

    case 'agent_message':
      console.log(`Another agent: ${message.content}`);
      break;

    case 'customer_typing':
      console.log('Customer is typing...');
      break;
  }
};

// Send agent message
ws.send(JSON.stringify({
  type: 'message',
  content: 'I can help you with that!'
}));

// Send typing indicator
ws.send(JSON.stringify({
  type: 'typing',
  is_typing: true
}));
```

---

### Message Types

#### From System to Client

| Type | Who Receives | Data |
|------|-------------|------|
| `agent_joined` | Customer | `{agent_name, agent_id, timestamp}` |
| `agent_left` | Customer | `{agent_name, agent_id, timestamp}` |
| `agent_message` | Customer, Other Agents | `{message_id, content, agent_name, timestamp}` |
| `customer_message` | Agents | `{message_id, content, timestamp}` |
| `typing` | Customer (from agent) | `{agent_name, is_typing}` |
| `customer_typing` | Agents | `{is_typing}` |

#### From Client to System

| Type | Who Sends | Data |
|------|-----------|------|
| `message` | Customer, Agent | `{content}` |
| `typing` | Customer, Agent | `{is_typing}` |

---

## Rate Limits & Quotas

### API Rate Limits

| Endpoint | Limit | Window |
|----------|-------|--------|
| **Chat Message** | Unlimited* | N/A |
| **Knowledge Upload** | 10 documents | 1 hour |
| **Analytics** | 60 requests | 1 minute |
| **Widget Config** | 100 requests | 1 minute |
| **Authentication** | 5 attempts | 15 minutes |

*Limited by AI model quotas (see below)

---

### AI Model Quotas

#### Google Embedding API

**Model:** `gemini-embedding-001`

| Quota Type | Limit | Enforced At |
|------------|-------|-------------|
| Per Minute | 90 RPM | 90% of 100 RPM |
| Per Day | 1,000 RPD | Hard limit |
| Circuit Breaker | 950 requests | 95% of daily |

**What happens when limit reached:**
1. Requests queue automatically
2. System waits for quota reset
3. Circuit breaker trips at 95% (950 requests)
4. Warning at 80% (800 requests)

---

#### Google Gemma API

**Model:** `gemma-3-12b-it`

| Quota Type | Limit | Enforced At |
|------------|-------|-------------|
| Per Minute | 27 RPM | 90% of 30 RPM |
| Per Day | 14,400 RPD | Hard limit |
| Circuit Breaker | 13,680 requests | 95% of daily |

**Typical Response Time:** 60-70 seconds

---

### Daily Conversation Capacity

**Bottleneck:** Embedding API (1,000 RPD)

**Calculation:**
- Each user message = 1 embedding + 1 generation
- Average conversation = 10 messages
- Daily capacity = 1,000 / 10 = **~100 conversations/day**

**Upgrading Quota:**
- Contact Google AI Studio for higher quotas
- Or use paid Vertex AI

---

### Rate Limit Headers

**Response Headers:**
```
X-RateLimit-Limit: 60
X-RateLimit-Remaining: 45
X-RateLimit-Reset: 1737302400
```

**429 Too Many Requests Response:**
```json
{
  "detail": "Rate limit exceeded. Please try again in 60 seconds.",
  "retry_after": 60
}
```

---

## Security

### Authentication Security

- ✅ **bcrypt** password hashing (cost factor 12)
- ✅ **JWT** tokens with HS256 signing
- ✅ **7-day** token expiration
- ✅ **API keys** hashed with bcrypt
- ✅ **Rate limiting** on login attempts

---

### Data Security

- ✅ **Tenant Isolation** - Strict tenant_id filtering on all queries
- ✅ **RBAC** - Role-based access control
- ✅ **SQL Injection Prevention** - SQLAlchemy ORM
- ✅ **XSS Protection** - Pydantic validation
- ✅ **CORS** - Configured for specific origins
- ✅ **HTTPS Only** - TLS 1.2+ required

---

### API Key Security

**Public Keys (`pk_live_...`):**
- ✅ Safe to embed in websites
- ✅ Read-only access to widget config
- ✅ Cannot modify data
- ✅ Rate limited per key

**Secret Keys (`sk_live_...`):**
- ❌ **NEVER** embed in websites
- ❌ **NEVER** commit to Git
- ✅ Server-to-server only
- ✅ Store in environment variables

---

### Best Practices

#### For Developers

✅ **DO:**
- Store tokens securely (httpOnly cookies, secure storage)
- Use environment variables for API keys
- Implement token refresh before expiry
- Log security events
- Use HTTPS everywhere
- Validate all inputs on frontend and backend

❌ **DON'T:**
- Store passwords in plaintext
- Commit API keys to Git
- Use `pk_live_` keys for admin operations
- Skip input validation
- Trust client-side validation alone

---

## Error Handling

### Error Response Format

```json
{
  "detail": "Error message here"
}
```

---

### HTTP Status Codes

| Code | Meaning | Common Causes |
|------|---------|---------------|
| **200 OK** | Success | Request succeeded |
| **201 Created** | Resource created | POST successful |
| **400 Bad Request** | Invalid input | Missing fields, invalid format |
| **401 Unauthorized** | Not authenticated | No token, invalid token |
| **403 Forbidden** | Not authorized | Wrong role, wrong tenant |
| **404 Not Found** | Resource not found | Invalid ID |
| **422 Unprocessable Entity** | Validation error | Pydantic validation failed |
| **429 Too Many Requests** | Rate limited | Too many requests |
| **500 Internal Server Error** | Server error | Unexpected error |

---

### Common Errors

#### 401 Unauthorized

```json
{
  "detail": "Not authenticated"
}
```

**Solution:**
- Include `Authorization: Bearer {token}` header
- Check token is valid and not expired
- Re-login if token expired

---

#### 403 Forbidden

```json
{
  "detail": "User does not have permission to access this resource"
}
```

**Solution:**
- Check user role (may need TENANT_ADMIN)
- Verify accessing own tenant's resources
- Check tenant capabilities (e.g., analytics enabled)

---

#### 404 Not Found

```json
{
  "detail": "Session not found"
}
```

**Solution:**
- Verify resource ID is correct
- Check resource exists
- Verify you have access to the tenant

---

#### 422 Validation Error

```json
{
  "detail": [
    {
      "loc": ["body", "email"],
      "msg": "value is not a valid email address",
      "type": "value_error.email"
    }
  ]
}
```

**Solution:**
- Fix input validation errors
- Check field types match schema
- Ensure required fields provided

---

#### 429 Rate Limit

```json
{
  "detail": "Rate limit exceeded. Please try again in 60 seconds.",
  "retry_after": 60
}
```

**Solution:**
- Wait for `retry_after` seconds
- Implement exponential backoff
- Review if you're making too many requests

---

## Best Practices

### API Integration

#### Rate Limiting

```javascript
// Implement exponential backoff
async function callAPIWithRetry(url, options, maxRetries = 3) {
  for (let i = 0; i < maxRetries; i++) {
    const response = await fetch(url, options);

    if (response.status === 429) {
      const retryAfter = parseInt(response.headers.get('Retry-After')) || 60;
      const backoff = retryAfter * Math.pow(2, i);

      console.log(`Rate limited. Waiting ${backoff}s before retry...`);
      await sleep(backoff * 1000);
      continue;
    }

    return response;
  }

  throw new Error('Max retries exceeded');
}
```

---

#### Error Handling

```javascript
try {
  const response = await fetch('/api/v1/chat/message', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(messageData),
  });

  if (!response.ok) {
    const error = await response.json();

    switch (response.status) {
      case 401:
        // Re-authenticate
        await refreshToken();
        return await callAPIWithRetry(url, options);

      case 429:
        // Rate limited
        showUserMessage('Please wait a moment before trying again');
        break;

      case 500:
        // Server error
        showUserMessage('Something went wrong. Please try again later');
        break;

      default:
        showUserMessage(error.detail);
    }

    return null;
  }

  return await response.json();

} catch (error) {
  console.error('Network error:', error);
  showUserMessage('Connection error. Please check your internet');
  return null;
}
```

---

#### Token Management

```javascript
// Store token securely
function storeToken(token) {
  // Option 1: httpOnly cookie (most secure)
  document.cookie = `token=${token}; Secure; HttpOnly; SameSite=Strict`;

  // Option 2: localStorage (less secure but easier)
  localStorage.setItem('luran_token', token);
}

// Check token expiry
function isTokenExpired(token) {
  const payload = JSON.parse(atob(token.split('.')[1]));
  return payload.exp * 1000 < Date.now();
}

// Refresh before expiry
setInterval(() => {
  const token = localStorage.getItem('luran_token');
  if (token && isTokenExpired(token)) {
    // Re-login
    redirectToLogin();
  }
}, 60000); // Check every minute
```

---

### Knowledge Base Optimization

#### Document Structure

**Good:**
```markdown
# How to File a Claim

## When to File

File a claim within 30 days of an incident...

## Step-by-Step Process

1. Log into your account
2. Navigate to Claims
3. Click "File New Claim"
...

## Required Information

- Date of incident
- Description of damages
- Photos (optional)

## What Happens Next

You'll receive confirmation within 24 hours...
```

**Why Good:**
- Clear headings
- Logical flow
- Complete information
- Self-contained sections

---

#### Update Frequency

- 📅 **Review monthly** - Check for outdated info
- 🔄 **Update immediately** - When processes change
- 🗑️ **Remove obsolete** - Delete old documents
- ✅ **Version control** - Keep track of changes

---

### Agent Workflow

#### Best Practices for Agents

1. **Review Context First**
   - Read full conversation history
   - Check customer metadata
   - Review previous tickets

2. **Take Over Promptly**
   - Respond within 5 minutes of escalation
   - Send greeting message immediately
   - Set status to IN_PROGRESS

3. **Communicate Clearly**
   - Use customer's name if available
   - Be empathetic and friendly
   - Provide clear next steps

4. **Document Resolution**
   - Add detailed resolution notes
   - Include steps taken
   - Note any follow-up needed

5. **Close Properly**
   - Verify customer satisfaction
   - Resolve ticket with notes
   - Close ticket when confirmed resolved

---

## FAQ

### General

**Q: What models does Luran use?**

A:
- **Embedding:** `gemini-embedding-001` (3072 dimensions)
- **Chat:** `gemma-3-12b-it` (12B parameters)
- Both from Google AI Studio (free tier)

---

**Q: Why are AI responses slow (~60 seconds)?**

A:
- Gemma 3 12B is a large model (high quality)
- Free tier has lower priority
- Trade-off: Free but slower

**Solution:** Upgrade to Vertex AI for faster responses (~3-5 seconds)

---

**Q: How many conversations can I have per day?**

A:
- Free tier: ~100 conversations/day (1,000 messages)
- Limited by embedding API quota (1,000 RPD)
- Average conversation = 10 messages

**Solution:** Request higher quota from Google AI Studio

---

### Authentication

**Q: How long do tokens last?**

A: 7 days by default (configurable)

---

**Q: What happens when my token expires?**

A: You'll get `401 Unauthorized`. Re-login to get new token.

---

**Q: Can I refresh tokens without re-login?**

A: Not currently. Future feature planned.

---

### Knowledge Base

**Q: What document formats are supported?**

A: PDF, TXT, MD, HTML, DOCX

---

**Q: How long does document processing take?**

A:
- Upload: Instant
- Processing: 5-30 minutes depending on size
- Track progress via WebSocket

---

**Q: Can I update a document?**

A:
- Delete old document
- Upload new version
- Old embeddings automatically removed

---

**Q: How do I improve AI accuracy?**

A:
1. Add more knowledge base content
2. Use clear, well-structured documents
3. Update outdated information
4. Add common Q&A examples
5. Adjust confidence threshold if needed

---

### Tickets

**Q: When are tickets auto-created?**

A: When conversation escalates due to:
- Low AI confidence
- No knowledge found
- Escalation keywords
- Blocked topics

---

**Q: Can I create tickets manually?**

A: Yes, via `POST /tickets` endpoint

---

**Q: What is auto-resolution?**

A:
- Stale tickets (7+ days no activity) auto-resolve
- Runs daily at 2 AM UTC
- Admins notified via email
- Prevents ticket backlog

---

### Analytics

**Q: When are analytics available?**

A: Immediately after first conversation. Real-time updates.

---

**Q: Can I export analytics data?**

A: Not via API yet. Future feature planned. Contact support for exports.

---

**Q: What is a "good" escalation rate?**

A:
- **Excellent:** < 10%
- **Good:** 10-20%
- **Acceptable:** 20-30%
- **Needs improvement:** > 30%

---

### Widget

**Q: Can I customize widget appearance?**

A: Yes, via widget configuration:
- Primary color
- Accent color
- Logo
- Position
- Greeting message

---

**Q: Does the widget work on mobile?**

A: Yes, fully responsive. Auto-adjusts for mobile screens.

---

**Q: Can I hide the widget on certain pages?**

A: Yes, conditionally load the script:

```javascript
if (window.location.pathname !== '/checkout') {
  // Load widget
}
```

---

### WebSocket

**Q: Do I need WebSocket for AI chat?**

A: No. Use REST API (`POST /chat/message`). WebSocket for agent chat only.

---

**Q: What happens if WebSocket disconnects?**

A: Auto-reconnect with exponential backoff. Messages queued until reconnected.

---

**Q: Can I use WebSocket from mobile apps?**

A: Yes. WebSocket supported on iOS and Android.

---

## Support

### Getting Help

**Documentation:** https://docs.luranai.com

**API Status:** https://status.luranai.com

**Email:** support@luranai.com

**Response Time:** Within 24 hours

---

### Reporting Issues

**Security Issues:** security@luranai.com (DO NOT report publicly)

**Bug Reports:** support@luranai.com

**Feature Requests:** feature-requests@luranai.com

---

### Resources

- **Widget Integration Guide:** `/docs/WIDGET_INTEGRATION_GUIDE.md`
- **Chat Frontend Guide:** `/docs/CHAT_FRONTEND_INTEGRATION_CORRECTED.md`
- **API Reference:** `/docs/FRONTEND_API_DOCUMENTATION.md`
- **WebSocket Guide:** `/docs/WEBSOCKET_INTEGRATION.md`

---

## Changelog

### v1.0.0 (2026-01-19)

**Initial Release:**
- ✨ AI-powered chat with RAG
- 📚 Knowledge base management
- 🎫 Support ticket system
- 💬 WebSocket real-time chat
- 📊 Analytics dashboard
- 🎨 Customizable widget
- 🔐 Multi-tenant architecture
- ⚡ Background job processing
- 📧 Email notifications
- 🔒 JWT authentication
- 🛡️ RBAC authorization

---

**Last Updated:** 2026-01-19
**Documentation Version:** 1.0.0
**API Version:** v1

---

© 2026 Luran AI. All rights reserved.

# Chat Frontend Integration Guide
**Version:** 2.0 (Corrected)
**Last Updated:** 2026-01-19
**Status:** ✅ Verified Against Implementation

---

## Overview

This guide explains how to integrate the Luran chat API with your frontend application to create a natural, user-friendly chat experience.

---

## 1. Initial Greeting Pattern

### ❌ DON'T: Let the AI greet on every message
```
User: "How do I file a claim?"
AI: "Hi there! Thanks for reaching out. To file a claim..."

User: "What about business hours?"
AI: "Hi there! Thanks for reaching out. I don't have..."
```
**Problem:** Repetitive greetings are annoying and waste tokens.

### ✅ DO: Frontend handles the initial greeting
```
[Chat Widget Opens]
System: "👋 Hi there! How can we help you today?"

User: "How do I file a claim?"
AI: "After an accident or covered incident, the first thing to do is..."

User: "What about business hours?"
AI: "I don't have information about our business hours. However..."
```
**Benefit:** Natural conversation flow, no repetition.

---

## 2. API Endpoint

### Chat Message Endpoint

**Endpoint:** `POST /api/v1/chat/message`

**Request:**
```json
{
  "message": "How do I file a claim?",
  "session_id": null,
  "tenant_id": "your-tenant-uuid",
  "user_identifier": "user@example.com",
  "metadata": {
    "source": "web_widget",
    "page_url": "https://example.com/support"
  }
}
```

**Response:**
```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "message": {
    "id": "message-uuid",
    "session_id": "550e8400-e29b-41d4-a716-446655440000",
    "role": "ASSISTANT",
    "content": "After an accident or covered incident, the first step is...",
    "confidence_score": 0.73,
    "token_count": 156,
    "retrieved_chunk_ids": ["chunk-uuid-1", "chunk-uuid-2"],
    "meta": {
      "chunks_used": 2,
      "similarity_scores": [0.82, 0.75]
    },
    "created_at": "2026-01-19T14:30:00Z"
  },
  "should_escalate": false,
  "escalation_reason": null,
  "ticket_id": null
}
```

---

## 3. Frontend Implementation

### When to Show the Greeting

Display the greeting banner when:
- Chat widget first opens
- New conversation starts (no `session_id`)
- User hasn't sent any messages yet

### Example Implementation

```javascript
// React example
function ChatWidget({ tenantId }) {
  const [sessionId, setSessionId] = useState(null);
  const [messages, setMessages] = useState([]);
  const [isLoading, setIsLoading] = useState(false);

  // Show welcome message on mount
  useEffect(() => {
    if (messages.length === 0) {
      setMessages([
        {
          type: 'system',
          content: '👋 Hi there! How can we help you today?',
          timestamp: new Date(),
        }
      ]);
    }
  }, []);

  const sendMessage = async (userMessage) => {
    // Add user message to UI
    setMessages(prev => [...prev, {
      type: 'user',
      content: userMessage,
      timestamp: new Date(),
    }]);

    setIsLoading(true);

    try {
      // Call API
      const response = await fetch('/api/v1/chat/message', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          tenant_id: tenantId,
          session_id: sessionId, // null for first message
          message: userMessage,
          user_identifier: getCurrentUserId(),
          metadata: {
            source: 'web_widget',
            page_url: window.location.href,
            user_agent: navigator.userAgent,
          }
        })
      });

      if (!response.ok) {
        throw new Error(`API error: ${response.status}`);
      }

      const data = await response.json();

      // Save session ID from first response
      if (!sessionId) {
        setSessionId(data.session_id);
        // Store in localStorage for persistence
        localStorage.setItem('luran_session_id', data.session_id);
      }

      // Add AI response to UI
      setMessages(prev => [...prev, {
        type: 'assistant',
        messageId: data.message.id,
        content: data.message.content,
        confidence: data.message.confidence_score,
        tokenCount: data.message.token_count,
        chunksUsed: data.message.meta?.chunks_used || 0,
        shouldEscalate: data.should_escalate,
        escalationReason: data.escalation_reason,
        ticketId: data.ticket_id,
        timestamp: new Date(data.message.created_at),
      }]);

      // If escalated, show notification
      if (data.should_escalate) {
        showEscalationNotice(data.escalation_reason, data.ticket_id);
      }

    } catch (error) {
      console.error('Chat error:', error);
      setMessages(prev => [...prev, {
        type: 'system',
        content: 'Sorry, I\'m having trouble connecting right now. Please try again in a moment.',
        isError: true,
        timestamp: new Date(),
      }]);
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="chat-widget">
      <MessageList messages={messages} />
      {isLoading && <LoadingIndicator text="Getting you an answer (this may take up to 60 seconds)..." />}
      <MessageInput onSend={sendMessage} disabled={isLoading} />
    </div>
  );
}
```

---

## 4. Alternative: WebSocket Integration

For real-time chat with human agents after escalation, use WebSocket:

### Customer WebSocket Connection

```javascript
const ws = new WebSocket(
  `wss://api.luranai.com/ws/chat/${sessionId}?user_identifier=${userId}`
);

ws.onopen = () => {
  console.log('Connected to agent chat');
};

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);

  switch (data.type) {
    case 'agent_joined':
      showAgentJoinedNotice(data.agent_name);
      break;

    case 'agent_message':
      addMessageToUI({
        type: 'agent',
        content: data.content,
        agentName: data.agent_name,
        timestamp: new Date(data.timestamp),
      });
      break;

    case 'agent_left':
      showAgentLeftNotice(data.agent_name);
      break;

    case 'typing':
      showTypingIndicator(data.agent_name);
      break;
  }
};

// Send customer message via WebSocket
function sendViaWebSocket(message) {
  ws.send(JSON.stringify({
    type: 'message',
    content: message,
  }));
}
```

### When to Use REST vs WebSocket

| Scenario | Use | Why |
|----------|-----|-----|
| Initial AI chat | REST API | Simple request/response |
| After escalation | WebSocket | Real-time with agents |
| View history | REST API | One-time data fetch |
| Agent dashboard | WebSocket | Live updates |

---

## 5. API Usage: Why Embedding API is Called During Chat

### Question: "Is the embedding API called during chat?"

**Answer: YES** - The embedding API is called **for every user message**. Here's why:

### The RAG Flow (Per Message)

```
User sends message: "How do I file a claim?"
         │
         ▼
1. EMBEDDING API CALL
   ┌─────────────────────────────────────┐
   │ Convert question to embedding       │
   │ Input: "How do I file a claim?"     │
   │ Output: [0.234, -0.567, ...] (3072) │
   │ Model: gemini-embedding-001         │
   │ Time: ~400ms                        │
   └─────────────┬───────────────────────┘
                 │
                 ▼
2. VECTOR SEARCH (Qdrant)
   ┌─────────────────────────────────────┐
   │ Find similar knowledge chunks        │
   │ Search using the embedding vector    │
   │ Returns: Top 2-5 relevant chunks     │
   │ Time: ~100ms                        │
   └─────────────┬───────────────────────┘
                 │
                 ▼
3. GEMMA API CALL
   ┌─────────────────────────────────────┐
   │ Generate response using:             │
   │ - User question                      │
   │ - Retrieved knowledge chunks         │
   │ - Conversation history               │
   │ - Tenant configuration               │
   │ Model: gemma-3-12b-it               │
   │ Time: ~60-70 seconds (typical)      │
   └─────────────┬───────────────────────┘
                 │
                 ▼
         AI Response sent to user
```

### Why Two API Calls?

1. **Embedding API** - Converts user's question into searchable format
   - Model: `models/gemini-embedding-001`
   - Speed: ~400ms
   - Cost: Free (1,000 requests/day)
   - Purpose: Enable semantic search in knowledge base

2. **Gemma API** - Generates the actual response
   - Model: `models/gemma-3-12b-it`
   - Speed: ~60-70 seconds (typical, varies by load)
   - Cost: Free (14,400 requests/day)
   - Purpose: Create natural language answer

### API Call Counts Per Chat Message

| Action | Embedding API | Gemma API | Total API Calls |
|--------|--------------|-----------|-----------------|
| User sends message | 1 | 1 | 2 |
| 10 message conversation | 10 | 10 | 20 |

### Rate Limits (Actual Enforced Limits)

```python
# From rate_limiter.py
EMBEDDING API:
- 90 RPM (90% of 100 for safety margin)
- 1,000 RPD (requests per day)

GEMMA API:
- 27 RPM (90% of 30 for safety margin)
- 14,400 RPD
```

**Bottleneck:** Embedding API has lower daily quota (1,000 vs 14,400), so that's your limiting factor for daily conversations.

**Safe daily capacity:**
- 1,000 embedding calls = ~1,000 user messages per day
- Average conversation = 10 messages
- **≈100 complete conversations per day** (free tier)

---

## 6. Message Types and UI States

### Message Types

```typescript
type Message = {
  type: 'system' | 'user' | 'assistant' | 'agent';
  content: string;
  timestamp: Date;

  // Only for assistant messages
  messageId?: string;
  confidence?: number;
  tokenCount?: number;
  chunksUsed?: number;
  shouldEscalate?: boolean;
  escalationReason?: string;
  ticketId?: string;

  // Only for agent messages
  agentName?: string;
  agentId?: string;

  // Only for system/error messages
  isError?: boolean;
};
```

### UI State Handling

```javascript
function MessageBubble({ message }) {
  // System greeting or notice
  if (message.type === 'system') {
    return (
      <div className={`system-message ${message.isError ? 'error' : ''}`}>
        {message.content}
      </div>
    );
  }

  // User message
  if (message.type === 'user') {
    return (
      <div className="user-message">
        <div className="message-content">{message.content}</div>
        <div className="message-time">{formatTime(message.timestamp)}</div>
      </div>
    );
  }

  // AI response
  if (message.type === 'assistant') {
    return (
      <div className="ai-message">
        <div className="message-content">{message.content}</div>

        {/* Show escalation notice if needed */}
        {message.shouldEscalate && (
          <div className="escalation-notice">
            <Icon name="alert" />
            <div>
              <strong>Escalated to Support</strong>
              <p>{message.escalationReason}</p>
              {message.ticketId && (
                <p className="ticket-number">Ticket ID: {message.ticketId}</p>
              )}
            </div>
          </div>
        )}

        {/* Optional: Show confidence indicator for debugging */}
        {process.env.NODE_ENV === 'development' && (
          <div className="debug-info">
            Confidence: {(message.confidence * 100).toFixed(1)}%
            | Chunks: {message.chunksUsed}
            | Tokens: {message.tokenCount}
          </div>
        )}

        <div className="message-time">{formatTime(message.timestamp)}</div>
      </div>
    );
  }

  // Human agent message (after escalation)
  if (message.type === 'agent') {
    return (
      <div className="agent-message">
        <div className="agent-badge">{message.agentName}</div>
        <div className="message-content">{message.content}</div>
        <div className="message-time">{formatTime(message.timestamp)}</div>
      </div>
    );
  }
}
```

---

## 7. Error Handling

### API Errors

```javascript
async function sendMessage(userMessage) {
  try {
    const response = await fetch('/api/v1/chat/message', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        tenant_id: tenantId,
        session_id: sessionId,
        message: userMessage,
        user_identifier: userId,
      })
    });

    if (!response.ok) {
      // Handle specific error codes
      if (response.status === 429) {
        throw new Error('RATE_LIMIT');
      } else if (response.status === 404) {
        throw new Error('SESSION_NOT_FOUND');
      } else if (response.status === 401) {
        throw new Error('UNAUTHORIZED');
      } else {
        throw new Error('API_ERROR');
      }
    }

    const data = await response.json();
    return data;

  } catch (error) {
    console.error('Chat error:', error);

    let errorMessage;
    switch (error.message) {
      case 'RATE_LIMIT':
        errorMessage = 'We\'re experiencing high traffic right now. Please wait a moment and try again.';
        break;
      case 'SESSION_NOT_FOUND':
        errorMessage = 'Your session expired. Let\'s start fresh.';
        // Reset session
        setSessionId(null);
        localStorage.removeItem('luran_session_id');
        break;
      case 'UNAUTHORIZED':
        errorMessage = 'Authentication error. Please refresh the page.';
        break;
      default:
        errorMessage = 'Sorry, I\'m having trouble connecting right now. Please try again in a moment.';
    }

    // Show user-friendly error
    setMessages(prev => [...prev, {
      type: 'system',
      content: errorMessage,
      isError: true,
      timestamp: new Date(),
    }]);
  }
}
```

### Rate Limit Handling

```javascript
// Detect rate limiting early
if (response.status === 429) {
  const retryAfter = response.headers.get('Retry-After');
  const waitTime = retryAfter ? parseInt(retryAfter) : 60;

  setMessages(prev => [...prev, {
    type: 'system',
    content: `We're experiencing high traffic. Please try again in ${waitTime} seconds.`,
    timestamp: new Date(),
  }]);

  // Optional: Auto-retry after delay
  setTimeout(() => {
    sendMessage(userMessage);
  }, waitTime * 1000);
}
```

---

## 8. Best Practices

### ✅ DO

- Handle initial greeting on frontend
- Store and reuse `session_id` for conversation continuity
- Persist session_id in localStorage for page reloads
- Show loading states during API calls (~60-70 second typical response time)
- Display escalation notices prominently with ticket ID
- Include relevant metadata (page URL, user agent, etc.)
- Implement proper error handling and retry logic
- Show estimated wait time during loading ("this may take up to 60 seconds")
- Use WebSocket for real-time chat after agent takeover
- Handle session expiration gracefully

### ❌ DON'T

- Let the AI greet on every message
- Create new sessions for follow-up questions
- Hide loading indicators (responses take time)
- Ignore `should_escalate` flag or `ticket_id`
- Skip error handling for rate limits and network issues
- Assume instant responses (<5 seconds)
- Poll REST API repeatedly (use WebSocket for real-time)
- Store sensitive data in metadata
- Forget to handle session persistence on page reload

---

## 9. Example: Complete Chat Flow

```javascript
// 1. User opens chat widget
<ChatWidget opens>
Frontend displays: "👋 Hi there! How can we help you today?"

// 2. User types first message
User: "How do I file a claim?"
Frontend: [Shows loading indicator with "Getting you an answer (this may take up to 60 seconds)..."]

Backend (invisible to frontend):
  1. Embedding API: Convert question → vector (~400ms)
  2. Qdrant search: Find relevant chunks (~100ms)
  3. Gemma API: Generate response (~60-70 seconds typical)

Frontend receives (after ~70 seconds):
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "message": {
    "id": "msg-uuid-1",
    "session_id": "550e8400-e29b-41d4-a716-446655440000",
    "role": "ASSISTANT",
    "content": "After an accident or covered incident, the first step is...",
    "confidence_score": 0.73,
    "token_count": 156,
    "retrieved_chunk_ids": ["chunk-1", "chunk-2"],
    "meta": {
      "chunks_used": 2,
      "similarity_scores": [0.82, 0.75]
    },
    "created_at": "2026-01-19T14:30:00Z"
  },
  "should_escalate": false,
  "escalation_reason": null,
  "ticket_id": null
}

Frontend displays: "After an accident or covered incident, the first step is..."
Frontend stores: session_id in state and localStorage

// 3. User asks follow-up
User: "What about business hours?"
Frontend: [Includes session_id in request]

Backend:
  1. Embedding API: Convert question → vector
  2. Qdrant search: Find relevant chunks (none found)
  3. Gemma API: Generate response with full conversation history

Frontend receives:
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "message": {
    "id": "msg-uuid-2",
    "session_id": "550e8400-e29b-41d4-a716-446655440000",
    "role": "ASSISTANT",
    "content": "I don't have information about our business hours...",
    "confidence_score": 0.0,
    "token_count": 98,
    "retrieved_chunk_ids": [],
    "meta": {
      "chunks_used": 0,
      "similarity_scores": []
    },
    "created_at": "2026-01-19T14:32:00Z"
  },
  "should_escalate": true,
  "escalation_reason": "Low confidence score: 0.00",
  "ticket_id": "ticket-uuid-123"
}

Frontend displays:
  - AI message content
  - Escalation notice: "Escalated to Support - Ticket ID: TKT-2026-0042"
  - "Someone from our team will reach out shortly"

// 4. Optional: Switch to WebSocket for agent chat
if (data.should_escalate) {
  connectToAgentWebSocket(data.session_id);
}
```

---

## 10. Testing Checklist

- [ ] Greeting shows only once when chat opens
- [ ] Session ID persists across messages
- [ ] Session ID saved in localStorage
- [ ] Session ID restored on page reload
- [ ] Loading states show during API calls
- [ ] Loading message mentions possible 60-second wait
- [ ] Errors display user-friendly messages
- [ ] Escalation notices are prominent
- [ ] Ticket ID displayed when escalated
- [ ] Messages display in correct order
- [ ] Typing indicators work
- [ ] Multi-tenant greetings work correctly
- [ ] Rate limit errors handled gracefully
- [ ] 429 status shows appropriate retry message
- [ ] Session not found resets gracefully
- [ ] WebSocket connects after escalation
- [ ] Agent messages display with agent name
- [ ] metadata fields are populated correctly

---

## 11. Response Time Expectations

Set user expectations appropriately:

```javascript
// Show estimated wait time
<div className="typing-indicator">
  <Spinner />
  <span>Getting you an answer... (this may take up to 60 seconds)</span>
</div>
```

**Why this timing?**
- Gemma 3 12B is a large model (12 billion parameters)
- Runs on free tier Google AI Studio
- Generates thoughtful, context-aware responses
- Trade-off: Slower but free and high-quality

**Actual timing varies by:**
- Prompt complexity
- Response length requested
- Current Google server load
- Time of day (usage patterns)

**Typical range:** 45-90 seconds (median ~60-70 seconds)

**Alternative:** Consider upgrading to `gemini-2.0-flash-lite` for faster responses (~3-5 seconds) if budget allows.

---

## Summary

- ✅ Frontend handles initial greeting (not the AI)
- ✅ Embedding API is called for every user message (required for RAG)
- ✅ Each chat message = 2 API calls (embedding + generation)
- ✅ Free tier limit: ~100 conversations/day (1,000 messages)
- ✅ Response time: ~60-70 seconds typical with Gemma 3 12B
- ✅ Store session_id in state and localStorage
- ✅ Handle escalations prominently with ticket ID
- ✅ Use WebSocket for real-time agent chat after escalation
- ✅ Show proper loading states with time expectations
- ✅ Implement comprehensive error handling

---

## Additional Resources

- **API Reference:** See `/docs/FRONTEND_API_DOCUMENTATION.md`
- **WebSocket Guide:** See `/docs/WEBSOCKET_INTEGRATION.md`
- **Widget Integration:** See `/docs/WIDGET_INTEGRATION_GUIDE.md`
- **Authentication:** See `/docs/AUTHENTICATION.md`

---

**Questions? Issues?** Check the main documentation or contact the backend team.

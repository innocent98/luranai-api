# Chat Frontend Integration Guide

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

## 2. Frontend Implementation

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
        }
      })
    });

    const data = await response.json();

    // Save session ID from first response
    if (!sessionId) {
      setSessionId(data.session_id);
    }

    // Add AI response to UI
    setMessages(prev => [...prev, {
      type: 'assistant',
      content: data.message.content,
      confidence: data.message.confidence_score,
      shouldEscalate: data.should_escalate,
      timestamp: new Date(data.message.created_at),
    }]);
  };

  return (
    <div className="chat-widget">
      <MessageList messages={messages} />
      <MessageInput onSend={sendMessage} />
    </div>
  );
}
```

---

## 3. Greeting Customization by Tenant

You can customize the greeting based on tenant brand voice:

```javascript
const TENANT_GREETINGS = {
  'weysure': {
    greeting: '👋 Welcome to Weysure Insurance! How can we assist you today?',
    placeholder: 'Ask about claims, coverage, billing...'
  },
  'default': {
    greeting: '👋 Hi there! How can we help you today?',
    placeholder: 'Type your question here...'
  }
};

function getGreeting(tenantName) {
  return TENANT_GREETINGS[tenantName] || TENANT_GREETINGS.default;
}
```

---

## 4. API Usage: Why Embedding API is Called During Chat

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
   └─────────────┬───────────────────────┘
                 │
                 ▼
2. VECTOR SEARCH (Qdrant)
   ┌─────────────────────────────────────┐
   │ Find similar knowledge chunks        │
   │ Search using the embedding vector    │
   │ Returns: Top 2-5 relevant chunks     │
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
   - Speed: ~60-70 seconds
   - Cost: Free (30 requests/minute, 14,400/day)
   - Purpose: Create natural language answer

### API Call Counts Per Chat Message

| Action | Embedding API | Gemma API | Total API Calls |
|--------|--------------|-----------|-----------------|
| User sends message | 1 | 1 | 2 |
| 10 message conversation | 10 | 10 | 20 |

### Rate Limits to Consider

```python
# From config
EMBEDDING_MODEL: 90 RPM, 1,000 RPD
GEMINI_MODEL (Gemma 3 12B): 30 RPM, 14,400 RPD
```

**Bottleneck:** Embedding API has lower daily quota (1,000 vs 14,400), so that's your limiting factor for daily conversations.

**Safe daily capacity:**
- 1,000 embedding calls = ~1,000 user messages per day
- Average conversation = 10 messages
- **≈100 complete conversations per day** (free tier)

---

## 5. Message Types and UI States

### Message Types

```typescript
type Message = {
  type: 'system' | 'user' | 'assistant';
  content: string;
  timestamp: Date;

  // Only for assistant messages
  confidence?: number;
  shouldEscalate?: boolean;
  escalationReason?: string;
  retrievedChunks?: number;
};
```

### UI State Handling

```javascript
function MessageBubble({ message }) {
  // System greeting
  if (message.type === 'system') {
    return (
      <div className="system-message">
        {message.content}
      </div>
    );
  }

  // User message
  if (message.type === 'user') {
    return (
      <div className="user-message">
        {message.content}
      </div>
    );
  }

  // AI response
  return (
    <div className="ai-message">
      {message.content}

      {/* Show escalation notice if needed */}
      {message.shouldEscalate && (
        <div className="escalation-notice">
          <Icon name="alert" />
          This conversation has been flagged for specialist review.
          Someone from our team will reach out shortly.
        </div>
      )}

      {/* Optional: Show confidence indicator for debugging */}
      {process.env.NODE_ENV === 'development' && (
        <div className="debug-info">
          Confidence: {(message.confidence * 100).toFixed(1)}%
          | Chunks: {message.retrievedChunks}
        </div>
      )}
    </div>
  );
}
```

---

## 6. Error Handling

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
      })
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    const data = await response.json();
    return data;

  } catch (error) {
    console.error('Chat error:', error);

    // Show user-friendly error
    setMessages(prev => [...prev, {
      type: 'system',
      content: 'Sorry, I\'m having trouble connecting right now. Please try again in a moment.',
      isError: true,
      timestamp: new Date(),
    }]);
  }
}
```

### Rate Limit Handling

```javascript
// If you get rate limited (429 error)
if (response.status === 429) {
  setMessages(prev => [...prev, {
    type: 'system',
    content: 'We\'re experiencing high traffic right now. Please wait a moment and try again.',
    timestamp: new Date(),
  }]);
}
```

---

## 7. Best Practices

### ✅ DO

- Handle initial greeting on frontend
- Store and reuse `session_id` for conversation continuity
- Show loading states during API calls (60-70 second response time)
- Display escalation notices prominently
- Include relevant metadata (page URL, user agent, etc.)
- Implement proper error handling and retry logic

### ❌ DON'T

- Let the AI greet on every message
- Create new sessions for follow-up questions
- Hide loading indicators (responses take time)
- Ignore `should_escalate` flag
- Skip error handling

---

## 8. Example: Complete Chat Flow

```javascript
// 1. User opens chat widget
<ChatWidget opens>
Frontend displays: "👋 Hi there! How can we help you today?"

// 2. User types first message
User: "How do I file a claim?"
Frontend: [Shows typing indicator for ~70 seconds]

Backend:
  1. Embedding API: Convert question → vector (400ms)
  2. Qdrant search: Find relevant chunks (100ms)
  3. Gemma API: Generate response (~70 seconds)

Frontend receives:
{
  session_id: "new-uuid-here",
  message: {
    content: "After an accident or covered incident...",
    confidence_score: 0.73,
    chunks_used: 2
  },
  should_escalate: false
}

Frontend displays: "After an accident or covered incident..."
Frontend stores: session_id for next message

// 3. User asks follow-up
User: "What about business hours?"
Frontend: [Includes session_id in request]

Backend:
  1. Embedding API: Convert question → vector
  2. Qdrant search: Find relevant chunks
  3. Gemma API: Generate response (with conversation history)

Frontend receives:
{
  session_id: "same-uuid",
  message: {
    content: "I don't have information about our business hours...",
    confidence_score: 0.0
  },
  should_escalate: true,
  escalation_reason: "Low confidence score: 0.00"
}

Frontend displays:
  - AI message
  - Escalation notice: "Someone from our team will reach out shortly"
```

---

## 9. Testing Checklist

- [ ] Greeting shows only once when chat opens
- [ ] Session ID persists across messages
- [ ] Loading states show during API calls
- [ ] Errors display user-friendly messages
- [ ] Escalation notices are prominent
- [ ] Messages display in correct order
- [ ] Typing indicators work
- [ ] Multi-tenant greetings work correctly
- [ ] Rate limit errors handled gracefully
- [ ] Conversation history persists (reload widget)

---

## 10. Response Time Expectations

Set user expectations appropriately:

```javascript
// Show estimated wait time
<div className="typing-indicator">
  <Spinner />
  Getting you an answer... (this may take up to 60 seconds)
</div>
```

**Why so slow?**
- Gemma 3 12B is a large model running on free tier
- Generates thoughtful, context-aware responses
- Trade-off: Slower but free (vs. faster paid models)

**Alternative:** Consider upgrading to `gemini-2.0-flash-lite` for faster responses (~3-5 seconds) if budget allows.

---

## Summary

- ✅ Frontend handles initial greeting (not the AI)
- ✅ Embedding API is called for every user message (required for RAG)
- ✅ Each chat message = 2 API calls (embedding + generation)
- ✅ Free tier limit: ~100 conversations/day (1,000 messages)
- ✅ Response time: ~60-70 seconds with Gemma 3 12B
- ✅ Store session_id for conversation continuity
- ✅ Handle escalations prominently in UI

Questions? Check the main documentation or API reference.

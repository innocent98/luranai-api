# WebSocket Live Chat Integration Guide

## Overview

This document describes the real-time live chat takeover system that allows support agents to seamlessly take over conversations from the AI when escalation occurs.

## Architecture

### Components

1. **WebSocket Connection Manager** (`app/core/websocket.py`)
   - Singleton service managing all active WebSocket connections
   - Maintains separate pools for customer and agent connections
   - Handles broadcasting, typing indicators, and presence management

2. **WebSocket Endpoints** (`app/api/v1/endpoints/websocket.py`)
   - `WS /api/v1/ws/chat/{session_id}` - Customer connections
   - `WS /api/v1/ws/agent/{session_id}` - Agent connections

3. **Agent Takeover Endpoints** (`app/api/v1/endpoints/chat.py`)
   - `POST /api/v1/chat/sessions/{session_id}/takeover` - Agent takes over session
   - `POST /api/v1/chat/sessions/{session_id}/agent-message` - Send message via HTTP

4. **Database Models** (`app/db/models/conversation.py`)
   - New `MessageRole.HUMAN_AGENT` for agent messages
   - `ConversationSession.agent_id` - Agent assigned to session
   - `ConversationSession.agent_joined_at` - When agent joined

## Complete Flow

### 1. Normal AI Chat (Before Escalation)

```
┌─────────┐                    ┌─────────┐                    ┌─────────┐
│Customer │                    │  API    │                    │   AI    │
│ Widget  │                    │ Backend │                    │ (Gemini)│
└────┬────┘                    └────┬────┘                    └────┬────┘
     │                              │                              │
     │ POST /chat/message           │                              │
     ├─────────────────────────────>│                              │
     │                              │                              │
     │                              │ Vector Search + RAG          │
     │                              ├─────────────────────────────>│
     │                              │                              │
     │                              │ AI Response                  │
     │                              │<─────────────────────────────┤
     │                              │                              │
     │ AI Response                  │                              │
     │<─────────────────────────────┤                              │
     │                              │                              │
```

### 2. Escalation Triggered (AI Can't Help)

```
┌─────────┐         ┌─────────┐         ┌─────────┐         ┌─────────┐
│Customer │         │   API   │         │ Ticket  │         │  Email  │
│ Widget  │         │ Backend │         │ Service │         │ Service │
└────┬────┘         └────┬────┘         └────┬────┘         └────┬────┘
     │                   │                   │                   │
     │ POST /chat/message│                   │                   │
     ├──────────────────>│                   │                   │
     │                   │                   │                   │
     │                   │ Detect Escalation │                   │
     │                   │ (Low Confidence)  │                   │
     │                   │                   │                   │
     │                   │ Create Ticket     │                   │
     │                   ├──────────────────>│                   │
     │                   │                   │                   │
     │                   │ Ticket Created    │                   │
     │                   │<──────────────────┤                   │
     │                   │                   │                   │
     │                   │ Send Notification │                   │
     │                   ├───────────────────┼──────────────────>│
     │                   │                   │                   │
     │                   │                   │ Email to Agents   │
     │                   │                   │ & Admins          │
     │                   │                   │                   │
     │ AI Response       │                   │                   │
     │ + Escalation Flag │                   │                   │
     │<──────────────────┤                   │                   │
     │                   │                   │                   │
     │ "Connecting you   │                   │                   │
     │  to support..."   │                   │                   │
     │                   │                   │                   │
```

### 3. Agent Takes Over (Live Chat)

```
┌─────────┐    ┌──────────┐    ┌─────────┐    ┌──────────┐    ┌─────────┐
│Customer │    │ Customer │    │   API   │    │  Agent   │    │  Agent  │
│ Browser │    │ WebSocket│    │ Backend │    │ WebSocket│    │Dashboard│
└────┬────┘    └────┬─────┘    └────┬────┘    └────┬─────┘    └────┬────┘
     │              │               │              │               │
     │ Connect WS   │               │              │               │
     ├─────────────>│               │              │               │
     │              │               │              │               │
     │              │ WS /chat/{id} │              │               │
     │              ├──────────────>│              │               │
     │              │               │              │               │
     │              │ Connected ✓   │              │               │
     │              │<──────────────┤              │               │
     │              │               │              │               │
     │              │               │              │ Agent clicks  │
     │              │               │              │ "Take Over"   │
     │              │               │              │<──────────────┤
     │              │               │              │               │
     │              │               │ POST /takeover               │
     │              │               │<─────────────┼───────────────┤
     │              │               │              │               │
     │              │               │ Assign Agent │               │
     │              │               │ Assign Ticket│               │
     │              │               │              │               │
     │              │ Agent Joined  │              │               │
     │              │<──────────────┤              │               │
     │              │               │              │               │
     │ Show:        │               │              │ Connect WS    │
     │ "Agent Sarah │               │              │<──────────────┤
     │  joined chat"│               │              │               │
     │              │               │ WS /agent/{id}               │
     │              │               │<─────────────┤               │
     │              │               │              │               │
     │              │               │ Connected ✓  │               │
     │              │               ├─────────────>│               │
     │              │               │              │               │
     │              │               │              │ View history  │
     │              │               │              │ Start chatting│
     │              │               │              │               │
```

### 4. Real-Time Messaging (After Takeover)

```
┌─────────┐         ┌──────────┐         ┌─────────┐         ┌──────────┐
│Customer │         │ Customer │         │   API   │         │  Agent   │
│         │         │ WebSocket│         │ Backend │         │ WebSocket│
└────┬────┘         └────┬─────┘         └────┬────┘         └────┬─────┘
     │                   │                    │                    │
     │ Types message...  │                    │                    │
     │                   │                    │                    │
     │                   │ typing: true       │                    │
     │                   ├───────────────────>│                    │
     │                   │                    │                    │
     │                   │                    │ typing indicator   │
     │                   │                    ├───────────────────>│
     │                   │                    │                    │
     │                   │                    │ "Customer typing..." │
     │                   │                    │                    │
     │ Sends "Help me"   │                    │                    │
     ├──────────────────>│                    │                    │
     │                   │                    │                    │
     │                   │ message: "Help me" │                    │
     │                   ├───────────────────>│                    │
     │                   │                    │                    │
     │                   │                    │ Save to DB         │
     │                   │                    │                    │
     │                   │                    │ customer_message   │
     │                   │                    ├───────────────────>│
     │                   │                    │                    │
     │                   │                    │                    │ Show in UI
     │                   │                    │                    │
     │                   │                    │                    │ Agent replies
     │                   │                    │                    │ "I can help!"
     │                   │                    │                    │
     │                   │                    │ message: "I can..." │
     │                   │                    │<────────────────────┤
     │                   │                    │                    │
     │                   │                    │ Save to DB         │
     │                   │                    │                    │
     │                   │ agent_message      │                    │
     │                   │<───────────────────┤                    │
     │                   │                    │                    │
     │ Display reply     │                    │                    │
     │<──────────────────┤                    │                    │
     │                   │                    │                    │
     │ "Sarah: I can     │                    │                    │
     │  help you!"       │                    │                    │
     │                   │                    │                    │
```

## WebSocket Message Types

### Customer → Backend

```json
{
  "type": "message",
  "content": "I need help with my account"
}
```

```json
{
  "type": "typing",
  "is_typing": true
}
```

### Agent → Backend

```json
{
  "type": "message",
  "content": "I can help you with that!"
}
```

```json
{
  "type": "typing",
  "is_typing": true
}
```

### Backend → Customer

**Agent Joined**
```json
{
  "type": "agent_joined",
  "agent_name": "Sarah Johnson",
  "agent_id": "uuid-here",
  "timestamp": "2026-01-17T10:30:00Z"
}
```

**Agent Message**
```json
{
  "type": "agent_message",
  "message_id": "uuid-here",
  "content": "Hi! I'm Sarah. How can I help you today?",
  "agent_name": "Sarah Johnson",
  "timestamp": "2026-01-17T10:30:05Z"
}
```

**Typing Indicator**
```json
{
  "type": "typing_indicator",
  "is_typing": true,
  "user_name": "Sarah Johnson",
  "timestamp": "2026-01-17T10:30:03Z"
}
```

**Agent Left**
```json
{
  "type": "agent_left",
  "agent_name": "Sarah Johnson",
  "timestamp": "2026-01-17T11:00:00Z"
}
```

### Backend → Agent

**Customer Message**
```json
{
  "type": "customer_message",
  "message_id": "uuid-here",
  "content": "I can't access my account",
  "user_identifier": "customer@example.com",
  "timestamp": "2026-01-17T10:29:55Z"
}
```

**Agent Message (from another agent)**
```json
{
  "type": "agent_message",
  "message_id": "uuid-here",
  "content": "I'm working on this",
  "agent_name": "John Doe",
  "timestamp": "2026-01-17T10:31:00Z"
}
```

## API Endpoints

### WebSocket Connections

#### Customer WebSocket
```
WS /api/v1/ws/chat/{session_id}?user_identifier=customer@example.com
```

**Purpose**: Customer chat widget connection for real-time updates

**Authentication**: Verified by matching `user_identifier` with session

**Receives**:
- Agent messages
- Agent joined/left notifications
- Typing indicators from agents

**Sends**:
- Customer messages
- Typing indicators

#### Agent WebSocket
```
WS /api/v1/ws/agent/{session_id}?token=JWT_TOKEN
```

**Purpose**: Agent dashboard connection for monitoring and responding

**Authentication**: JWT token with TENANT_ADMIN or SUPPORT_AGENT role

**Receives**:
- Customer messages
- Messages from other agents
- Typing indicators from customer

**Sends**:
- Agent messages
- Typing indicators

### REST Endpoints

#### Agent Takeover
```http
POST /api/v1/chat/sessions/{session_id}/takeover
Authorization: Bearer JWT_TOKEN
```

**Purpose**: Agent claims responsibility for an escalated conversation

**Actions**:
1. Assigns session to agent
2. Assigns associated ticket to agent
3. Adds system message "Agent joined"
4. Broadcasts to customer via WebSocket

**Response**:
```json
{
  "id": "session-uuid",
  "tenant_id": "tenant-uuid",
  "user_identifier": "customer@example.com",
  "status": "escalated",
  "agent_id": "agent-uuid",
  "agent_joined_at": "2026-01-17T10:30:00Z",
  "escalation_reason": "Low confidence score: 0.15",
  "escalated_at": "2026-01-17T10:25:00Z",
  "created_at": "2026-01-17T10:00:00Z",
  "updated_at": "2026-01-17T10:30:00Z"
}
```

#### Send Agent Message (HTTP Alternative)
```http
POST /api/v1/chat/sessions/{session_id}/agent-message
Authorization: Bearer JWT_TOKEN
Content-Type: application/json

{
  "message": "Hi! I can help you with that.",
  "session_id": "session-uuid",
  "tenant_id": "tenant-uuid",
  "user_identifier": "customer@example.com"
}
```

**Purpose**: Send agent message via REST instead of WebSocket

**Use Cases**:
- Agents who prefer HTTP over WebSocket
- Automated agent responses
- Testing and debugging

**Response**:
```json
{
  "id": "message-uuid",
  "session_id": "session-uuid",
  "role": "human_agent",
  "content": "Hi! I can help you with that.",
  "confidence_score": null,
  "token_count": null,
  "created_at": "2026-01-17T10:30:05Z",
  "meta": {
    "agent_id": "agent-uuid",
    "agent_name": "Sarah Johnson"
  }
}
```

## Frontend Integration

### Customer Widget Integration

```javascript
// 1. Connect to WebSocket when session starts
const ws = new WebSocket(
  `wss://api.example.com/api/v1/ws/chat/${sessionId}?user_identifier=${userEmail}`
);

ws.onopen = () => {
  console.log('Connected to chat');
};

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);

  switch (data.type) {
    case 'agent_joined':
      showNotification(`${data.agent_name} joined the chat`);
      updateUIToAgentMode(data.agent_name);
      break;

    case 'agent_message':
      displayMessage({
        sender: data.agent_name,
        content: data.content,
        timestamp: data.timestamp,
        isAgent: true
      });
      break;

    case 'typing_indicator':
      if (data.is_typing) {
        showTypingIndicator(data.user_name);
      } else {
        hideTypingIndicator();
      }
      break;

    case 'agent_left':
      showNotification(`${data.agent_name} left the chat`);
      break;
  }
};

// 2. Send customer messages
function sendMessage(content) {
  ws.send(JSON.stringify({
    type: 'message',
    content: content
  }));
}

// 3. Send typing indicators
function notifyTyping(isTyping) {
  ws.send(JSON.stringify({
    type: 'typing',
    is_typing: isTyping
  }));
}
```

### Agent Dashboard Integration

```javascript
// 1. Connect to WebSocket when viewing a session
const ws = new WebSocket(
  `wss://api.example.com/api/v1/ws/agent/${sessionId}?token=${jwtToken}`
);

ws.onopen = () => {
  console.log('Monitoring session');
};

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);

  switch (data.type) {
    case 'customer_message':
      displayMessage({
        sender: 'Customer',
        content: data.content,
        timestamp: data.timestamp,
        isCustomer: true
      });
      playNotificationSound();
      break;

    case 'agent_message':
      // Message from another agent
      displayMessage({
        sender: data.agent_name,
        content: data.content,
        timestamp: data.timestamp,
        isAgent: true
      });
      break;

    case 'typing_indicator':
      if (data.is_typing) {
        showTypingIndicator('Customer');
      } else {
        hideTypingIndicator();
      }
      break;
  }
};

// 2. Take over conversation
async function takeOver() {
  const response = await fetch(
    `/api/v1/chat/sessions/${sessionId}/takeover`,
    {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${jwtToken}`
      }
    }
  );

  if (response.ok) {
    showSuccess('You are now handling this conversation');
    enableMessageInput();
  }
}

// 3. Send agent messages
function sendAgentMessage(content) {
  ws.send(JSON.stringify({
    type: 'message',
    content: content
  }));
}

// 4. Send typing indicators
function notifyTyping(isTyping) {
  ws.send(JSON.stringify({
    type: 'typing',
    is_typing: isTyping
  }));
}
```

## Database Schema Changes

### ConversationSession Table
```sql
ALTER TABLE conversation_sessions
ADD COLUMN agent_id UUID REFERENCES users(id) ON DELETE SET NULL,
ADD COLUMN agent_joined_at TIMESTAMP WITH TIME ZONE;
```

### Message Role Enum
```sql
ALTER TYPE message_role ADD VALUE 'human_agent';
```

## Connection Manager Details

### Connection Tracking

The `ConnectionManager` class maintains three data structures:

1. **customer_connections**: `Dict[str, WebSocket]`
   - Key: session_id (as string)
   - Value: Customer WebSocket connection
   - Only one customer per session

2. **agent_connections**: `Dict[str, Set[WebSocket]]`
   - Key: session_id (as string)
   - Value: Set of agent WebSocket connections
   - Multiple agents can monitor same session

3. **connection_metadata**: `Dict[WebSocket, dict]`
   - Key: WebSocket connection
   - Value: Metadata (type, session_id, user info, connected_at)
   - Used for tracking and cleanup

### Broadcasting Logic

**Send to Customer**:
```python
await connection_manager.send_to_customer(
    session_id=session_id,
    message={"type": "agent_message", "content": "Hello"}
)
```

**Send to All Agents**:
```python
await connection_manager.send_to_agents(
    session_id=session_id,
    message={"type": "customer_message", "content": "Help"},
    exclude_agent_id=sender_id  # Optional
)
```

**Broadcast to Everyone**:
```python
await connection_manager.broadcast_to_session(
    session_id=session_id,
    message={"type": "system_announcement", "content": "Session closing"},
    exclude_websocket=ws  # Optional
)
```

## Error Handling

### WebSocket Disconnections

**Customer Disconnects**:
- Connection removed from pool
- No notification sent (customer might reconnect)
- Session remains active

**Agent Disconnects**:
- Connection removed from pool
- "Agent left" notification sent to customer
- Session remains assigned to agent
- Agent can reconnect anytime

### Failed Message Delivery

**If customer offline**:
- Message saved to database
- Message delivery fails silently
- Customer sees message history when reconnects

**If agent offline**:
- Message saved to database
- Message delivery fails silently
- Agent sees message history when reconnects

## Security Considerations

### Customer Authentication
- Verified by matching `user_identifier` with session
- No JWT required (public widget)
- Session-scoped access only

### Agent Authentication
- Requires valid JWT token
- Must have TENANT_ADMIN or SUPPORT_AGENT role
- Tenant-scoped access (can only view own tenant's sessions)

### Message Validation
- All messages sanitized before broadcasting
- Content length limits enforced
- Rate limiting applied (via Redis)

## Scaling Considerations

### Current Implementation
- In-memory connection pool (single server)
- No persistence of connection state
- No cross-server broadcasting

### Future Enhancements (When Scaling)

**Redis Pub/Sub for Multi-Server**:
```python
# Publish message to Redis
redis.publish(f"session:{session_id}", json.dumps(message))

# Subscribe to Redis channel
pubsub = redis.pubsub()
pubsub.subscribe(f"session:{session_id}")

# Broadcast to local WebSocket connections
for message in pubsub.listen():
    await connection_manager.send_to_customer(...)
```

**Sticky Sessions**:
- Route customers to same server instance
- Use load balancer with session affinity
- Reduces need for cross-server messaging

## Testing

### Manual Testing

**Test Customer WebSocket**:
```bash
# Using wscat
wscat -c "ws://localhost:8000/api/v1/ws/chat/SESSION_ID?user_identifier=test@example.com"

# Send message
> {"type": "message", "content": "Hello"}

# Send typing indicator
> {"type": "typing", "is_typing": true}
```

**Test Agent WebSocket**:
```bash
# Get JWT token first
TOKEN=$(curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"agent@example.com","password":"password"}' | jq -r '.access_token')

# Connect with wscat
wscat -c "ws://localhost:8000/api/v1/ws/agent/SESSION_ID?token=$TOKEN"

# Send message
> {"type": "message", "content": "I can help!"}
```

### Automated Testing

See `tests/test_websocket.py` for comprehensive test suite including:
- Connection authentication
- Message broadcasting
- Typing indicators
- Agent takeover flow
- Disconnection handling

## Monitoring

### Key Metrics

- Active WebSocket connections (customers + agents)
- Average connection duration
- Message throughput (messages/second)
- Failed message deliveries
- Agent response time (time from customer message to agent reply)

### Logging

All WebSocket events are logged with:
```python
logger.info(f"Customer connected to session {session_id}")
logger.info(f"Agent {agent_name} took over session {session_id}")
logger.error(f"Error sending to customer: {error}")
```

## Troubleshooting

### Common Issues

**"WebSocket connection failed"**
- Check CORS configuration
- Verify WebSocket upgrade headers
- Ensure load balancer supports WebSocket

**"No agent connection for session"**
- Agent hasn't connected to WebSocket yet
- Agent disconnected - need to reconnect
- Check agent authentication token

**"Messages not being received"**
- Check connection state
- Verify session_id matches
- Check network/firewall blocking WebSocket

## Summary

This implementation provides:
- ✅ Real-time bidirectional communication
- ✅ Seamless agent takeover from AI
- ✅ Typing indicators and presence
- ✅ Multiple agents can monitor same session
- ✅ Automatic ticket assignment
- ✅ Email notifications to agents
- ✅ Message persistence in database
- ✅ HTTP fallback for agent messages

**Next Steps**:
1. Run database migration to add new fields
2. Test WebSocket connections
3. Integrate frontend widget
4. Build agent dashboard UI
5. Add monitoring and metrics

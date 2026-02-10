# Chat Widget - Welcome Flow Implementation Guide

## Overview

This guide explains how to implement Solution 3: automatic welcome messages with flow support for the chat widget.

**User Experience:**
- ✅ **With flows:** Buttons appear immediately when chat opens
- ✅ **Without flows:** Friendly welcome message appears
- ✅ **Seamless:** Works automatically, no extra configuration needed

---

## How It Works

### Backend Behavior (Already Implemented ✅)

1. Frontend sends `message: "start"` on chat initialization
2. Backend checks if tenant has a welcome flow
3. **With flow:** Returns buttons for user interaction
4. **Without flow:** Returns default welcome message
5. **Important:** "start" message is never stored in conversation history

### What Frontend Needs to Do

Frontend only needs to:
1. Create session on first chat open
2. Send "start" message automatically
3. Display AI response (hide "start" from user)
4. Render buttons if present

---

## Implementation Steps

### Step 1: Initialize Chat Widget

```typescript
class ChatWidget {
  private sessionId: string | null = null;
  private apiKey: string;
  private tenantId: string;

  async initialize() {
    // Check if session already exists
    this.sessionId = localStorage.getItem('luran_session_id');

    if (!this.sessionId) {
      // New user - create session and show welcome
      await this.createNewSession();
    } else {
      // Returning user - load conversation history
      await this.loadConversationHistory();
    }

    // Show chat widget
    this.showWidget();
  }

  private async createNewSession() {
    try {
      // 1. Create session
      const sessionResponse = await fetch('/api/v1/chat/sessions', {
        method: 'POST',
        headers: {
          'X-API-Key': this.apiKey,
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({
          tenant_id: this.tenantId,
          user_identifier: this.getUserIdentifier(),
          metadata: {
            source: 'website',
            page_url: window.location.href,
            user_agent: navigator.userAgent
          }
        })
      });

      const session = await sessionResponse.json();
      this.sessionId = session.id;

      // Save session for future
      localStorage.setItem('luran_session_id', session.id);

      // 2. Auto-send welcome trigger
      await this.sendWelcomeMessage();
    } catch (error) {
      console.error('Failed to initialize chat:', error);
      this.showErrorMessage();
    }
  }

  private async sendWelcomeMessage() {
    try {
      const response = await fetch('/api/v1/chat/message', {
        method: 'POST',
        headers: {
          'X-API-Key': this.apiKey,
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({
          session_id: this.sessionId,
          message: 'start'  // Special welcome trigger
        })
      });

      const data = await response.json();

      // IMPORTANT: Only display AI response, not the "start" message
      this.displayMessage({
        role: 'assistant',
        content: data.message.content,
        meta: data.message.meta,
        created_at: data.message.created_at
      });

      // Handle escalation if needed
      if (data.should_escalate) {
        this.handleEscalation(data.escalation_reason);
      }
    } catch (error) {
      console.error('Failed to send welcome message:', error);
    }
  }

  private getUserIdentifier(): string {
    // Try to get user email if they're logged in
    const userEmail = this.getUserEmail();
    if (userEmail) return userEmail;

    // Otherwise, generate anonymous ID
    let anonymousId = localStorage.getItem('luran_anonymous_id');
    if (!anonymousId) {
      anonymousId = `anon_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;
      localStorage.setItem('luran_anonymous_id', anonymousId);
    }
    return anonymousId;
  }
}
```

### Step 2: Display Messages with Button Support

```typescript
interface Message {
  role: 'user' | 'assistant';
  content: string;
  meta?: {
    flow_triggered?: boolean;
    flow_id?: string;
    buttons?: string[];
    auto_welcome?: boolean;
  };
  created_at: string;
}

displayMessage(message: Message) {
  const messageEl = document.createElement('div');
  messageEl.className = `message ${message.role}-message`;

  // Message content
  const contentEl = document.createElement('div');
  contentEl.className = 'message-content';
  contentEl.textContent = message.content;
  messageEl.appendChild(contentEl);

  // Flow buttons (if present)
  if (message.meta?.buttons && message.meta.buttons.length > 0) {
    const buttonsEl = this.createButtonsElement(message.meta.buttons);
    messageEl.appendChild(buttonsEl);
  }

  // Add to chat
  this.chatContainer.appendChild(messageEl);
  this.scrollToBottom();
}

private createButtonsElement(buttons: string[]): HTMLElement {
  const container = document.createElement('div');
  container.className = 'flow-buttons';

  buttons.forEach(buttonText => {
    const button = document.createElement('button');
    button.className = 'flow-button';
    button.textContent = buttonText;
    button.onclick = () => this.handleButtonClick(buttonText);
    container.appendChild(button);
  });

  return container;
}
```

### Step 3: Handle Button Clicks

```typescript
async handleButtonClick(buttonText: string) {
  // Send button text as user's message
  await this.sendMessage(buttonText);
}

async sendMessage(messageText: string) {
  try {
    // Show user message
    this.displayMessage({
      role: 'user',
      content: messageText,
      created_at: new Date().toISOString()
    });

    // Send to backend
    const response = await fetch('/api/v1/chat/message', {
      method: 'POST',
      headers: {
        'X-API-Key': this.apiKey,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        session_id: this.sessionId,
        message: messageText
      })
    });

    const data = await response.json();

    // Show AI response (may include more buttons)
    this.displayMessage({
      role: 'assistant',
      content: data.message.content,
      meta: data.message.meta,
      created_at: data.message.created_at
    });

    // Handle escalation
    if (data.should_escalate) {
      this.handleEscalation(data.escalation_reason);
    }
  } catch (error) {
    console.error('Failed to send message:', error);
    this.showErrorMessage();
  }
}

private handleEscalation(reason?: string) {
  const escalationMsg = document.createElement('div');
  escalationMsg.className = 'escalation-notice';
  escalationMsg.innerHTML = `
    <div class="escalation-icon">👤</div>
    <div class="escalation-text">
      <strong>Connecting to support team</strong>
      <p>An agent will join this conversation shortly.</p>
    </div>
  `;
  this.chatContainer.appendChild(escalationMsg);
}
```

### Step 4: CSS Styling

```css
/* Flow Buttons */
.flow-buttons {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 12px;
  padding: 8px 0;
}

.flow-button {
  background: white;
  border: 1.5px solid #e5e7eb;
  border-radius: 20px;
  padding: 10px 20px;
  font-size: 14px;
  font-weight: 500;
  color: #374151;
  cursor: pointer;
  transition: all 0.2s ease;
  white-space: nowrap;
}

.flow-button:hover {
  background: #f9fafb;
  border-color: var(--primary-color, #2563eb);
  color: var(--primary-color, #2563eb);
}

.flow-button:active {
  transform: scale(0.98);
}

/* Message Styling */
.message {
  margin-bottom: 16px;
  max-width: 80%;
}

.message.assistant-message {
  align-self: flex-start;
}

.message.user-message {
  align-self: flex-end;
  background: var(--primary-color, #2563eb);
  color: white;
  border-radius: 18px;
  padding: 10px 16px;
}

.message-content {
  line-height: 1.5;
}

/* Escalation Notice */
.escalation-notice {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px;
  background: #fef3c7;
  border: 1px solid #fde68a;
  border-radius: 8px;
  margin: 16px 0;
}

.escalation-icon {
  font-size: 24px;
}

.escalation-text strong {
  color: #92400e;
}

.escalation-text p {
  margin: 4px 0 0 0;
  font-size: 13px;
  color: #78350f;
}
```

---

## Testing

### Test Case 1: Tenant WITH Welcome Flow

**Setup:**
1. Tenant has a welcome flow with keyword trigger: "start"
2. Flow has button options

**Expected Behavior:**
```
User opens chat
  ↓
Frontend sends: { message: "start" }
  ↓
Backend: Matches welcome flow
  ↓
Response: {
  message: {
    content: "What can I help you with?",
    meta: {
      buttons: ["Request demo", "View pricing", "Get support"]
    }
  }
}
  ↓
User sees: Message + 3 clickable buttons
```

### Test Case 2: Tenant WITHOUT Welcome Flow

**Setup:**
1. Tenant has no flows configured

**Expected Behavior:**
```
User opens chat
  ↓
Frontend sends: { message: "start" }
  ↓
Backend: No flow matches
  ↓
Response: {
  message: {
    content: "Hi! Welcome to Acme Corp. I'm here to help! What can I assist you with today?",
    meta: { auto_welcome: true, no_flow_available: true }
  }
}
  ↓
User sees: Friendly welcome message (no buttons)
```

### Test Case 3: Returning User

**Setup:**
1. User has existing session in localStorage

**Expected Behavior:**
```
User opens chat
  ↓
Session ID found in localStorage
  ↓
Load conversation history (no "start" message sent)
  ↓
User sees: Previous conversation
```

---

## Common Issues & Solutions

### Issue 1: "start" message appears in chat

**Problem:** User sees their own "start" message

**Solution:** Make sure you only display the AI response:
```typescript
// ❌ WRONG
this.displayMessage({ role: 'user', content: 'start' });
this.displayMessage(data.message);

// ✅ CORRECT
this.displayMessage(data.message); // Only AI response
```

### Issue 2: Buttons not clickable

**Problem:** Buttons don't respond to clicks

**Solution:** Check button click handler is properly bound:
```typescript
button.onclick = () => this.handleButtonClick(buttonText);
// NOT: button.onclick = this.handleButtonClick(buttonText);
```

### Issue 3: Welcome message shows every time

**Problem:** Welcome message appears even for returning users

**Solution:** Check localStorage for existing session:
```typescript
const sessionId = localStorage.getItem('luran_session_id');
if (!sessionId) {
  await this.sendWelcomeMessage();
}
```

---

## API Reference

### Create Session
```
POST /api/v1/chat/sessions
X-API-Key: pk_live_...
{
  "tenant_id": "uuid",
  "user_identifier": "user@example.com"
}
```

### Send Message
```
POST /api/v1/chat/message
X-API-Key: pk_live_...
{
  "session_id": "uuid",
  "message": "start"
}
```

### Response with Buttons
```json
{
  "session_id": "uuid",
  "message": {
    "content": "What can I help with?",
    "meta": {
      "buttons": ["Option 1", "Option 2", "Option 3"]
    }
  },
  "should_escalate": false
}
```

---

## Summary

**What You Need to Do:**
1. ✅ Send "start" message on chat initialization
2. ✅ Display only AI response (hide "start")
3. ✅ Render buttons if `meta.buttons` exists
4. ✅ Handle button clicks by sending button text as message

**What Backend Does Automatically:**
1. ✅ Detects "start" trigger
2. ✅ Matches welcome flow (if exists)
3. ✅ Returns buttons OR default welcome
4. ✅ Never stores "start" in conversation history

**Result:** Clean, professional welcome experience that works with or without flows! 🚀

# Luran AI Actions System - Technical Specification

**Feature Name**: Configurable AI Actions (Workflows)
**Status**: Future Roadmap (Post-Launch)
**Estimated Effort**: 4-6 weeks
**Priority**: High (Premium Feature)
**Target Release**: Q2 2026

---

## 1. Executive Summary

### What is This?

Transform Luran from a Q&A chatbot into an **AI agent** that can execute real-world actions on behalf of users by calling tenant-configured API endpoints.

### Value Proposition

- **For End Users**: "Book my appointment" → AI books it, no human needed
- **For Tenants**: Reduce support workload, improve customer experience, automate repetitive tasks
- **For Luran**: Premium feature, competitive differentiation, increased stickiness

### Current State (Already Implemented ✅)

**Simplified Actions System** - AI can create support tickets:
- Auto-detects escalation needs
- Creates tickets with smart summaries
- Determines priority automatically
- Sends notifications
- Confirms to user

This proves the concept works. Now we expand it to **configurable tenant actions**.

---

## 2. Feature Overview

### Core Concept

Tenants configure custom actions in their dashboard:
- **Action name**: `book_appointment`, `track_order`, `cancel_subscription`
- **Description**: Helps AI understand when to use it
- **API endpoint**: Tenant's system we'll call
- **Request schema**: What data we need from user
- **Response schema**: What we expect back

### User Experience Flow

```
User: "I need to book a haircut appointment for tomorrow at 2pm"

AI (Internal):
  1. Detects intent → booking
  2. Checks: Does tenant have "book_appointment" action enabled?
  3. Yes → Extracts details from message:
     - service: "haircut"
     - date: "tomorrow"
     - time: "2pm"
  4. Validates against schema (need email too)

AI: "I can help book that! What's your email for confirmation?"

User: "john@example.com"

AI (Internal):
  5. All fields collected
  6. Validates data
  7. Calls tenant API: POST https://salon.com/api/bookings
  8. Receives: { "success": true, "booking_id": "APT-12345" }

AI: "Done! Your haircut appointment is booked for tomorrow at 2pm.
     Booking reference: APT-12345. You'll receive a confirmation email shortly."
```

### Key Principles

- ✅ **Explicit configuration**: AI never invents actions
- ✅ **Safe by default**: Permission-based, validated inputs
- ✅ **Conversational**: AI collects missing details naturally
- ✅ **Transparent**: User knows what's happening
- ✅ **Auditable**: Every action execution logged

---

## 3. Database Schema

### 3.1 TenantAction Model

```python
class TenantAction(Base):
    """Configurable actions that AI can execute for a tenant."""

    __tablename__ = "tenant_actions"

    # Core fields
    id: UUID = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: UUID = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)

    # Action identification
    name: str = Column(String(100), nullable=False)  # e.g., "book_appointment"
    display_name: str = Column(String(200), nullable=False)  # e.g., "Book Appointment"
    description: str = Column(Text, nullable=False)  # AI uses this to know when to trigger

    # API configuration
    endpoint_url: str = Column(String(500), nullable=False)  # e.g., "https://api.tenant.com/bookings"
    http_method: str = Column(String(10), default="POST")  # POST, PUT, PATCH, DELETE
    timeout_seconds: int = Column(Integer, default=30)

    # Authentication
    auth_type: str = Column(String(50), default="bearer")  # bearer, api_key, basic, custom
    auth_header_name: str = Column(String(100), default="Authorization")
    auth_header_value_encrypted: str = Column(Text, nullable=False)  # Encrypted token/key

    # Additional headers (JSON)
    custom_headers: dict = Column(JSONB, default={})  # e.g., {"Content-Type": "application/json"}

    # Request/Response schemas (JSON Schema format)
    request_schema: dict = Column(JSONB, nullable=False)
    response_schema: dict = Column(JSONB, nullable=True)  # Optional but recommended

    # AI behavior
    requires_confirmation: bool = Column(Boolean, default=True)  # Ask user before executing
    confirmation_message: str = Column(Text, nullable=True)  # e.g., "Should I book this appointment?"
    success_message_template: str = Column(Text, nullable=True)  # e.g., "Booked! ID: {booking_id}"

    # Settings
    is_enabled: bool = Column(Boolean, default=True)
    max_retries: int = Column(Integer, default=1)

    # Metadata
    created_at: datetime = Column(DateTime, default=datetime.utcnow)
    updated_at: datetime = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by: UUID = Column(UUID(as_uuid=True), ForeignKey("users.id"))

    # Stats
    execution_count: int = Column(Integer, default=0)
    last_executed_at: datetime = Column(DateTime, nullable=True)

    # Relationships
    tenant = relationship("Tenant", back_populates="actions")
    executions = relationship("ActionExecution", back_populates="action", cascade="all, delete-orphan")

    # Indexes
    __table_args__ = (
        Index("idx_tenant_actions_tenant_id", "tenant_id"),
        Index("idx_tenant_actions_name", "tenant_id", "name"),
        Index("idx_tenant_actions_enabled", "tenant_id", "is_enabled"),
        UniqueConstraint("tenant_id", "name", name="uq_tenant_action_name"),
    )
```

### 3.2 ActionExecution Model (Audit Log)

```python
class ActionExecution(Base):
    """Audit log of all action executions."""

    __tablename__ = "action_executions"

    # Core fields
    id: UUID = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    action_id: UUID = Column(UUID(as_uuid=True), ForeignKey("tenant_actions.id"), nullable=False)
    session_id: UUID = Column(UUID(as_uuid=True), ForeignKey("conversation_sessions.id"), nullable=False)
    message_id: UUID = Column(UUID(as_uuid=True), ForeignKey("messages.id"), nullable=True)

    # Execution details
    status: str = Column(String(50), nullable=False)  # success, failed, timeout, validation_error

    # Request
    input_params: dict = Column(JSONB, nullable=False)  # User-provided data
    request_body: dict = Column(JSONB, nullable=True)  # Actual request sent
    request_headers: dict = Column(JSONB, nullable=True)  # Headers sent (sanitized)

    # Response
    response_status_code: int = Column(Integer, nullable=True)
    response_body: dict = Column(JSONB, nullable=True)
    response_time_ms: int = Column(Integer, nullable=True)

    # Error handling
    error_message: str = Column(Text, nullable=True)
    error_type: str = Column(String(100), nullable=True)  # validation, network, timeout, api_error
    retry_count: int = Column(Integer, default=0)

    # User confirmation
    required_confirmation: bool = Column(Boolean, default=True)
    user_confirmed: bool = Column(Boolean, nullable=True)
    confirmed_at: datetime = Column(DateTime, nullable=True)

    # Metadata
    executed_at: datetime = Column(DateTime, default=datetime.utcnow)
    completed_at: datetime = Column(DateTime, nullable=True)

    # Relationships
    action = relationship("TenantAction", back_populates="executions")
    session = relationship("ConversationSession")
    message = relationship("Message")

    # Indexes
    __table_args__ = (
        Index("idx_action_executions_action_id", "action_id"),
        Index("idx_action_executions_session_id", "session_id"),
        Index("idx_action_executions_status", "status"),
        Index("idx_action_executions_executed_at", "executed_at"),
    )
```

### 3.3 Update Tenant Model

```python
# Add to Tenant model
class Tenant(Base):
    # ... existing fields ...

    # Relationships
    actions = relationship("TenantAction", back_populates="tenant", cascade="all, delete-orphan")
```

### 3.4 Update TenantCapabilities

```python
# Add to TenantCapabilities model
class TenantCapabilities(Base):
    # ... existing fields ...

    # Actions feature flag
    can_use_actions: bool = Column(Boolean, default=False)
    max_actions: int = Column(Integer, default=5)  # Limit per plan
```

---

## 4. API Endpoints

### 4.1 Action Management (Admin/Tenant Admin)

#### `GET /api/v1/actions`
**Purpose**: List all actions for tenant
**Auth**: Tenant Admin
**Response**:
```json
{
  "actions": [
    {
      "id": "uuid",
      "name": "book_appointment",
      "display_name": "Book Appointment",
      "description": "Books appointments for customers",
      "is_enabled": true,
      "execution_count": 45,
      "last_executed_at": "2026-01-15T10:30:00Z"
    }
  ]
}
```

#### `POST /api/v1/actions`
**Purpose**: Create new action
**Auth**: Tenant Admin
**Request**:
```json
{
  "name": "book_appointment",
  "display_name": "Book Appointment",
  "description": "Use this to book appointments for customers. Ask for service type, date, time, and email.",
  "endpoint_url": "https://api.mycompany.com/bookings",
  "http_method": "POST",
  "auth_type": "bearer",
  "auth_token": "sk_live_xxxxx",
  "request_schema": {
    "type": "object",
    "required": ["service", "date", "time", "email"],
    "properties": {
      "service": {"type": "string", "description": "Service type (e.g., haircut, massage)"},
      "date": {"type": "string", "format": "date", "description": "Appointment date"},
      "time": {"type": "string", "pattern": "^([0-1]?[0-9]|2[0-3]):[0-5][0-9]$"},
      "email": {"type": "string", "format": "email"}
    }
  },
  "response_schema": {
    "type": "object",
    "properties": {
      "success": {"type": "boolean"},
      "booking_id": {"type": "string"},
      "message": {"type": "string"}
    }
  },
  "requires_confirmation": true,
  "success_message_template": "Your appointment is booked! Confirmation: {booking_id}"
}
```

#### `GET /api/v1/actions/{action_id}`
**Purpose**: Get action details
**Auth**: Tenant Admin

#### `PATCH /api/v1/actions/{action_id}`
**Purpose**: Update action
**Auth**: Tenant Admin

#### `DELETE /api/v1/actions/{action_id}`
**Purpose**: Delete action
**Auth**: Tenant Admin

#### `POST /api/v1/actions/{action_id}/test`
**Purpose**: Test action with sample data
**Auth**: Tenant Admin
**Request**:
```json
{
  "test_params": {
    "service": "haircut",
    "date": "2026-01-20",
    "time": "14:00",
    "email": "test@example.com"
  }
}
```

#### `POST /api/v1/actions/{action_id}/toggle`
**Purpose**: Enable/disable action
**Auth**: Tenant Admin

---

### 4.2 Action Execution Logs (Admin/Tenant Admin)

#### `GET /api/v1/actions/{action_id}/executions`
**Purpose**: Get execution history for an action
**Auth**: Tenant Admin
**Query Params**: `?status=success&limit=50&offset=0`

#### `GET /api/v1/actions/executions/{execution_id}`
**Purpose**: Get detailed execution log
**Auth**: Tenant Admin
**Response**:
```json
{
  "id": "uuid",
  "action_name": "book_appointment",
  "status": "success",
  "input_params": {
    "service": "haircut",
    "date": "2026-01-20",
    "time": "14:00",
    "email": "john@example.com"
  },
  "response_body": {
    "success": true,
    "booking_id": "APT-12345"
  },
  "response_time_ms": 432,
  "executed_at": "2026-01-15T10:30:00Z",
  "session_id": "uuid",
  "user_confirmed": true
}
```

---

## 5. AI Integration (Function Calling with Gemini)

### 5.1 Prompt Injection Strategy

When AI generates responses, inject available actions as "tools":

```python
# In RAGService.assemble_prompt()

def assemble_prompt_with_actions(
    self,
    ai_config: TenantAIConfig,
    user_query: str,
    retrieved_chunks: list[KnowledgeChunkResponse],
    conversation_history: list[MessageResponse],
    available_actions: list[TenantAction],  # NEW
    company_name: str,
    company_description: Optional[str] = None,
    max_history_turns: int = 10,
) -> dict:

    # ... existing prompt assembly ...

    # NEW: Add available actions
    if available_actions:
        actions_context = self._build_actions_context(available_actions)
        full_prompt += f"\n\nAVAILABLE ACTIONS:\n{actions_context}"

    return {"full_prompt": full_prompt, "actions": available_actions}

def _build_actions_context(self, actions: list[TenantAction]) -> str:
    """Build context about available actions for AI."""
    if not actions:
        return "No actions available."

    context_parts = []
    for action in actions:
        schema_fields = ", ".join([
            f"{name} ({props.get('description', 'no description')})"
            for name, props in action.request_schema.get('properties', {}).items()
        ])

        context_parts.append(
            f"[Action: {action.display_name}]\n"
            f"Description: {action.description}\n"
            f"Required fields: {schema_fields}\n"
            f"Confirmation required: {'Yes' if action.requires_confirmation else 'No'}"
        )

    return "\n\n".join(context_parts)
```

### 5.2 Function Calling with Gemini

Use Gemini's native function calling:

```python
from google import genai
from google.genai import types

async def generate_ai_response_with_actions(
    self,
    prompt: dict,
    available_actions: list[TenantAction],
    max_tokens: int = 400,
) -> tuple[str, Optional[dict], Optional[int]]:
    """
    Generate AI response with function calling support.

    Returns:
        Tuple of (response_text, action_call, token_count)
        - action_call is dict with {action_name, params} if AI wants to call action
        - None if AI just answers normally
    """
    client = genai.Client(api_key=settings.GOOGLE_API_KEY)

    # Convert actions to Gemini function declarations
    tools = []
    for action in available_actions:
        if not action.is_enabled:
            continue

        tools.append(
            types.Tool(
                function_declarations=[
                    types.FunctionDeclaration(
                        name=action.name,
                        description=action.description,
                        parameters=types.Schema(
                            type=types.Type.OBJECT,
                            properties=action.request_schema.get('properties', {}),
                            required=action.request_schema.get('required', []),
                        )
                    )
                ]
            )
        )

    # Generate response
    response = client.models.generate_content(
        model=settings.GEMINI_MODEL,
        contents=prompt["full_prompt"],
        config=types.GenerateContentConfig(
            max_output_tokens=max_tokens,
            temperature=0.7,
            tools=tools if tools else None,
        ),
    )

    # Check if AI wants to call a function
    if response.candidates[0].content.parts:
        for part in response.candidates[0].content.parts:
            if hasattr(part, 'function_call'):
                function_call = part.function_call
                return (
                    None,  # No text response yet
                    {
                        "action_name": function_call.name,
                        "params": dict(function_call.args)
                    },
                    response.usage_metadata.total_token_count
                )

    # Normal text response
    return response.text, None, response.usage_metadata.total_token_count
```

### 5.3 Multi-Turn Parameter Collection

```python
class ActionExecutionService:
    """Service for executing tenant actions."""

    def __init__(self, db: Session):
        self.db = db

    async def collect_and_execute_action(
        self,
        action: TenantAction,
        session_id: UUID,
        initial_params: dict,
        conversation_service: ConversationService,
        rag_service: RAGService,
    ) -> dict:
        """
        Collect missing parameters through conversation, then execute action.

        Flow:
        1. Validate initial params against schema
        2. If missing fields → ask user for them
        3. Collect responses over multiple turns
        4. Once complete → validate all → execute
        """

        # Get required fields from schema
        required_fields = action.request_schema.get('required', [])
        properties = action.request_schema.get('properties', {})

        # Track what we have
        collected_params = initial_params.copy()
        missing_fields = [
            field for field in required_fields
            if field not in collected_params or not collected_params[field]
        ]

        # If we have everything, execute immediately
        if not missing_fields:
            return await self.execute_action(action, session_id, collected_params)

        # Otherwise, store pending action in session metadata
        session_meta = {
            'pending_action': {
                'action_id': str(action.id),
                'action_name': action.name,
                'collected_params': collected_params,
                'missing_fields': missing_fields,
                'next_field_index': 0,
            }
        }

        # Ask for first missing field
        next_field = missing_fields[0]
        field_info = properties.get(next_field, {})

        question = self._generate_field_question(next_field, field_info)

        return {
            'status': 'collecting_params',
            'message': question,
            'missing_fields': missing_fields,
        }

    def _generate_field_question(self, field_name: str, field_info: dict) -> str:
        """Generate natural question for missing field."""
        description = field_info.get('description', '')

        # Map field names to natural questions
        question_templates = {
            'email': "What's your email address?",
            'phone': "What's your phone number?",
            'date': "What date works for you?",
            'time': "What time would you prefer?",
            'name': "What's your name?",
        }

        if field_name in question_templates:
            return question_templates[field_name]

        # Fallback
        if description:
            return f"I need to know: {description}"
        return f"Could you provide the {field_name.replace('_', ' ')}?"

    async def execute_action(
        self,
        action: TenantAction,
        session_id: UUID,
        params: dict,
    ) -> dict:
        """Execute action by calling tenant API."""
        import httpx
        from app.utils.encryption import decrypt_value

        # Validate params against schema
        validation_errors = self._validate_params(params, action.request_schema)
        if validation_errors:
            return {
                'status': 'validation_error',
                'errors': validation_errors
            }

        # Create execution record
        execution = ActionExecution(
            action_id=action.id,
            session_id=session_id,
            status='pending',
            input_params=params,
        )
        self.db.add(execution)
        self.db.commit()

        try:
            # Decrypt auth token
            auth_value = decrypt_value(action.auth_header_value_encrypted)

            # Prepare headers
            headers = {
                action.auth_header_name: auth_value,
                **action.custom_headers,
            }

            # Make HTTP request
            async with httpx.AsyncClient(timeout=action.timeout_seconds) as client:
                start_time = time.time()

                if action.http_method == "POST":
                    response = await client.post(
                        action.endpoint_url,
                        json=params,
                        headers=headers,
                    )
                elif action.http_method == "PUT":
                    response = await client.put(
                        action.endpoint_url,
                        json=params,
                        headers=headers,
                    )
                # ... other methods

                response_time_ms = int((time.time() - start_time) * 1000)

            # Update execution record
            execution.response_status_code = response.status_code
            execution.response_body = response.json() if response.headers.get('content-type') == 'application/json' else {'raw': response.text}
            execution.response_time_ms = response_time_ms
            execution.completed_at = datetime.utcnow()

            # Check if successful
            if 200 <= response.status_code < 300:
                execution.status = 'success'

                # Update action stats
                action.execution_count += 1
                action.last_executed_at = datetime.utcnow()

                self.db.commit()

                # Format success message
                success_message = self._format_success_message(
                    action.success_message_template,
                    execution.response_body
                )

                return {
                    'status': 'success',
                    'message': success_message,
                    'response': execution.response_body,
                    'execution_id': str(execution.id),
                }
            else:
                execution.status = 'failed'
                execution.error_type = 'api_error'
                execution.error_message = f"HTTP {response.status_code}: {response.text[:500]}"
                self.db.commit()

                return {
                    'status': 'failed',
                    'error': execution.error_message,
                }

        except httpx.TimeoutException:
            execution.status = 'timeout'
            execution.error_type = 'timeout'
            execution.error_message = f"Request timed out after {action.timeout_seconds}s"
            execution.completed_at = datetime.utcnow()
            self.db.commit()

            return {
                'status': 'timeout',
                'error': 'The request timed out. Please try again.',
            }

        except Exception as e:
            execution.status = 'failed'
            execution.error_type = 'network_error'
            execution.error_message = str(e)
            execution.completed_at = datetime.utcnow()
            self.db.commit()

            logger.error(f"Action execution failed: {e}")
            return {
                'status': 'failed',
                'error': 'An error occurred while executing the action.',
            }

    def _validate_params(self, params: dict, schema: dict) -> list[str]:
        """Validate params against JSON schema."""
        from jsonschema import validate, ValidationError

        try:
            validate(instance=params, schema=schema)
            return []
        except ValidationError as e:
            return [e.message]

    def _format_success_message(self, template: str, response_data: dict) -> str:
        """Format success message with response data."""
        if not template:
            return "Action completed successfully!"

        try:
            return template.format(**response_data)
        except KeyError:
            return template
```

---

## 6. Security & Validation

### 6.1 Input Validation

```python
from jsonschema import validate, ValidationError, Draft7Validator

class ActionValidator:
    """Validates action configurations and execution params."""

    @staticmethod
    def validate_action_config(action_data: dict) -> list[str]:
        """Validate action configuration before saving."""
        errors = []

        # Validate endpoint URL
        if not action_data.get('endpoint_url', '').startswith('https://'):
            errors.append("Endpoint URL must use HTTPS")

        # Validate request schema is valid JSON Schema
        try:
            Draft7Validator.check_schema(action_data.get('request_schema', {}))
        except Exception as e:
            errors.append(f"Invalid request schema: {e}")

        # Validate response schema if provided
        if action_data.get('response_schema'):
            try:
                Draft7Validator.check_schema(action_data['response_schema'])
            except Exception as e:
                errors.append(f"Invalid response schema: {e}")

        return errors

    @staticmethod
    def sanitize_user_input(params: dict, schema: dict) -> dict:
        """Sanitize user-provided parameters."""
        sanitized = {}
        properties = schema.get('properties', {})

        for key, value in params.items():
            if key not in properties:
                continue  # Ignore extra fields

            field_type = properties[key].get('type')

            # Type coercion and sanitization
            if field_type == 'string':
                sanitized[key] = str(value).strip()[:1000]  # Max length
            elif field_type == 'integer':
                try:
                    sanitized[key] = int(value)
                except (ValueError, TypeError):
                    pass
            elif field_type == 'boolean':
                sanitized[key] = bool(value)
            else:
                sanitized[key] = value

        return sanitized
```

### 6.2 Encryption for Auth Tokens

```python
# app/utils/encryption.py

from cryptography.fernet import Fernet
from app.core.config import settings

cipher_suite = Fernet(settings.ENCRYPTION_KEY.encode())

def encrypt_value(plain_text: str) -> str:
    """Encrypt sensitive value."""
    return cipher_suite.encrypt(plain_text.encode()).decode()

def decrypt_value(encrypted_text: str) -> str:
    """Decrypt sensitive value."""
    return cipher_suite.decrypt(encrypted_text.encode()).decode()
```

### 6.3 Rate Limiting

```python
from app.utils.rate_limiter import RateLimiter

# Per-action rate limiter
action_rate_limiter = RateLimiter(
    max_requests=100,  # 100 executions
    window_seconds=3600,  # per hour
    key_prefix="action_execution"
)

# In ActionExecutionService.execute_action()
async def execute_action(self, action: TenantAction, session_id: UUID, params: dict):
    # Check rate limit
    key = f"{action.tenant_id}:{action.id}"
    await action_rate_limiter.acquire(key)

    # ... rest of execution
```

### 6.4 Prompt Injection Prevention

```python
def detect_prompt_injection(user_input: str) -> bool:
    """Detect potential prompt injection attempts."""
    dangerous_patterns = [
        r'ignore\s+previous\s+instructions',
        r'disregard\s+all\s+rules',
        r'you\s+are\s+now',
        r'execute\s+action',
        r'call\s+function',
        r'bypass\s+validation',
    ]

    input_lower = user_input.lower()
    for pattern in dangerous_patterns:
        if re.search(pattern, input_lower):
            logger.warning(f"Potential prompt injection detected: {user_input[:100]}")
            return True

    return False
```

---

## 7. Admin UI Requirements

### 7.1 Actions Dashboard

**Location**: Settings → AI Actions

**Components**:
1. **Actions List**
   - Table showing all configured actions
   - Columns: Name, Status, Executions, Success Rate, Last Used
   - Quick enable/disable toggle
   - Delete button

2. **Create/Edit Action Form**
   - Action name (auto-generated from display name)
   - Display name
   - Description (help text: "Explain to AI when to use this")
   - API endpoint URL
   - HTTP method dropdown
   - Authentication section:
     - Auth type: Bearer Token / API Key / Basic / Custom
     - Token input (masked)
   - Custom headers (key-value pairs)
   - Request schema editor (JSON with syntax highlighting)
   - Response schema editor (optional)
   - Confirmation settings:
     - Checkbox: "Require user confirmation before executing"
     - Success message template input
   - Test section:
     - Sample parameters input
     - "Test Action" button
     - Response preview

3. **Action Analytics**
   - Total executions
   - Success rate chart
   - Average response time
   - Recent failures list

4. **Execution Logs**
   - Filter by status, date range
   - Show: timestamp, parameters, response, duration
   - Export to CSV

### 7.2 Wireframe Example

```
┌─────────────────────────────────────────────────────────┐
│ Settings > AI Actions                         [+ New]   │
├─────────────────────────────────────────────────────────┤
│                                                          │
│ ┌────────────────────────────────────────────────────┐ │
│ │ Action Name     Status    Executions  Success Rate │ │
│ ├────────────────────────────────────────────────────┤ │
│ │ Book Appt      ✅ Active      145        98%       │ │
│ │ Track Order    ✅ Active       87        100%      │ │
│ │ Cancel Sub     ⛔ Disabled      0         -        │ │
│ └────────────────────────────────────────────────────┘ │
│                                                          │
│ Click action to edit, configure, or view logs.          │
└─────────────────────────────────────────────────────────┘
```

---

## 8. Implementation Phases

### Phase 1: Database & Core Models (Week 1)
- ✅ Create TenantAction model
- ✅ Create ActionExecution model
- ✅ Update Tenant relationships
- ✅ Add capability flag
- ✅ Write database migrations
- ✅ Create Pydantic schemas

### Phase 2: Backend Services (Week 2)
- ✅ ActionService (CRUD for actions)
- ✅ ActionExecutionService (execute actions, HTTP client)
- ✅ ActionValidator (schema validation)
- ✅ Encryption utilities for auth tokens
- ✅ Rate limiting
- ✅ Unit tests

### Phase 3: API Endpoints (Week 3)
- ✅ Action management endpoints
- ✅ Execution logs endpoints
- ✅ Test action endpoint
- ✅ Integration tests
- ✅ API documentation

### Phase 4: AI Integration (Week 4)
- ✅ Update RAGService to support function calling
- ✅ Inject actions into prompt
- ✅ Handle multi-turn parameter collection
- ✅ Update chat endpoint to detect action calls
- ✅ Handle confirmation flow
- ✅ Format success messages

### Phase 5: Admin UI (Week 5)
- ✅ Actions list page
- ✅ Create/edit action form
- ✅ JSON schema editor
- ✅ Test action interface
- ✅ Execution logs viewer

### Phase 6: Testing & Polish (Week 6)
- ✅ End-to-end testing
- ✅ Security review
- ✅ Performance testing
- ✅ Documentation
- ✅ Example actions templates
- ✅ Launch prep

---

## 9. Use Cases & Examples

### Use Case 1: Salon Booking System

**Tenant**: Bella's Hair Salon

**Action Configuration**:
```json
{
  "name": "book_appointment",
  "display_name": "Book Appointment",
  "description": "Use this to book hair salon appointments. Ask for service type, preferred date, time, and customer email.",
  "endpoint_url": "https://api.bella-salon.com/bookings",
  "http_method": "POST",
  "auth_type": "bearer",
  "request_schema": {
    "type": "object",
    "required": ["service", "date", "time", "email", "phone"],
    "properties": {
      "service": {
        "type": "string",
        "enum": ["haircut", "coloring", "styling", "treatment"],
        "description": "Type of service"
      },
      "date": {
        "type": "string",
        "format": "date",
        "description": "Appointment date (YYYY-MM-DD)"
      },
      "time": {
        "type": "string",
        "pattern": "^([0-1][0-9]|2[0-3]):[0-5][0-9]$",
        "description": "Appointment time (HH:MM)"
      },
      "email": {
        "type": "string",
        "format": "email"
      },
      "phone": {
        "type": "string",
        "pattern": "^\\+?[1-9]\\d{9,14}$"
      }
    }
  },
  "response_schema": {
    "type": "object",
    "properties": {
      "success": {"type": "boolean"},
      "booking_id": {"type": "string"},
      "confirmation_url": {"type": "string"}
    }
  },
  "requires_confirmation": true,
  "success_message_template": "Perfect! Your {service} appointment is booked for {date} at {time}. Booking ID: {booking_id}"
}
```

**Conversation Flow**:
```
User: "I want to book a haircut"

AI: "I can help book that! What date works for you?"

User: "Next Friday"

AI: "What time would you prefer?"

User: "2pm"

AI: "Great! What's your email address?"

User: "sarah@email.com"

AI: "And your phone number?"

User: "+1234567890"

AI: "Let me confirm: Haircut appointment on 2026-01-24 at 14:00. Should I book this?"

User: "Yes"

AI: [calls API]
    "Perfect! Your haircut appointment is booked for 2026-01-24 at 14:00.
     Booking ID: APT-789. You'll receive a confirmation email shortly."
```

---

### Use Case 2: E-commerce Order Tracking

**Tenant**: TechGadgets Store

**Action Configuration**:
```json
{
  "name": "track_order",
  "display_name": "Track Order",
  "description": "Use this to check order status and tracking information. Ask for order number.",
  "endpoint_url": "https://api.techgadgets.com/orders/{order_number}/status",
  "http_method": "GET",
  "auth_type": "api_key",
  "request_schema": {
    "type": "object",
    "required": ["order_number"],
    "properties": {
      "order_number": {
        "type": "string",
        "pattern": "^ORD-[0-9]{6}$",
        "description": "Order number (format: ORD-123456)"
      }
    }
  },
  "requires_confirmation": false,
  "success_message_template": "Your order {order_number} is {status}. Expected delivery: {estimated_delivery}"
}
```

**Conversation Flow**:
```
User: "Where's my order?"

AI: "I can check that for you! What's your order number?"

User: "ORD-123456"

AI: [calls API without confirmation since it's read-only]
    "Your order ORD-123456 is Out for Delivery. Expected delivery: Today by 8pm.
     Tracking: https://track.com/ORD-123456"
```

---

### Use Case 3: Subscription Management

**Tenant**: StreamFlix

**Action Configuration**:
```json
{
  "name": "cancel_subscription",
  "display_name": "Cancel Subscription",
  "description": "Use this to cancel user subscriptions. ONLY use if user explicitly says they want to cancel. Ask for account email.",
  "endpoint_url": "https://api.streamflix.com/subscriptions/cancel",
  "http_method": "POST",
  "auth_type": "bearer",
  "request_schema": {
    "type": "object",
    "required": ["email", "reason"],
    "properties": {
      "email": {
        "type": "string",
        "format": "email"
      },
      "reason": {
        "type": "string",
        "enum": ["too_expensive", "not_using", "technical_issues", "other"],
        "description": "Cancellation reason"
      }
    }
  },
  "requires_confirmation": true,
  "confirmation_message": "Are you sure you want to cancel your subscription? This will take effect at the end of your billing period.",
  "success_message_template": "Your subscription has been cancelled. You'll have access until {end_date}. We're sorry to see you go!"
}
```

---

## 10. Rollout Strategy

### Beta Testing (Week 7-8)
- Select 3-5 pilot tenants
- Provide hands-on onboarding
- Gather feedback on UX
- Monitor execution logs for issues
- Iterate on pain points

### General Availability (Week 9+)
- Announce as premium feature
- Pricing: $99/month for Actions tier (unlimited actions)
- Create tutorial videos
- Add to documentation
- Marketing: "AI that takes action, not just answers"

### Success Metrics
- **Adoption**: % of tenants who configure at least 1 action
- **Usage**: Average executions per action per day
- **Success Rate**: % of executions that complete successfully
- **Customer Feedback**: NPS score for Actions feature
- **Revenue Impact**: Upgrades to Actions tier

---

## 11. Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Tenant API downtime breaks actions | High | Implement retries, timeout handling, graceful degradation |
| User provides malicious input | High | Strict schema validation, input sanitization, rate limiting |
| AI calls wrong action | Medium | Clear action descriptions, confirmation flow for destructive actions |
| Slow API responses degrade UX | Medium | Async execution, timeout limits, loading indicators |
| Security breach exposes auth tokens | Critical | Encrypt tokens at rest, use short-lived tokens when possible, audit logs |
| Complex configuration confuses users | Medium | Templates for common actions, onboarding videos, good defaults |

---

## 12. Future Enhancements

### Phase 2 Features (6+ months out)
- **Conditional Actions**: "If order total > $100, apply discount"
- **Chained Actions**: Execute multiple actions in sequence
- **Webhooks (Inbound)**: Tenant systems notify Luran of events
- **Action Templates Library**: Pre-built integrations (Shopify, Calendly, Stripe)
- **No-Code Builder**: Visual action builder without JSON
- **Testing Sandbox**: Safe environment to test actions with mock data

---

## 13. Conclusion

AI Actions transforms Luran from a knowledge assistant into a true AI agent capable of completing tasks autonomously. This positions Luran as a premium, enterprise-grade platform with significant competitive differentiation.

**Recommendation**: Implement post-launch as premium feature after validating core product market fit.

**Timeline**: 6 weeks from kickoff to GA
**Team**: 1 backend engineer, 1 frontend engineer, 1 designer (part-time)
**Investment**: ~$30-40K in engineering time
**Expected ROI**: 30-40% of customers upgrade to Actions tier → $30-40K MRR at 100 customers

---

**Document Status**: ✅ Complete
**Last Updated**: 2026-01-21
**Next Review**: After core product launch

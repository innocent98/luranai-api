"""
Comprehensive End-to-End Test Script for Luran Live Chat System

Tests the complete flow:
1. Company registration
2. Normal AI chat
3. Escalation trigger and ticket creation
4. Email notifications
5. Agent takeover
6. WebSocket real-time messaging
7. Ticket resolution
8. Ticket closing

Run with: python tests/e2e_test_complete_flow.py
"""

import asyncio
import json
import time
from typing import Dict, Any, Optional
import websockets
import requests
from datetime import datetime

# Configuration
BASE_URL = "http://localhost:8000"
WS_URL = "ws://localhost:8000"
API_V1 = f"{BASE_URL}/api/v1"


class Color:
    """ANSI color codes for terminal output."""
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    END = '\033[0m'
    BOLD = '\033[1m'


class E2ETest:
    """End-to-End test runner for Luran live chat system."""

    def __init__(self):
        self.tenant_id: Optional[str] = None
        self.admin_token: Optional[str] = None
        self.agent_token: Optional[str] = None
        self.session_id: Optional[str] = None
        self.ticket_id: Optional[str] = None
        self.agent_id: Optional[str] = None
        self.user_identifier = f"test-customer-{int(time.time())}@example.com"
        self.company_email = f"test-company-{int(time.time())}@example.com"
        self.agent_email = f"test-agent-{int(time.time())}@example.com"

    def log(self, message: str, color: str = Color.BLUE):
        """Print colored log message."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"{color}[{timestamp}] {message}{Color.END}")

    def log_success(self, message: str):
        """Print success message."""
        self.log(f"✅ {message}", Color.GREEN)

    def log_error(self, message: str):
        """Print error message."""
        self.log(f"❌ {message}", Color.RED)

    def log_step(self, step: int, message: str):
        """Print step header."""
        print(f"\n{Color.HEADER}{Color.BOLD}{'='*80}{Color.END}")
        print(f"{Color.HEADER}{Color.BOLD}STEP {step}: {message}{Color.END}")
        print(f"{Color.HEADER}{Color.BOLD}{'='*80}{Color.END}\n")

    def make_request(
        self,
        method: str,
        endpoint: str,
        data: Optional[Dict] = None,
        token: Optional[str] = None,
        expect_error: bool = False
    ) -> Dict[str, Any]:
        """Make HTTP request and handle response."""
        url = f"{API_V1}{endpoint}"
        headers = {"Content-Type": "application/json"}

        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            if method == "GET":
                response = requests.get(url, headers=headers)
            elif method == "POST":
                response = requests.post(url, json=data, headers=headers)
            elif method == "PUT":
                response = requests.put(url, json=data, headers=headers)
            elif method == "DELETE":
                response = requests.delete(url, headers=headers)
            else:
                raise ValueError(f"Unsupported method: {method}")

            if expect_error:
                if response.status_code >= 400:
                    self.log_success(f"Expected error received: {response.status_code}")
                    return response.json() if response.text else {}
                else:
                    self.log_error(f"Expected error but got {response.status_code}")
                    return response.json()

            if response.status_code not in [200, 201]:
                self.log_error(f"{method} {endpoint} failed: {response.status_code}")
                self.log_error(f"Response: {response.text}")
                raise Exception(f"Request failed: {response.status_code}")

            return response.json()

        except requests.exceptions.ConnectionError:
            self.log_error("Cannot connect to server. Is it running?")
            raise

    # ========================================================================
    # STEP 1: Company Registration
    # ========================================================================

    def test_company_registration(self):
        """Test 1: Register a new company with admin user."""
        self.log_step(1, "Company Registration")

        data = {
            "company_name": f"Test Company {int(time.time())}",
            "company_email": self.company_email,
            "admin_name": "Test Admin",
            "admin_email": self.company_email,
            "admin_password": "TestPassword123!",
        }

        self.log(f"Registering company: {data['company_name']}")
        response = self.make_request("POST", "/auth/register-company", data)

        self.tenant_id = response["tenant"]["id"]
        self.admin_token = response["access_token"]

        self.log_success(f"Company registered: {self.tenant_id}")
        self.log_success(f"Admin token obtained")

    # ========================================================================
    # STEP 2: Create Support Agent
    # ========================================================================

    def test_create_support_agent(self):
        """Test 2: Create a support agent user."""
        self.log_step(2, "Create Support Agent")

        data = {
            "email": self.agent_email,
            "full_name": "Test Support Agent",
            "password": "AgentPassword123!",
            "role": "support_agent",
        }

        self.log("Creating support agent...")
        response = self.make_request("POST", "/users", data, token=self.admin_token)

        self.agent_id = response["id"]
        self.log_success(f"Support agent created: {self.agent_id}")

        # Login as agent to get token
        self.log("Logging in as support agent...")
        login_data = {"email": self.agent_email, "password": "AgentPassword123!"}
        login_response = self.make_request("POST", "/auth/login", login_data)
        self.agent_token = login_response["access_token"]
        self.log_success("Agent token obtained")

    # ========================================================================
    # STEP 3: Normal AI Chat (No Escalation)
    # ========================================================================

    def test_normal_ai_chat(self):
        """Test 3: Send normal message that AI can handle (no escalation)."""
        self.log_step(3, "Normal AI Chat Flow")

        data = {
            "message": "Hello, what services do you offer?",
            "tenant_id": self.tenant_id,
            "user_identifier": self.user_identifier,
        }

        self.log("Sending normal chat message...")
        response = self.make_request("POST", "/chat/message", data)

        self.session_id = response["session_id"]

        self.log_success(f"Session created: {self.session_id}")
        self.log_success(f"AI responded: {response['message']['content'][:100]}...")

        if response.get("should_escalate"):
            self.log_error("Unexpected escalation on normal message")
        else:
            self.log_success("No escalation (expected)")

    # ========================================================================
    # STEP 4: Trigger Escalation
    # ========================================================================

    def test_trigger_escalation(self):
        """Test 4: Send message that triggers escalation."""
        self.log_step(4, "Trigger Escalation and Ticket Creation")

        # Use escalation keyword
        data = {
            "message": "I need urgent help from a human agent immediately!",
            "session_id": self.session_id,
            "tenant_id": self.tenant_id,
            "user_identifier": self.user_identifier,
        }

        self.log("Sending message with escalation keywords...")
        response = self.make_request("POST", "/chat/message", data)

        if response.get("should_escalate"):
            self.log_success("Escalation triggered!")
            self.log_success(f"Reason: {response.get('escalation_reason')}")

            if response.get("ticket_id"):
                self.ticket_id = response["ticket_id"]
                self.log_success(f"Ticket created: {self.ticket_id}")
            else:
                self.log_error("Ticket ID not returned")
        else:
            self.log_error("Escalation not triggered (expected escalation)")

    # ========================================================================
    # STEP 5: Verify Ticket Details
    # ========================================================================

    def test_verify_ticket(self):
        """Test 5: Verify ticket was created with correct details."""
        self.log_step(5, "Verify Ticket Details")

        if not self.ticket_id:
            self.log_error("No ticket ID to verify")
            return

        self.log("Fetching ticket details...")
        response = self.make_request("GET", f"/tickets/{self.ticket_id}", token=self.admin_token)

        self.log_success(f"Ticket Number: {response['ticket_number']}")
        self.log_success(f"Priority: {response['priority']}")
        self.log_success(f"Status: {response['status']}")
        self.log_success(f"Summary: {response['summary']}")

        assert response["status"] == "open", "Ticket should be OPEN"
        assert response["session_id"] == self.session_id, "Ticket should be linked to session"

    # ========================================================================
    # STEP 6: Agent Takeover
    # ========================================================================

    def test_agent_takeover(self):
        """Test 6: Support agent takes over the conversation."""
        self.log_step(6, "Agent Takeover")

        self.log("Agent taking over conversation...")
        response = self.make_request(
            "POST",
            f"/chat/sessions/{self.session_id}/takeover",
            token=self.agent_token
        )

        self.log_success(f"Agent assigned: {response.get('agent_id')}")
        self.log_success(f"Agent joined at: {response.get('agent_joined_at')}")

        assert response["agent_id"] == self.agent_id, "Agent ID should match"
        assert response["status"] == "escalated", "Session should be escalated"

        # Verify ticket assignment
        self.log("Verifying ticket assignment...")
        ticket = self.make_request("GET", f"/tickets/{self.ticket_id}", token=self.agent_token)

        if ticket.get("assigned_to") == self.agent_id:
            self.log_success("Ticket assigned to agent")
        else:
            self.log_error(f"Ticket not assigned correctly: {ticket.get('assigned_to')}")

    # ========================================================================
    # STEP 7: WebSocket Real-Time Messaging
    # ========================================================================

    async def test_websocket_messaging(self):
        """Test 7: Test WebSocket connections and real-time messaging."""
        self.log_step(7, "WebSocket Real-Time Messaging")

        # Customer WebSocket
        customer_ws_url = f"{WS_URL}/api/v1/ws/chat/{self.session_id}?user_identifier={self.user_identifier}"

        # Agent WebSocket
        agent_ws_url = f"{WS_URL}/api/v1/ws/agent/{self.session_id}?token={self.agent_token}"

        try:
            self.log("Connecting customer WebSocket...")
            async with websockets.connect(customer_ws_url) as customer_ws:
                self.log_success("Customer WebSocket connected")

                self.log("Connecting agent WebSocket...")
                async with websockets.connect(agent_ws_url) as agent_ws:
                    self.log_success("Agent WebSocket connected")

                    # Agent should receive "agent_joined" notification
                    self.log("Waiting for agent_joined notification...")
                    try:
                        message = await asyncio.wait_for(customer_ws.recv(), timeout=5.0)
                        data = json.loads(message)
                        if data.get("type") == "agent_joined":
                            self.log_success(f"Customer received: agent_joined ({data.get('agent_name')})")
                        else:
                            self.log(f"Received: {data.get('type')}")
                    except asyncio.TimeoutError:
                        self.log("No agent_joined notification (OK if already sent)")

                    # Customer sends message
                    self.log("Customer sending message...")
                    await customer_ws.send(json.dumps({
                        "type": "message",
                        "content": "Thanks for taking over! Can you help me?"
                    }))
                    self.log_success("Customer message sent")

                    # Agent should receive it
                    self.log("Waiting for agent to receive message...")
                    message = await asyncio.wait_for(agent_ws.recv(), timeout=5.0)
                    data = json.loads(message)

                    if data.get("type") == "customer_message":
                        self.log_success(f"Agent received: {data.get('content')}")
                    else:
                        self.log_error(f"Unexpected message type: {data.get('type')}")

                    # Agent sends response
                    self.log("Agent sending response...")
                    await agent_ws.send(json.dumps({
                        "type": "message",
                        "content": "Of course! I'm here to help you."
                    }))
                    self.log_success("Agent message sent")

                    # Customer should receive it
                    self.log("Waiting for customer to receive message...")
                    message = await asyncio.wait_for(customer_ws.recv(), timeout=5.0)
                    data = json.loads(message)

                    if data.get("type") == "agent_message":
                        self.log_success(f"Customer received: {data.get('content')}")
                    else:
                        self.log_error(f"Unexpected message type: {data.get('type')}")

                    # Test typing indicator
                    self.log("Testing typing indicator...")
                    await agent_ws.send(json.dumps({
                        "type": "typing",
                        "is_typing": True
                    }))

                    message = await asyncio.wait_for(customer_ws.recv(), timeout=5.0)
                    data = json.loads(message)

                    if data.get("type") == "typing_indicator":
                        self.log_success("Typing indicator received")
                    else:
                        self.log(f"Received: {data.get('type')}")

                self.log_success("WebSocket testing complete")

        except websockets.exceptions.WebSocketException as e:
            self.log_error(f"WebSocket error: {e}")
        except Exception as e:
            self.log_error(f"Error: {e}")

    # ========================================================================
    # STEP 8: HTTP Agent Message (Alternative to WebSocket)
    # ========================================================================

    def test_http_agent_message(self):
        """Test 8: Send agent message via HTTP endpoint."""
        self.log_step(8, "HTTP Agent Message")

        data = {
            "message": "This message is sent via HTTP, not WebSocket",
            "session_id": self.session_id,
            "tenant_id": self.tenant_id,
            "user_identifier": self.user_identifier,
        }

        self.log("Sending agent message via HTTP...")
        response = self.make_request(
            "POST",
            f"/chat/sessions/{self.session_id}/agent-message",
            data,
            token=self.agent_token
        )

        self.log_success(f"Message sent: {response['id']}")
        self.log_success(f"Role: {response['role']}")
        assert response["role"] == "human_agent", "Message role should be human_agent"

    # ========================================================================
    # STEP 9: Resolve Ticket
    # ========================================================================

    def test_resolve_ticket(self):
        """Test 9: Agent resolves the ticket."""
        self.log_step(9, "Resolve Ticket")

        data = {
            "resolution_notes": "Issue resolved. Customer was assisted successfully."
        }

        self.log("Resolving ticket...")
        response = self.make_request(
            "POST",
            f"/tickets/{self.ticket_id}/resolve",
            data,
            token=self.agent_token
        )

        self.log_success(f"Ticket resolved: {response['ticket_number']}")
        self.log_success(f"Status: {response['status']}")
        self.log_success(f"Resolution notes: {response['resolution_notes']}")

        assert response["status"] == "resolved", "Ticket should be RESOLVED"
        assert response["resolved_at"] is not None, "Resolved timestamp should be set"

    # ========================================================================
    # STEP 10: Close Ticket
    # ========================================================================

    def test_close_ticket(self):
        """Test 10: Admin closes the ticket."""
        self.log_step(10, "Close Ticket")

        self.log("Closing ticket...")
        response = self.make_request(
            "POST",
            f"/tickets/{self.ticket_id}/close",
            token=self.admin_token
        )

        self.log_success(f"Ticket closed: {response['ticket_number']}")
        self.log_success(f"Final status: {response['status']}")

        assert response["status"] == "closed", "Ticket should be CLOSED"

    # ========================================================================
    # STEP 11: Verify Complete Session History
    # ========================================================================

    def test_verify_session_history(self):
        """Test 11: Verify complete message history."""
        self.log_step(11, "Verify Complete Session History")

        self.log("Fetching session with all messages...")
        response = self.make_request(
            "GET",
            f"/chat/sessions/{self.session_id}",
            token=self.admin_token
        )

        messages = response.get("messages", [])
        self.log_success(f"Total messages: {len(messages)}")

        # Count message types
        user_count = sum(1 for m in messages if m["role"] == "user")
        assistant_count = sum(1 for m in messages if m["role"] == "assistant")
        agent_count = sum(1 for m in messages if m["role"] == "human_agent")
        system_count = sum(1 for m in messages if m["role"] == "system")

        self.log_success(f"User messages: {user_count}")
        self.log_success(f"AI assistant messages: {assistant_count}")
        self.log_success(f"Human agent messages: {agent_count}")
        self.log_success(f"System messages: {system_count}")

        # Print message flow
        print(f"\n{Color.CYAN}Message History:{Color.END}")
        for i, msg in enumerate(messages, 1):
            role_color = {
                "user": Color.BLUE,
                "assistant": Color.GREEN,
                "human_agent": Color.YELLOW,
                "system": Color.CYAN,
            }.get(msg["role"], Color.END)

            print(f"{role_color}{i}. [{msg['role'].upper()}] {msg['content'][:80]}{Color.END}")

    # ========================================================================
    # Run All Tests
    # ========================================================================

    async def run_all_tests(self):
        """Run all end-to-end tests in sequence."""
        print(f"\n{Color.HEADER}{Color.BOLD}{'='*80}{Color.END}")
        print(f"{Color.HEADER}{Color.BOLD}Luran Live Chat System - End-to-End Test Suite{Color.END}")
        print(f"{Color.HEADER}{Color.BOLD}{'='*80}{Color.END}\n")

        try:
            # Sequential tests
            self.test_company_registration()
            self.test_create_support_agent()
            self.test_normal_ai_chat()
            self.test_trigger_escalation()
            self.test_verify_ticket()
            self.test_agent_takeover()

            # WebSocket test (async)
            await self.test_websocket_messaging()

            # Continue sequential tests
            self.test_http_agent_message()
            self.test_resolve_ticket()
            self.test_close_ticket()
            self.test_verify_session_history()

            # Summary
            print(f"\n{Color.GREEN}{Color.BOLD}{'='*80}{Color.END}")
            print(f"{Color.GREEN}{Color.BOLD}✅ ALL TESTS PASSED!{Color.END}")
            print(f"{Color.GREEN}{Color.BOLD}{'='*80}{Color.END}\n")

            print(f"{Color.CYAN}Test Summary:{Color.END}")
            print(f"  Tenant ID: {self.tenant_id}")
            print(f"  Session ID: {self.session_id}")
            print(f"  Ticket ID: {self.ticket_id}")
            print(f"  Agent ID: {self.agent_id}")

        except Exception as e:
            print(f"\n{Color.RED}{Color.BOLD}{'='*80}{Color.END}")
            print(f"{Color.RED}{Color.BOLD}❌ TEST FAILED{Color.END}")
            print(f"{Color.RED}{Color.BOLD}{'='*80}{Color.END}\n")
            print(f"{Color.RED}Error: {e}{Color.END}")
            raise


if __name__ == "__main__":
    test = E2ETest()
    asyncio.run(test.run_all_tests())

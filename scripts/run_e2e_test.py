"""
Simple End-to-End Test with Existing Users

Run with: poetry run python scripts/run_e2e_test.py
"""

import asyncio
import json
import time
import requests
import websockets
from datetime import datetime

# Configuration
BASE_URL = "http://localhost:8000"
WS_URL = "ws://localhost:8000"
API_V1 = f"{BASE_URL}/api/v1"

# Colors
GREEN = '\033[92m'
RED = '\033[91m'
BLUE = '\033[94m'
YELLOW = '\033[93m'
END = '\033[0m'
BOLD = '\033[1m'

# Test data
TENANT_ID = "bb24bf9e-63c2-4497-b5f5-e46dd916f7ea"
ADMIN_EMAIL = "admin@weysure.com"
AGENT_EMAIL = "adevictor98@gmail.com"
USER_IDENTIFIER = f"test-customer-{int(time.time())}@example.com"

# Tokens (will be filled after login)
admin_token = None
agent_token = None
agent_id = None
session_id = None
ticket_id = None


def log(message, color=BLUE):
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"{color}[{timestamp}] {message}{END}")


def log_success(message):
    log(f"✅ {message}", GREEN)


def log_error(message):
    log(f"❌ {message}", RED)


def log_step(step, message):
    print(f"\n{BOLD}{'='*80}{END}")
    print(f"{BOLD}STEP {step}: {message}{END}")
    print(f"{BOLD}{'='*80}{END}\n")


def make_request(method, endpoint, data=None, token=None):
    """Make HTTP request."""
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
        else:
            raise ValueError(f"Unsupported method: {method}")

        if response.status_code not in [200, 201]:
            log_error(f"{method} {endpoint} failed: {response.status_code}")
            log_error(f"Response: {response.text}")
            return None

        return response.json()

    except requests.exceptions.ConnectionError:
        log_error("Cannot connect to server. Is it running?")
        return None


# ==============================================================================
# STEP 1: Login
# ==============================================================================

def test_login():
    global admin_token, agent_token, agent_id

    log_step(1, "Login with Existing Users")

    # Get passwords from environment or command-line
    import sys
    import os

    admin_password = os.getenv("ADMIN_PASSWORD")
    agent_password = os.getenv("AGENT_PASSWORD")

    if not admin_password or not agent_password:
        if len(sys.argv) < 3:
            print("Usage: python scripts/run_e2e_test.py <admin_password> <agent_password>")
            print("   OR: ADMIN_PASSWORD=xxx AGENT_PASSWORD=yyy python scripts/run_e2e_test.py")
            sys.exit(1)
        admin_password = sys.argv[1]
        agent_password = sys.argv[2]

    # Login as admin
    log("Logging in as admin...")
    admin_response = make_request("POST", "/auth/login", {
        "email": ADMIN_EMAIL,
        "password": admin_password
    })

    if not admin_response:
        log_error("Admin login failed")
        return False

    admin_token = admin_response["access_token"]
    log_success(f"Admin logged in: {ADMIN_EMAIL}")

    # Login as agent
    log("Logging in as agent...")
    agent_response = make_request("POST", "/auth/login", {
        "email": AGENT_EMAIL,
        "password": agent_password
    })

    if not agent_response:
        log_error("Agent login failed")
        return False

    agent_token = agent_response["access_token"]

    # Get agent user info
    log("Getting agent user info...")
    agent_user = make_request("GET", "/auth/me", token=agent_token)
    if not agent_user:
        log_error("Failed to get agent user info")
        return False

    agent_id = agent_user["id"]
    log_success(f"Agent logged in: {AGENT_EMAIL} (ID: {agent_id})")

    return True


# ==============================================================================
# STEP 2: Normal AI Chat
# ==============================================================================

def test_normal_chat():
    global session_id

    log_step(2, "Normal AI Chat (No Escalation)")

    log("Sending normal message...")
    response = make_request("POST", "/chat/message", {
        "message": "What services do you offer?",
        "tenant_id": TENANT_ID,
        "user_identifier": USER_IDENTIFIER
    })

    if not response:
        return False

    session_id = response["session_id"]
    log_success(f"Session created: {session_id}")
    log_success(f"AI responded: {response['message']['content'][:100]}...")

    if response.get("should_escalate"):
        log(f"⚠️  Escalation on normal message (possibly due to low confidence/no knowledge base)", YELLOW)
        log(f"Reason: {response.get('escalation_reason')}", YELLOW)
        log("This is OK - we'll test explicit escalation next", YELLOW)
    else:
        log_success("No escalation (as expected)")

    return True


# ==============================================================================
# STEP 3: Trigger Escalation
# ==============================================================================

def test_escalation():
    global ticket_id

    log_step(3, "Trigger Escalation with Urgent Keyword")

    log("Sending message with escalation keywords...")
    response = make_request("POST", "/chat/message", {
        "message": "I need urgent help from a human agent immediately!",
        "session_id": session_id,
        "tenant_id": TENANT_ID,
        "user_identifier": USER_IDENTIFIER
    })

    if not response:
        return False

    if not response.get("should_escalate"):
        log_error("Escalation not triggered (expected escalation)")
        return False

    log_success("Escalation triggered!")
    log_success(f"Reason: {response.get('escalation_reason')}")

    # Debug: print full response
    log(f"Full response: {json.dumps(response, indent=2)}", YELLOW)

    if response.get("ticket_id"):
        ticket_id = response["ticket_id"]
        log_success(f"Ticket created: {ticket_id}")
    else:
        log_error("Ticket ID not returned - checking if ticket creation failed")
        log("This might be due to an error in ticket creation. Check server logs.", YELLOW)
        return False

    return True


# ==============================================================================
# STEP 4: Verify Ticket
# ==============================================================================

def test_verify_ticket():
    log_step(4, "Verify Ticket Details")

    log("Fetching ticket details...")
    response = make_request("GET", f"/tickets/{ticket_id}", token=admin_token)

    if not response:
        return False

    log_success(f"Ticket Number: {response['ticket_number']}")
    log_success(f"Priority: {response['priority']}")
    log_success(f"Status: {response['status']}")
    log_success(f"Summary: {response['summary'][:80]}...")

    if response["status"] != "open":
        log_error(f"Expected status 'open' but got '{response['status']}'")
        return False

    return True


# ==============================================================================
# STEP 5: Agent Takeover
# ==============================================================================

def test_agent_takeover():
    log_step(5, "Agent Takeover")

    log("Agent taking over conversation...")
    response = make_request("POST", f"/chat/sessions/{session_id}/takeover", token=agent_token)

    if not response:
        return False

    log_success(f"Agent assigned: {response.get('agent_id')}")
    log_success(f"Agent joined at: {response.get('agent_joined_at')}")

    if response["agent_id"] != agent_id:
        log_error("Agent ID mismatch")
        return False

    if response["status"] != "escalated":
        log_error(f"Expected status 'escalated' but got '{response['status']}'")
        return False

    # Verify ticket assignment
    log("Verifying ticket assignment...")
    ticket = make_request("GET", f"/tickets/{ticket_id}", token=agent_token)

    if not ticket:
        return False

    if ticket.get("assigned_to") == agent_id:
        log_success("Ticket assigned to agent")
    else:
        log_error(f"Ticket not assigned correctly: {ticket.get('assigned_to')}")
        return False

    return True


# ==============================================================================
# STEP 6: WebSocket Real-Time Messaging
# ==============================================================================

async def test_websocket():
    log_step(6, "WebSocket Real-Time Messaging")

    customer_ws_url = f"{WS_URL}/api/v1/ws/chat/{session_id}?user_identifier={USER_IDENTIFIER}"
    agent_ws_url = f"{WS_URL}/api/v1/ws/agent/{session_id}?token={agent_token}"

    try:
        log("Connecting customer WebSocket...")
        async with websockets.connect(customer_ws_url) as customer_ws:
            log_success("Customer WebSocket connected")

            log("Connecting agent WebSocket...")
            async with websockets.connect(agent_ws_url) as agent_ws:
                log_success("Agent WebSocket connected")

                # Wait for agent_joined notification
                try:
                    message = await asyncio.wait_for(customer_ws.recv(), timeout=3.0)
                    data = json.loads(message)
                    if data.get("type") == "agent_joined":
                        log_success(f"Customer received agent_joined: {data.get('agent_name')}")
                except asyncio.TimeoutError:
                    log("No agent_joined notification (might have been sent earlier)")

                # Customer sends message
                log("Customer sending message...")
                await customer_ws.send(json.dumps({
                    "type": "message",
                    "content": "Thanks for taking over! Can you help me?"
                }))
                log_success("Customer message sent")

                # Agent receives it
                log("Waiting for agent to receive...")
                message = await asyncio.wait_for(agent_ws.recv(), timeout=5.0)
                data = json.loads(message)

                if data.get("type") == "customer_message":
                    log_success(f"Agent received: {data.get('content')[:50]}")
                else:
                    log_error(f"Unexpected type: {data.get('type')}")

                # Agent sends response
                log("Agent sending response...")
                await agent_ws.send(json.dumps({
                    "type": "message",
                    "content": "Of course! I'm here to help you."
                }))
                log_success("Agent message sent")

                # Customer receives it
                log("Waiting for customer to receive...")
                message = await asyncio.wait_for(customer_ws.recv(), timeout=5.0)
                data = json.loads(message)

                if data.get("type") == "agent_message":
                    log_success(f"Customer received: {data.get('content')[:50]}")
                else:
                    log_error(f"Unexpected type: {data.get('type')}")

                log_success("WebSocket messaging works!")
                return True

    except websockets.exceptions.WebSocketException as e:
        log_error(f"WebSocket error: {e}")
        return False
    except Exception as e:
        log_error(f"Error: {e}")
        return False


# ==============================================================================
# STEP 7: HTTP Agent Message
# ==============================================================================

def test_http_agent_message():
    log_step(7, "HTTP Agent Message (Alternative)")

    log("Sending agent message via HTTP...")
    response = make_request("POST", f"/chat/sessions/{session_id}/agent-message", {
        "message": "This is sent via HTTP, not WebSocket",
        "session_id": session_id,
        "tenant_id": TENANT_ID,
        "user_identifier": USER_IDENTIFIER
    }, token=agent_token)

    if not response:
        return False

    log_success(f"Message sent: {response['id']}")
    log_success(f"Role: {response['role']}")

    if response["role"] != "human_agent":
        log_error(f"Expected role 'human_agent' but got '{response['role']}'")
        return False

    return True


# ==============================================================================
# STEP 8: Resolve Ticket
# ==============================================================================

def test_resolve_ticket():
    log_step(8, "Resolve Ticket")

    log("Resolving ticket...")
    response = make_request("POST", f"/tickets/{ticket_id}/resolve", {
        "resolution_notes": "Customer issue resolved successfully via live chat."
    }, token=agent_token)

    if not response:
        return False

    log_success(f"Ticket resolved: {response['ticket_number']}")
    log_success(f"Status: {response['status']}")

    if response["status"] != "resolved":
        log_error(f"Expected status 'resolved' but got '{response['status']}'")
        return False

    return True


# ==============================================================================
# STEP 9: Close Ticket
# ==============================================================================

def test_close_ticket():
    log_step(9, "Close Ticket")

    log("Closing ticket...")
    response = make_request("POST", f"/tickets/{ticket_id}/close", token=admin_token)

    if not response:
        return False

    log_success(f"Ticket closed: {response['ticket_number']}")
    log_success(f"Final status: {response['status']}")

    if response["status"] != "closed":
        log_error(f"Expected status 'closed' but got '{response['status']}'")
        return False

    return True


# ==============================================================================
# STEP 10: Verify Complete History
# ==============================================================================

def test_verify_history():
    log_step(10, "Verify Complete Message History")

    log("Fetching session with all messages...")
    response = make_request("GET", f"/chat/sessions/{session_id}", token=admin_token)

    if not response:
        return False

    messages = response.get("messages", [])
    log_success(f"Total messages: {len(messages)}")

    # Count by role
    user_count = sum(1 for m in messages if m["role"] == "user")
    assistant_count = sum(1 for m in messages if m["role"] == "assistant")
    agent_count = sum(1 for m in messages if m["role"] == "human_agent")
    system_count = sum(1 for m in messages if m["role"] == "system")

    log_success(f"User messages: {user_count}")
    log_success(f"AI assistant messages: {assistant_count}")
    log_success(f"Human agent messages: {agent_count}")
    log_success(f"System messages: {system_count}")

    # Display some messages
    print(f"\n{BLUE}Recent Message History:{END}")
    for i, msg in enumerate(messages[-10:], 1):  # Last 10 messages
        role_colors = {
            "user": BLUE,
            "assistant": GREEN,
            "human_agent": YELLOW,
            "system": END
        }
        color = role_colors.get(msg["role"], END)
        print(f"{color}{i}. [{msg['role'].upper()}] {msg['content'][:80]}{END}")

    return True


# ==============================================================================
# Run All Tests
# ==============================================================================

async def run_all_tests():
    print(f"\n{BOLD}{'='*80}{END}")
    print(f"{BOLD}Luran Live Chat System - End-to-End Test{END}")
    print(f"{BOLD}Using Existing Users{END}")
    print(f"{BOLD}{'='*80}{END}\n")

    try:
        if not test_login():
            raise Exception("Login failed")

        if not test_normal_chat():
            raise Exception("Normal chat failed")

        if not test_escalation():
            raise Exception("Escalation failed")

        if not test_verify_ticket():
            raise Exception("Ticket verification failed")

        if not test_agent_takeover():
            raise Exception("Agent takeover failed")

        if not await test_websocket():
            raise Exception("WebSocket test failed")

        if not test_http_agent_message():
            raise Exception("HTTP agent message failed")

        if not test_resolve_ticket():
            raise Exception("Ticket resolution failed")

        if not test_close_ticket():
            raise Exception("Ticket closing failed")

        if not test_verify_history():
            raise Exception("History verification failed")

        # Summary
        print(f"\n{GREEN}{BOLD}{'='*80}{END}")
        print(f"{GREEN}{BOLD}✅ ALL TESTS PASSED!{END}")
        print(f"{GREEN}{BOLD}{'='*80}{END}\n")

        print(f"Test Summary:")
        print(f"  Tenant ID: {TENANT_ID}")
        print(f"  Session ID: {session_id}")
        print(f"  Ticket ID: {ticket_id}")
        print(f"  Agent ID: {agent_id}")

    except Exception as e:
        print(f"\n{RED}{BOLD}{'='*80}{END}")
        print(f"{RED}{BOLD}❌ TEST FAILED{END}")
        print(f"{RED}{BOLD}{'='*80}{END}\n")
        print(f"{RED}Error: {e}{END}")


if __name__ == "__main__":
    asyncio.run(run_all_tests())

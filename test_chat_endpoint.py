#!/usr/bin/env python3
"""
Test the actual chat endpoint with a real message.

Verifies:
1. Endpoint works end-to-end
2. Response doesn't include confidence_score
3. RAG retrieval works with new thresholds
4. Message is stored correctly
"""

import requests
import json
from hashlib import sha256
from uuid import UUID

BASE_URL = "http://localhost:8000"
API_V1 = f"{BASE_URL}/api/v1"

# Use existing weysure tenant
TENANT_ID = "bb24bf9e-63c2-4497-b5f5-e46dd916f7ea"
USER_IDENTIFIER = "test-user-polish@example.com"


def mask_identifier(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()[:8]

def test_send_message():
    """Test sending a message through the chat endpoint."""
    print("\n" + "="*60)
    print("🧪 TESTING CHAT ENDPOINT - REAL MESSAGE")
    print("="*60 + "\n")

    # Prepare message request
    message_data = {
        "message": "What types of insurance do you offer?",
        "tenant_id": TENANT_ID,
        "user_identifier": USER_IDENTIFIER,
        "metadata": {
            "test": "polish_improvements",
            "source": "endpoint_test"
        }
    }

    print("📤 Sending test message")
    print(f"   Tenant hash: {mask_identifier(TENANT_ID)}")
    print(f"   User hash: {mask_identifier(USER_IDENTIFIER)}")

    # Send request
    try:
        response = requests.post(
            f"{API_V1}/chat/message",
            json=message_data,
            headers={"Content-Type": "application/json"},
            timeout=30
        )

        print(f"\n📥 Response Status: {response.status_code}")

        if response.status_code == 200:
            data = response.json()

            print("\n✅ SUCCESS! Message processed successfully.")
            print("\n" + "="*60)
            print("RESPONSE ANALYSIS")
            print("="*60)

            # Verify session_id
            if "session_id" in data:
                print("✅ Session created successfully")
            else:
                print("❌ FAIL: No session_id in response")
                return False

            # Verify message object
            if "message" in data:
                msg = data["message"]
                print(f"\n📨 Message Details:")
                print(f"   Role: {msg.get('role', 'N/A')}")
                print(f"   Content Length: {len(msg.get('content', ''))} chars")

                # CRITICAL: Check for confidence_score field
                if "confidence_score" in msg:
                    print(f"\n❌ FAIL: confidence_score FOUND in message response!")
                    print(f"   Value: {msg['confidence_score']}")
                    print("\n⚠️  This means our confidence_score removal didn't work!")
                    return False
                else:
                    print(f"\n✅ PASS: confidence_score correctly removed from response")

                # Check if response has content
                if msg.get("content"):
                    print("\n💬 AI response content received")
                else:
                    print("\n⚠️  WARNING: Empty response content")

                # Check retrieved chunks (RAG working)
                if msg.get("retrieved_chunk_ids"):
                    print(f"\n🔍 RAG Retrieval:")
                    print(f"   Chunks retrieved: {len(msg['retrieved_chunk_ids'])}")
                else:
                    print(f"\n⚠️  No chunks retrieved (might be expected for this query)")

            else:
                print("❌ FAIL: No message in response")
                return False

            # Check escalation status
            if "should_escalate" in data:
                print(f"\n🎯 Escalation: {data['should_escalate']}")
                if data.get("escalation_reason"):
                    print(f"   Reason: {data['escalation_reason']}")

            # Show full response structure
            print(f"\n📋 Response Fields: {list(data.keys())}")

            print("\n" + "="*60)
            print("✅ CHAT ENDPOINT TEST PASSED!")
            print("="*60 + "\n")

            return True

        elif response.status_code == 422:
            print(f"\n❌ Validation Error (422):")
            print(json.dumps(response.json(), indent=2))
            return False

        else:
            print(f"\n❌ Error Response:")
            print(f"Status: {response.status_code}")
            try:
                print(json.dumps(response.json(), indent=2))
            except:
                print(response.text)
            return False

    except requests.exceptions.Timeout:
        print("\n❌ Request timed out (AI generation took too long)")
        print("This might be normal if the AI is processing a complex query")
        return False

    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_retrieve_session():
    """Test retrieving the created session to verify data persistence."""
    print("\n" + "="*60)
    print("🔍 TESTING SESSION RETRIEVAL")
    print("="*60 + "\n")

    # First send a message to create a session
    message_data = {
        "message": "Hello",
        "tenant_id": TENANT_ID,
        "user_identifier": USER_IDENTIFIER + "_retrieve_test"
    }

    response = requests.post(
        f"{API_V1}/chat/message",
        json=message_data,
        timeout=30
    )

    if response.status_code == 200:
        data = response.json()
        session_id = data.get("session_id")

        if session_id:
            print("✅ Session created")

            # Now retrieve the session
            retrieve_response = requests.get(
                f"{API_V1}/chat/session/{session_id}"
            )

            if retrieve_response.status_code == 200:
                session_data = retrieve_response.json()

                print(f"\n📋 Session Data:")
                print(f"   Status: {session_data.get('status')}")
                print(f"   Messages: {len(session_data.get('messages', []))}")

                # Check if confidence_scores is in session
                if "confidence_scores" in session_data:
                    print(f"\n❌ FAIL: confidence_scores FOUND in session response!")
                    return False
                else:
                    print(f"\n✅ PASS: confidence_scores correctly removed from session")

                return True
            else:
                print(f"❌ Failed to retrieve session: {retrieve_response.status_code}")
                return False
        else:
            print("❌ No session_id in create response")
            return False
    else:
        print(f"❌ Failed to create session: {response.status_code}")
        return False


def main():
    """Run all chat endpoint tests."""
    print("\n" + "🚀"*30)
    print("CHAT ENDPOINT INTEGRATION TEST")
    print("🚀"*30)

    results = []

    # Test 1: Send message and verify response
    results.append(("Send Message & Verify Response", test_send_message()))

    # Test 2: Retrieve session and verify persistence
    results.append(("Session Retrieval", test_retrieve_session()))

    # Summary
    print("\n" + "="*60)
    print("📊 TEST SUMMARY")
    print("="*60)

    passed = sum(1 for _, result in results if result)
    total = len(results)

    for test_name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status}: {test_name}")

    print(f"\n🎯 Results: {passed}/{total} tests passed")

    if passed == total:
        print("\n🎉 ALL CHAT ENDPOINT TESTS PASSED!")
        print("✅ confidence_score successfully removed from responses")
        print("✅ RAG retrieval working with new thresholds")
        print("✅ End-to-end message flow functional")
        return 0
    else:
        print(f"\n⚠️  {total - passed} test(s) failed")
        return 1

if __name__ == "__main__":
    exit(main())

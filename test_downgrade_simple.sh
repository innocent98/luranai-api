#!/bin/bash

# Simple test script for downgrade endpoint
# Tests the endpoint logic and error handling

echo "========================================"
echo "DOWNGRADE ENDPOINT TESTS"
echo "========================================"
echo

#Test 1: No authentication (should get 403)
echo "Test 1: No authentication"
echo "Expected: 403 Forbidden"
response=$(curl -s -w "\nHTTP_CODE:%{http_code}" -X POST http://localhost:8000/api/v1/subscription/downgrade \
  -H "Content-Type: application/json" \
  -d '{"tier": "lite", "reason": "test"}')

http_code=$(echo "$response" | grep HTTP_CODE | cut -d: -f2)
body=$(echo "$response" | sed '/HTTP_CODE/d')

echo "Status: $http_code"
echo "Response: $body" | python3 -m json.tool 2>/dev/null || echo "$body"
echo

# Test 2: Invalid token (should get 401)
echo "Test 2: Invalid token"
echo "Expected: 401 Unauthorized"
response=$(curl -s -w "\nHTTP_CODE:%{http_code}" -X POST http://localhost:8000/api/v1/subscription/downgrade \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer invalid_token_12345" \
  -d '{"tier": "lite", "reason": "test"}')

http_code=$(echo "$response" | grep HTTP_CODE | cut -d: -f2)
body=$(echo "$response" | sed '/HTTP_CODE/d')

echo "Status: $http_code"
echo "Response: $body" | python3 -m json.tool 2>/dev/null || echo "$body"
echo

# Test 3: Check endpoint exists
echo "Test 3: Verify endpoint is registered"
echo "Checking OpenAPI docs..."
docs_check=$(curl -s http://localhost:8000/docs | grep -o "downgrade" | head -1)

if [ "$docs_check" == "downgrade" ]; then
    echo "✅ Downgrade endpoint found in API docs"
else
    echo "⚠️  Endpoint may not be in docs"
fi

echo
echo "========================================"
echo "Testing complete!"
echo "========================================"
echo
echo "Next steps:"
echo "1. Create a test tenant and user (via database or seed script)"
echo "2. Login to get a valid token"
echo "3. Test full downgrade flow"

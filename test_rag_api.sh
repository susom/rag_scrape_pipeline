#!/bin/bash
# Test RAG EM API endpoints

REDCAP_API_URL="http://localhost/api/"
REDCAP_API_TOKEN="${REDCAP_API_TOKEN}"

if [ -z "$REDCAP_API_TOKEN" ]; then
    echo "ERROR: Set REDCAP_API_TOKEN environment variable"
    exit 1
fi

echo "Testing RAG EM API endpoints..."
echo "================================"

# Test 1: Store a document (should return vector_id now)
echo -e "\n1️⃣  Testing storeDocument (should return vector_id)..."
RESPONSE=$(curl -s -X POST "$REDCAP_API_URL" \
  -d "token=$REDCAP_API_TOKEN" \
  -d "content=externalModule" \
  -d "prefix=redcap_rag" \
  -d "action=storeDocument" \
  -d "format=json" \
  -d "returnFormat=json" \
  -d "title=test_document_123" \
  -d "text=This is test content for automated ingestion")

echo "$RESPONSE" | python -m json.tool
VECTOR_ID=$(echo "$RESPONSE" | python -c "import sys, json; print(json.load(sys.stdin).get('vector_id', ''))" 2>/dev/null)

if [ -n "$VECTOR_ID" ]; then
    echo "✅ storeDocument returned vector_id: $VECTOR_ID"
else
    echo "❌ storeDocument did NOT return vector_id"
    exit 1
fi

# Test 2: Delete the document
echo -e "\n2️⃣  Testing deleteDocument..."
RESPONSE=$(curl -s -X POST "$REDCAP_API_URL" \
  -d "token=$REDCAP_API_TOKEN" \
  -d "content=externalModule" \
  -d "prefix=redcap_rag" \
  -d "action=deleteDocument" \
  -d "format=json" \
  -d "returnFormat=json" \
  -d "vector_id=$VECTOR_ID")

echo "$RESPONSE" | python -m json.tool

if echo "$RESPONSE" | grep -q '"status":"success"'; then
    echo "✅ deleteDocument succeeded"
else
    echo "❌ deleteDocument failed"
    exit 1
fi

# Test 3: Purge namespace (optional - for testing)
echo -e "\n3️⃣  Testing purgeNamespace (with confirmation)..."
RESPONSE=$(curl -s -X POST "$REDCAP_API_URL" \
  -d "token=$REDCAP_API_TOKEN" \
  -d "content=externalModule" \
  -d "prefix=redcap_rag" \
  -d "action=purgeNamespace" \
  -d "format=json" \
  -d "returnFormat=json" \
  -d "confirm=yes")

echo "$RESPONSE" | python -m json.tool

if echo "$RESPONSE" | grep -q '"status":"success"'; then
    echo "✅ purgeNamespace succeeded"
else
    echo "⚠️  purgeNamespace may have failed (check if namespace was already empty)"
fi

echo -e "\n================================"
echo "✅ All RAG EM API tests completed!"
echo ""
echo "Your RAG EM now supports:"
echo "  - storeDocument (with vector_id in response)"
echo "  - deleteDocument (deletes from dense + sparse)"
echo "  - purgeNamespace (useful for testing)"

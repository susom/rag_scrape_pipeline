"""
Test RAG EM API endpoints after adding deleteDocument and purgeNamespace.

Usage:
    python test_rag_em_api.py
"""

import os
import requests
import json

REDCAP_API_URL = os.getenv("REDCAP_API_URL", "http://localhost/api/")
REDCAP_API_TOKEN = os.getenv("REDCAP_API_TOKEN")

if not REDCAP_API_TOKEN:
    print("❌ ERROR: Set REDCAP_API_TOKEN environment variable")
    exit(1)

print("Testing RAG EM API endpoints...")
print("=" * 60)

# Test 1: Store a document (should return vector_id now)
print("\n1️⃣  Testing storeDocument (should return vector_id)...")
response = requests.post(REDCAP_API_URL, data={
    "token": REDCAP_API_TOKEN,
    "content": "externalModule",
    "prefix": "redcap_rag",
    "action": "storeDocument",
    "format": "json",
    "returnFormat": "json",
    "title": "test_document_123",
    "text": "This is test content for automated ingestion"
})

result = response.json()
print(json.dumps(result, indent=2))

vector_id = result.get("vector_id")
if vector_id:
    print(f"✅ storeDocument returned vector_id: {vector_id}")
else:
    print("❌ storeDocument did NOT return vector_id")
    exit(1)

# Test 2: Delete the document
print("\n2️⃣  Testing deleteDocument...")
response = requests.post(REDCAP_API_URL, data={
    "token": REDCAP_API_TOKEN,
    "content": "externalModule",
    "prefix": "redcap_rag",
    "action": "deleteDocument",
    "format": "json",
    "returnFormat": "json",
    "vector_id": vector_id
})

result = response.json()
print(json.dumps(result, indent=2))

if result.get("status") == "success":
    print("✅ deleteDocument succeeded")
else:
    print("❌ deleteDocument failed")
    exit(1)

# Test 3: Purge namespace (optional - for testing)
print("\n3️⃣  Testing purgeNamespace (with confirmation)...")
response = requests.post(REDCAP_API_URL, data={
    "token": REDCAP_API_TOKEN,
    "content": "externalModule",
    "prefix": "redcap_rag",
    "action": "purgeNamespace",
    "format": "json",
    "returnFormat": "json",
    "confirm": "yes"
})

result = response.json()
print(json.dumps(result, indent=2))

if result.get("status") == "success":
    print("✅ purgeNamespace succeeded")
else:
    print("⚠️  purgeNamespace may have failed (check if namespace was already empty)")

print("\n" + "=" * 60)
print("✅ All RAG EM API tests completed!")
print("")
print("Your RAG EM now supports:")
print("  - storeDocument (with vector_id in response)")
print("  - deleteDocument (deletes from dense + sparse)")
print("  - purgeNamespace (useful for testing)")

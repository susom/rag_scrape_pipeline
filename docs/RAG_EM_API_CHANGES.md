# RAG EM API Changes Needed

## Summary

Your RAG EM already has all the functions needed! You just need to expose them via the API.

**File to modify:** `/Users/irvins/Work/redcap/www/modules-local/redcap_rag_v9.9.9/RedcapRAG.php`

---

## Existing Functions (Already Implemented ✅)

These functions already exist and handle both **dense** and **sparse** vector indexes:

1. ✅ `storeDocument($namespace, $title, $content, ...)` - Line 359
   - Upserts to both dense and sparse Pinecone indexes
   - Uses SHA256 hash of content as vector ID
   - Already exposed via API ✅

2. ✅ `deleteContextDocument($namespace, $id)` - Line 1096
   - Deletes from **both** dense and sparse indexes
   - Takes namespace and vector_id (SHA256 hash)
   - **NOT exposed via API yet** ⚠️

3. ✅ `getRelevantDocuments($namespace, $queryArray, $topK)` - Line 187
   - Searches/queries documents
   - **NOT exposed via API yet** ⚠️

4. ✅ `purgeContextNamespace($namespace)` - Line 1144
   - Deletes all documents in a namespace
   - Useful for testing
   - **NOT exposed via API yet** ⚠️

---

## Changes Required

### Location: `RedcapRAG.php` → `redcap_module_api()` function (line ~1339)

Add new API cases to the existing switch statement.

---

### Change 1: Update `storeDocument` Response (CRITICAL)

**Problem:** Current response doesn't include `vector_id`, which the automated ingestion needs for deletion tracking.

**Find:** Lines 1398-1408 (the success response for storeDocument)

**Replace with:**
```php
if ($success) {
    // Generate vector_id (same as storeDocument uses for Pinecone ID)
    $contentHash = hash('sha256', $text);

    return [
        "status"  => 200,
        "body"    => json_encode([
            "status" => "success",
            "namespace" => $namespace,
            "vector_id" => $contentHash,  // ← ADD THIS LINE
            "title" => $title,
            "message" => "Document stored successfully"
        ]),
        "headers" => ["Content-Type" => "application/json"]
    ];
}
```

---

### Change 2: Add `deleteDocument` Case (REQUIRED)

**Add after `storeDocument` case, before `default`:**

```php
case "deleteDocument":
    $vector_id = $payload['vector_id'] ?? null;

    // Validate required field
    if (!$vector_id) {
        return [
            "status"  => 400,
            "body"    => json_encode([
                "error" => "Missing required field: vector_id"
            ]),
            "headers" => ["Content-Type" => "application/json"]
        ];
    }

    // Delete from both dense and sparse indexes
    $success = $this->deleteContextDocument($namespace, $vector_id);

    if ($success) {
        return [
            "status"  => 200,
            "body"    => json_encode([
                "status" => "success",
                "namespace" => $namespace,
                "vector_id" => $vector_id,
                "message" => "Document deleted successfully from both dense and sparse indexes"
            ]),
            "headers" => ["Content-Type" => "application/json"]
        ];
    } else {
        return [
            "status"  => 500,
            "body"    => json_encode([
                "status" => "error",
                "namespace" => $namespace,
                "vector_id" => $vector_id,
                "error" => "Failed to delete document"
            ]),
            "headers" => ["Content-Type" => "application/json"]
        ];
    }
```

---

### Change 3: Add `purgeNamespace` Case (OPTIONAL - for testing)

**Add after `deleteDocument` case:**

```php
case "purgeNamespace":
    // Dangerous operation - require confirmation
    $confirm = $payload['confirm'] ?? null;

    if ($confirm !== "yes") {
        return [
            "status"  => 400,
            "body"    => json_encode([
                "error" => "Must set confirm=yes to purge namespace"
            ]),
            "headers" => ["Content-Type" => "application/json"]
        ];
    }

    $success = $this->purgeContextNamespace($namespace);

    if ($success) {
        return [
            "status"  => 200,
            "body"    => json_encode([
                "status" => "success",
                "namespace" => $namespace,
                "message" => "Namespace purged successfully (both dense and sparse)"
            ]),
            "headers" => ["Content-Type" => "application/json"]
        ];
    } else {
        return [
            "status"  => 500,
            "body"    => json_encode([
                "status" => "error",
                "error" => "Failed to purge namespace"
            ]),
            "headers" => ["Content-Type" => "application/json"]
        ];
    }
```

---

### Change 4: Add `queryDocuments` Case (OPTIONAL - future)

**Add after `purgeNamespace` case:**

```php
case "queryDocuments":
    $query = $payload['query'] ?? null;
    $topK = (int)($payload['top_k'] ?? 5);

    if (!$query) {
        return [
            "status"  => 400,
            "body"    => json_encode([
                "error" => "Missing required field: query"
            ]),
            "headers" => ["Content-Type" => "application/json"]
        ];
    }

    // Use existing getRelevantDocuments function
    $results = $this->getRelevantDocuments($namespace, [$query], $topK);

    return [
        "status"  => 200,
        "body"    => json_encode([
            "status" => "success",
            "namespace" => $namespace,
            "results" => $results
        ]),
        "headers" => ["Content-Type" => "application/json"]
    ];
```

---

## Testing After Changes

### Test deleteDocument:
```bash
curl -X POST "http://localhost/api/" \
  -d "token=$REDCAP_API_TOKEN" \
  -d "content=externalModule" \
  -d "prefix=redcap_rag" \
  -d "action=deleteDocument" \
  -d "format=json" \
  -d "returnFormat=json" \
  -d "vector_id=abc123def456..."

# Expected:
# {"status":"success","namespace":"default","vector_id":"abc123...","message":"Document deleted successfully from both dense and sparse indexes"}
```

### Test purgeNamespace (for testing):
```bash
curl -X POST "http://localhost/api/" \
  -d "token=$REDCAP_API_TOKEN" \
  -d "content=externalModule" \
  -d "prefix=redcap_rag" \
  -d "action=purgeNamespace" \
  -d "format=json" \
  -d "returnFormat=json" \
  -d "confirm=yes"

# Expected:
# {"status":"success","namespace":"default","message":"Namespace purged successfully (both dense and sparse)"}
```

### Test storeDocument (verify vector_id returned):
```bash
curl -X POST "http://localhost/api/" \
  -d "token=$REDCAP_API_TOKEN" \
  -d "content=externalModule" \
  -d "prefix=redcap_rag" \
  -d "action=storeDocument" \
  -d "format=json" \
  -d "returnFormat=json" \
  -d "title=test_doc" \
  -d "text=This is test content"

# Expected (note vector_id field):
# {"status":"success","namespace":"default","vector_id":"sha256:9f86d081...","title":"test_doc","message":"Document stored successfully"}
```

---

## Priority

**Must have for automated ingestion:**
1. ✅ Change 1: Update `storeDocument` to return `vector_id`
2. ✅ Change 2: Add `deleteDocument` case

**Nice to have:**
3. 🔜 Change 3: Add `purgeNamespace` (useful for testing)
4. 🔜 Change 4: Add `queryDocuments` (for future search features)

---

## Why This Works

Your existing functions already handle the complexity:
- ✅ Dense + sparse vector sync
- ✅ SHA256 hash as vector ID
- ✅ Namespace management
- ✅ Error handling

You're just exposing them via the API so the automated ingestion pipeline can call them!

---

## Summary Checklist

- [ ] Open `/Users/irvins/Work/redcap/www/modules-local/redcap_rag_v9.9.9/RedcapRAG.php`
- [ ] Find `redcap_module_api()` function (line ~1339)
- [ ] Update `storeDocument` success response to include `vector_id`
- [ ] Add `deleteDocument` case after `storeDocument`
- [ ] (Optional) Add `purgeNamespace` case
- [ ] (Optional) Add `queryDocuments` case
- [ ] Test with curl commands above
- [ ] Run automated ingestion test!

**Estimated time:** 15 minutes ⏱️

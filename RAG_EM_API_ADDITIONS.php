<?php
/**
 * ADD THESE CASES to redcap_rag_v9.9.9/RedcapRAG.php
 * in the redcap_module_api() function's switch statement
 * (around line 1362, after the storeDocument case)
 */

// CHANGE 1: Update storeDocument to return vector_id
// Replace lines 1398-1408 with:
if ($success) {
    // Generate vector_id (same as storeDocument uses for Pinecone ID)
    $contentHash = hash('sha256', $text);

    return [
        "status"  => 200,
        "body"    => json_encode([
            "status" => "success",
            "namespace" => $namespace,
            "vector_id" => $contentHash,  // ADD THIS - needed for deletion tracking
            "title" => $title,
            "message" => "Document stored successfully"
        ]),
        "headers" => ["Content-Type" => "application/json"]
    ];
}

// CHANGE 2: Add deleteDocument case (after storeDocument case, before default)
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

// CHANGE 3: Add queryDocuments case (OPTIONAL - for future search functionality)
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

// CHANGE 4: Add purgeNamespace case (OPTIONAL - useful for testing)
case "purgeNamespace":
    // Dangerous operation - consider adding extra validation
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
                "message" => "Namespace purged successfully"
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

?>

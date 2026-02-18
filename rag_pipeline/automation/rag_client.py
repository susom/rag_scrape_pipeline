"""
RAG Client - Interface to REDCap RAG EM's vector database via API.

This module provides functions to store and manage documents in the RAG
vector database through the REDCap External Module API.

Based on: /Users/irvins/Work/redcap/www/modules-local/redcap_rag_v9.9.9/examples/api_usage.py
"""

import os
import json
import time
import requests
from typing import Dict, Optional
from dotenv import load_dotenv
from rag_pipeline.utils.logger import setup_logger

# Ensure .env is loaded
load_dotenv()

logger = setup_logger()

# Retry configuration
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 2


def store_document(
    title: str,
    content: str,
    metadata: Dict,
    api_url: Optional[str] = None,
    api_token: Optional[str] = None,
    namespace: Optional[str] = None,
) -> Dict:
    """
    Store a document section to the RAG vector database via REDCap EM API.

    Args:
        title: Section identifier (used as document title)
        content: Section text content to be vectorized
        metadata: Dictionary with document metadata (doc_id, section_id, source_type, etc.)
        api_url: REDCap API endpoint (defaults to REDCAP_API_URL env var)
        api_token: REDCap API token (defaults to REDCAP_API_TOKEN env var)
        namespace: Vector namespace (optional, defaults to EM's default)

    Returns:
        {
            "status": "success" | "error",
            "namespace": "default",
            "vector_id": "sha256:abc123...",
            "title": section_id,
            "message": "Document stored successfully",
            "error": "..." (if failed)
        }

    Raises:
        ValueError: If API token is missing
        RuntimeError: If API call fails after retries
    """
    # Fetch credentials
    api_url = api_url or os.getenv("REDCAP_API_URL", "http://localhost/api/")
    api_token = api_token or os.getenv("REDCAP_API_TOKEN")

    if not api_token:
        raise ValueError("Missing REDCAP_API_TOKEN in environment.")

    # Build payload for storeDocument action
    payload = {
        "token": api_token,
        "content": "externalModule",
        "prefix": "redcap_rag",
        "action": "storeDocument",
        "format": "json",
        "returnFormat": "json",
        "title": title,
        "text": content,
        "metadata": json.dumps(metadata),
    }

    if namespace:
        payload["namespace"] = namespace

    # Retry loop with exponential backoff
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.debug(f"Storing document '{title}' (attempt {attempt}/{MAX_RETRIES})")

            resp = requests.post(api_url, data=payload, timeout=120)
            resp.raise_for_status()
            data = resp.json()

            # Check API-level status
            if data.get("status") != "success":
                error_msg = data.get("error") or data.get("message") or "Unknown API error"
                raise RuntimeError(f"REDCap RAG API error: {error_msg}")

            logger.info(f"Document '{title}' stored successfully. Vector ID: {data.get('vector_id')}")
            return data

        except requests.exceptions.RequestException as e:
            last_error = e
            logger.warning(f"HTTP error on attempt {attempt}/{MAX_RETRIES}: {e}")

            if attempt < MAX_RETRIES:
                delay = RETRY_DELAY_SECONDS * (2 ** (attempt - 1))  # Exponential backoff
                logger.info(f"Retrying in {delay} seconds...")
                time.sleep(delay)
            else:
                logger.error(f"Failed to store document '{title}' after {MAX_RETRIES} attempts")
                raise RuntimeError(f"REDCap RAG API call failed after {MAX_RETRIES} attempts: {last_error}")

        except Exception as e:
            last_error = e
            logger.error(f"Unexpected error storing document '{title}': {e}")
            raise RuntimeError(f"REDCap RAG API call failed: {e}")

    # Should never reach here, but just in case
    raise RuntimeError(f"REDCap RAG API call failed: {last_error}")


def query_documents(
    query: str,
    top_k: int = 5,
    namespace: Optional[str] = None,
    api_url: Optional[str] = None,
    api_token: Optional[str] = None,
) -> Dict:
    """
    Query the RAG vector database for similar documents.

    Args:
        query: Search query text
        top_k: Number of results to return
        namespace: Vector namespace to search
        api_url: REDCap API endpoint
        api_token: REDCap API token

    Returns:
        {
            "status": "success" | "error",
            "results": [
                {
                    "vector_id": "sha256:...",
                    "title": "section_id",
                    "score": 0.95,
                    "text": "...",
                    "metadata": {...}
                }
            ]
        }

    Raises:
        ValueError: If API token is missing
        RuntimeError: If API call fails
    """
    api_url = api_url or os.getenv("REDCAP_API_URL", "http://localhost/api/")
    api_token = api_token or os.getenv("REDCAP_API_TOKEN")

    if not api_token:
        raise ValueError("Missing REDCAP_API_TOKEN in environment.")

    payload = {
        "token": api_token,
        "content": "externalModule",
        "prefix": "redcap_rag",
        "action": "queryDocuments",
        "format": "json",
        "returnFormat": "json",
        "query": query,
        "top_k": str(top_k),
    }

    if namespace:
        payload["namespace"] = namespace

    try:
        resp = requests.post(api_url, data=payload, timeout=120)
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") != "success":
            error_msg = data.get("error") or "Unknown API error"
            raise RuntimeError(f"REDCap RAG API error: {error_msg}")

        return data

    except Exception as e:
        logger.error(f"Query failed: {e}")
        raise RuntimeError(f"REDCap RAG API query failed: {e}")


def delete_document(
    vector_id: str,
    namespace: Optional[str] = None,
    api_url: Optional[str] = None,
    api_token: Optional[str] = None,
) -> Dict:
    """
    Delete a document from the RAG vector database.

    Args:
        vector_id: Vector ID to delete (SHA256 hash)
        namespace: Vector namespace
        api_url: REDCap API endpoint
        api_token: REDCap API token

    Returns:
        {
            "status": "success" | "error",
            "message": "Document deleted",
            "error": "..." (if failed)
        }

    Raises:
        ValueError: If API token is missing
        RuntimeError: If API call fails
    """
    api_url = api_url or os.getenv("REDCAP_API_URL", "http://localhost/api/")
    api_token = api_token or os.getenv("REDCAP_API_TOKEN")

    if not api_token:
        raise ValueError("Missing REDCAP_API_TOKEN in environment.")

    payload = {
        "token": api_token,
        "content": "externalModule",
        "prefix": "redcap_rag",
        "action": "deleteDocument",
        "format": "json",
        "returnFormat": "json",
        "vector_id": vector_id,
    }

    if namespace:
        payload["namespace"] = namespace

    try:
        resp = requests.post(api_url, data=payload, timeout=120)
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") != "success":
            error_msg = data.get("error") or "Unknown API error"
            raise RuntimeError(f"REDCap RAG API error: {error_msg}")

        logger.info(f"Document '{vector_id}' deleted successfully")
        return data

    except Exception as e:
        logger.error(f"Delete failed: {e}")
        raise RuntimeError(f"REDCap RAG API delete failed: {e}")

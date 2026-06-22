"""
pgvector RAG client — stores documents via the RExI AI backend's /rag/ingest endpoint.

Drop-in replacement for rag_client.store_document / delete_document when
RAG_BACKEND=pgvector.  The RExI backend handles embedding (AI Hub
text-embedding-3-small) and INSERT into rag_chunks (pgvector postgres).

Environment variables:
  REXI_AI_BACKEND_URL   Base URL of the RExI AI backend.
                        Default: http://localhost:7701
                        Production: the Cloud Run service URL for rexi-ai.
  PGVECTOR_NAMESPACE    Vector namespace written to rag_chunks.namespace.
                        Default: rexi_knowledge
"""

import os
import time
import requests
from typing import Dict, Optional
from rag_pipeline.utils.http import get_session
from rag_pipeline.utils.logger import setup_logger

logger = setup_logger()

MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 2

DEFAULT_REXI_URL = os.getenv("REXI_AI_BACKEND_URL", "http://localhost:7701")
DEFAULT_NAMESPACE = os.getenv("PGVECTOR_NAMESPACE", "rexi_knowledge")


def store_document(
    title: str,
    content: str,
    metadata: Dict,
    api_url: Optional[str] = None,
    api_token: Optional[str] = None,
    namespace: Optional[str] = None,
) -> Dict:
    """
    Store a document section in pgvector via the RExI /rag/ingest endpoint.

    Interface mirrors rag_client.store_document so orchestrator.py can swap
    backends with a single import change.

    Returns:
        {
            "status": "success",
            "vector_id": "<uuid from rag_chunks.id>",
            "namespace": "rexi_knowledge",
            "title": "<section_id>",
            "message": "Document stored in pgvector",
        }
    """
    base_url = (api_url or DEFAULT_REXI_URL).rstrip("/")
    ns = namespace or DEFAULT_NAMESPACE

    payload = {
        "title":     title,
        "text":      content,
        "metadata":  metadata or {},
        "namespace": ns,
    }

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.debug(f"pgvector ingest '{title}' (attempt {attempt}/{MAX_RETRIES})")
            resp = get_session().post(f"{base_url}/rag/ingest", json=payload, timeout=60)
            resp.raise_for_status()
            data = resp.json()

            vector_id = data.get("id") or data.get("vector_id", "")
            logger.info(f"pgvector stored '{title}', id={vector_id}")
            return {
                "status":    "success",
                "vector_id": vector_id,
                "namespace": ns,
                "title":     title,
                "message":   "Document stored in pgvector",
            }

        except requests.exceptions.RequestException as e:
            last_error = e
            logger.warning(f"pgvector ingest attempt {attempt}/{MAX_RETRIES} failed: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS * (2 ** (attempt - 1)))

    raise RuntimeError(f"pgvector ingest failed after {MAX_RETRIES} attempts: {last_error}")


def delete_document(
    vector_id: str,
    namespace: Optional[str] = None,
    api_url: Optional[str] = None,
    api_token: Optional[str] = None,
) -> Dict:
    """
    Delete a chunk from pgvector via the RExI /rag/delete endpoint.

    If the endpoint doesn't exist yet, logs a warning and returns success so
    orchestrator cleanup loops don't break.
    """
    base_url = (api_url or DEFAULT_REXI_URL).rstrip("/")
    ns = namespace or DEFAULT_NAMESPACE

    try:
        resp = get_session().post(
            f"{base_url}/rag/delete",
            json={"id": vector_id, "namespace": ns},
            timeout=30,
        )
        resp.raise_for_status()
        logger.info(f"pgvector deleted chunk {vector_id}")
        return {"status": "success", "message": f"Deleted {vector_id}"}
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            logger.warning(f"pgvector /rag/delete not implemented on backend, skipping {vector_id}")
            return {"status": "success", "message": "delete not implemented on backend"}
        raise RuntimeError(f"pgvector delete failed: {e}")
    except Exception as e:
        logger.warning(f"pgvector delete failed for {vector_id}: {e}")
        return {"status": "success", "message": f"delete skipped: {e}"}

"""
pgvector RAG client — writes documents DIRECTLY into the RExI rag_chunks table.

Drop-in replacement for rag_client.store_document / delete_document when
RAG_BACKEND=pgvector.  The pipeline already connects to the RExI Postgres
(rexi_db) for ingestion tracking, and rag_chunks lives in the SAME database,
so we embed via AI Hub and INSERT directly — no HTTP call, no IAP auth, no
dependency on the RExI backend being up.

This mirrors RExI's PgVectorRagService.ingest exactly:
  - dense:  AI Hub text-embedding-3-small (1536) -> rag_chunks.dense_vec
  - sparse: to_tsvector('english', text)         -> rag_chunks.ts_vec
  - id:     server-generated uuid (gen_random_uuid)

RExI's /api/ai/rag/ingest endpoint remains available for one-off/ad-hoc work;
both paths write the same table with the same embedding model and ts config.

Environment variables:
  PGVECTOR_NAMESPACE     rag_chunks.namespace value (default: rexi_knowledge).
  AI_HUB_EMBEDDING_URL   AI Hub embeddings URL (used by aihub_client.embed).
  DB_SCHEMA              Schema holding rag_chunks (default: rexi).
"""

import json
import os
import time
from typing import Dict, Optional

from sqlalchemy import text

from rag_pipeline.processing.aihub_client import embed
from rag_pipeline.database.connection import engine, DB_SCHEMA
from rag_pipeline.utils.logger import setup_logger

logger = setup_logger()

MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 2

DEFAULT_NAMESPACE = os.getenv("PGVECTOR_NAMESPACE", "rexi_knowledge")


def _table_ref() -> str:
    """Schema-qualified rag_chunks reference (falls back to unqualified)."""
    schema = DB_SCHEMA or os.getenv("DB_SCHEMA", "rexi")
    return f'"{schema}".rag_chunks' if schema else "rag_chunks"


def _vector_literal(embedding) -> str:
    """List[float] -> pgvector text literal '[0.1,0.2,...]'."""
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


def store_document(
    title: str,
    content: str,
    metadata: Dict,
    api_url: Optional[str] = None,
    api_token: Optional[str] = None,
    namespace: Optional[str] = None,
) -> Dict:
    """
    Embed `content` via AI Hub and INSERT a row into rag_chunks.

    Interface mirrors rag_client.store_document so orchestrator.py swaps backends
    with a single import change. Returns the server-generated uuid as vector_id
    (the orchestrator stores it for later cleanup on re-ingestion).
    """
    if engine is None:
        raise RuntimeError("pgvector store_document: database engine not configured")

    ns = namespace or DEFAULT_NAMESPACE
    # Mirror RExI: stuff the title into metadata alongside the dedicated column.
    full_meta = dict(metadata or {})
    full_meta["title"] = title
    meta_json = json.dumps(full_meta)
    table = _table_ref()

    insert_sql = text(
        f"INSERT INTO {table} (namespace, title, text, metadata, dense_vec, ts_vec) "
        f"VALUES (:ns, :title, :text, CAST(:meta AS jsonb), CAST(:vec AS vector), "
        f"to_tsvector('english', :text)) "
        f"RETURNING id"
    )

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.debug(f"pgvector ingest '{title}' (attempt {attempt}/{MAX_RETRIES})")
            embedding = embed(content)
            vec_literal = _vector_literal(embedding)

            with engine.begin() as conn:
                row = conn.execute(
                    insert_sql,
                    {"ns": ns, "title": title, "text": content, "meta": meta_json, "vec": vec_literal},
                ).fetchone()

            vector_id = str(row[0]) if row else ""
            logger.info(f"pgvector stored '{title}', id={vector_id}")
            return {
                "status": "success",
                "vector_id": vector_id,
                "namespace": ns,
                "title": title,
                "message": "Document stored in pgvector",
            }

        except Exception as e:
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
    Delete a chunk from rag_chunks by id. Used by orchestrator cleanup loops to
    remove stale vectors after a re-ingestion. Missing rows are treated as success.
    """
    if engine is None:
        raise RuntimeError("pgvector delete_document: database engine not configured")

    table = _table_ref()
    delete_sql = text(f"DELETE FROM {table} WHERE id = CAST(:id AS uuid)")

    try:
        with engine.begin() as conn:
            result = conn.execute(delete_sql, {"id": vector_id})
        deleted = result.rowcount if result.rowcount is not None else 0
        logger.info(f"pgvector deleted chunk {vector_id} (rows={deleted})")
        return {"status": "success", "message": f"Deleted {vector_id}"}
    except Exception as e:
        logger.warning(f"pgvector delete failed for {vector_id}: {e}")
        return {"status": "success", "message": f"delete skipped: {e}"}

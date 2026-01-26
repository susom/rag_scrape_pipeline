"""
Canonical JSON output writer for RPP pipeline.
Produces rpp.v1 schema artifacts.
"""

import os
import json
import hashlib
from datetime import datetime, timezone
from typing import Optional

from rag_pipeline.processing.ai_client import DEFAULT_MODEL

# Pipeline version - update on releases
RPP_VERSION = "0.2.0"


def _sha256(data: str) -> str:
    """Compute SHA256 hash of string data."""
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _generate_doc_id(uri: str) -> str:
    """Generate deterministic document ID from URI."""
    return f"doc_{_sha256(uri)[:12]}"


def _generate_section_id(doc_id: str, index: int) -> str:
    """Generate deterministic section ID from doc_id and index."""
    return f"sec_{doc_id[4:16]}_{index:03d}"


def write_canonical_json(
    run_id: str,
    run_mode: str,
    follow_links: bool,
    triggered_by: str,
    documents: list[dict],
    warnings: list[dict],
    start_time: datetime,
    end_time: Optional[datetime] = None,
    tags: Optional[list[str]] = None,
    output_dir: str = "cache/rag_ready",
    model_hint: Optional[str] = None,
) -> dict:
    """
    Write canonical rpp.v1 JSON artifact.

    Args:
        run_id: Unique run identifier (computed at entrypoint)
        run_mode: "deterministic" | "ai_auto" | "ai_always"
        follow_links: Whether same-domain links were followed
        triggered_by: "web_api" | "cli" | "main"
        documents: List of document dicts with structure:
            {
                "uri": str,
                "source_type": str,  # "url" | "pdf" | "docx" | "txt"
                "cached_files": {"raw_html": str|None, "pdf_text": str|None},
                "followed_from": str|None,  # parent doc_id if followed
                "sections": [{"text": str, "section_title": str|None}],
                "errors": list[str]
            }
        warnings: List of warning dicts with {"level", "message", ...}
        start_time: Pipeline start timestamp
        end_time: Pipeline end timestamp (defaults to now)
        tags: Optional run-level tags
        output_dir: Directory for output file

    Returns:
        Dict with keys: run_id, output_path, stats, warnings

    Schema additions:
        - section_version: Content version identifier (currently "v_1")
        - section_updated: ISO timestamp of when section was processed
    """
    if end_time is None:
        end_time = datetime.now(timezone.utc)

    # Build documents with computed IDs and hashes
    canonical_documents = []
    total_sections = 0
    total_chars = 0
    total_errors = 0

    for doc in documents:
        doc_id = _generate_doc_id(doc["uri"])

        # Build sections with IDs and hashes
        sections = []
        section_texts = []
        for idx, sec in enumerate(doc.get("sections", []), start=1):
            text = sec.get("text", "")
            section_texts.append(text)
            total_chars += len(text)

            sections.append({
                "section_id": _generate_section_id(doc_id, idx),
                "section_hash": f"sha256:{_sha256(text)}",
                "section_version": "v_1",  # Placeholder for future versioning system
                "section_updated": datetime.now(timezone.utc).isoformat(),
                "text": text,
                "location": {
                    "window_index": sec.get("window_index"),
                    "char_start": sec.get("char_start"),
                    "char_end": sec.get("char_end"),
                    "page": sec.get("page"),
                    "section_title": sec.get("section_title"),
                },
                "ai": {
                    "normalized": sec.get("ai_normalized", False),
                    "trigger_reason": sec.get("ai_trigger_reason"),
                    "request_count": sec.get("ai_request_count", 0),
                    "input_tokens": sec.get("ai_input_tokens"),
                    "output_tokens": sec.get("ai_output_tokens"),
                },
            })

        # Compute document hash from all section texts
        doc_hash = _sha256("".join(section_texts)) if section_texts else None

        total_sections += len(sections)
        total_errors += len(doc.get("errors", []))

        canonical_documents.append({
            "doc_id": doc_id,
            "doc_hash": f"sha256:{doc_hash}" if doc_hash else None,
            "source": {
                "type": doc.get("source_type", "url"),
                "uri": doc["uri"],
                "followed_from": doc.get("followed_from"),
                "cached_files": doc.get("cached_files", {}),
            },
            "sections": sections,
            "document_stats": {
                "section_count": len(sections),
                "total_chars": sum(len(s["text"]) for s in sections),
            },
            "errors": doc.get("errors", []),
        })

    # Build aggregate stats
    processing_time = (end_time - start_time).total_seconds()
    aggregate_stats = {
        "documents_processed": len(canonical_documents),
        "total_sections": total_sections,
        "total_chars_extracted": total_chars,
        "total_errors": total_errors,
        "processing_time_seconds": round(processing_time, 2),
    }

    # Assemble canonical output
    canonical_output = {
        "schema_version": "rpp.v1",
        "rpp_version": RPP_VERSION,
        "run": {
            "run_id": run_id,
            "timestamp_start": start_time.isoformat(),
            "timestamp_end": end_time.isoformat(),
            "triggered_by": triggered_by,
            "run_mode": run_mode,
            "input_count": len(documents),
            "follow_links": follow_links,
            "tags": tags or [],
        },
        "ai_config": {
            "enabled": run_mode != "deterministic",
            "provider": "secure_chat_ai",
            "model_hint": model_hint or DEFAULT_MODEL,
        },
        "documents": canonical_documents,
        "aggregate_stats": aggregate_stats,
        "warnings": warnings,
    }

    # Write to file
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"{run_id}.json")

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(canonical_output, f, ensure_ascii=False, indent=2)

    # Return summary for API response
    return {
        "run_id": run_id,
        "output_path": output_path,
        "stats": aggregate_stats,
        "warnings": warnings,
    }


def generate_run_id(input_uris: list[str]) -> str:
    """
    Generate deterministic run ID from timestamp and input URIs.

    Format: rpp_YYYY-MM-DDTHH-MM-SSZ_XXXXXXXX
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    uri_hash = _sha256("".join(sorted(input_uris)))[:8]
    return f"rpp_{timestamp}_{uri_hash}"

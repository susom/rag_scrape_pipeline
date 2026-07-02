"""
Headless batch-ingestion entrypoint (no web server).

Runs ONE automated ingestion pass and exits — the idiomatic shape for a
Kubernetes CronJob / one-shot Job pod (Pattern B). Mirrors the logic of the
FastAPI POST /api/ingest-batch handler in web.py (auth aside): init DB, open a
session, acquire the distributed lock, run the orchestrator, print a JSON
summary, exit with a status-appropriate code.

Usage:
    python -m rag_pipeline.ingest_batch [--site rexi] [--days-back 1]
                                        [--force-reprocess] [--dry-run]
                                        [--document-ids id1,id2]

Exit codes:
    0  completed (may include per-document errors in the summary)
    1  failed (fatal / total failure)
    2  locked (another ingestion already in progress)
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

from rag_pipeline.database import init_db, SessionLocal
from rag_pipeline.automation.locking import DistributedLock, LockAlreadyHeld
from rag_pipeline.automation.orchestrator import run_automated_ingestion
from rag_pipeline.utils.logger import setup_logger
from rag_pipeline.utils.secret_file import load_secret_file

logger = setup_logger()


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="rag_pipeline.ingest_batch",
        description="Run one automated SharePoint -> RAG ingestion pass and exit.",
    )
    parser.add_argument(
        "--site",
        default=None,
        help="SharePoint site name (e.g. 'rexi', 'som'). Omit for the default site.",
    )
    parser.add_argument(
        "--days-back",
        type=int,
        default=1,
        help="Only fetch SharePoint files modified in the last N days (default: 1). "
        "Ignored when --force-reprocess is set.",
    )
    parser.add_argument(
        "--force-reprocess",
        action="store_true",
        help="Ignore content hashes and reprocess all documents (full sync).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report detected changes without ingesting.",
    )
    parser.add_argument(
        "--document-ids",
        default=None,
        help="Comma-separated list of specific document IDs to process.",
    )
    return parser.parse_args(argv)


def run(argv=None) -> int:
    args = _parse_args(argv)

    # If secrets are provided as a mounted CSI properties file (rexi-app style),
    # fold them into the environment. No-op when secrets arrive as env vars.
    loaded = load_secret_file()
    if loaded:
        logger.info(f"Loaded {len(loaded)} secret(s) from properties file: {', '.join(sorted(loaded))}")

    doc_id_list = None
    if args.document_ids:
        doc_id_list = [d.strip() for d in args.document_ids.split(",") if d.strip()]

    # SharePoint date filter (ignored on force-reprocess), matching the endpoint.
    modified_since = None
    if not args.force_reprocess:
        modified_since = datetime.now(timezone.utc) - timedelta(days=args.days_back)
        logger.info(
            f"SharePoint date filter: files modified since "
            f"{modified_since.isoformat()} ({args.days_back} days)"
        )

    # Ensure tracking tables/schema exist (idempotent), then open a session.
    init_db()
    if SessionLocal is None:
        logger.error("Database is not configured (SessionLocal is None); cannot run.")
        print(json.dumps({"status": "failed", "message": "database not configured", "run_id": None}))
        return 1

    # Per-site lock key so multiple sites can run concurrently (mirrors web.py).
    lock_key = f"automated_ingestion:{args.site}" if args.site else "automated_ingestion"

    db = SessionLocal()
    try:
        with DistributedLock(lock_key=lock_key, db_session=db, timeout_minutes=60):
            result = run_automated_ingestion(
                db_session=db,
                force_reprocess=args.force_reprocess,
                document_ids=doc_id_list,
                dry_run=args.dry_run,
                modified_since=modified_since,
                site_name=args.site,
            )

        summary = {
            "status": result.status,
            "run_id": result.run_id,
            "site": args.site or "default",
            "summary": {
                "documents_processed": result.documents_processed,
                "sections_ingested": result.sections_ingested,
                "documents_skipped": result.documents_skipped,
                "documents_failed": result.documents_failed,
                "processing_time_seconds": result.processing_time_seconds,
            },
            "errors": result.errors,
            "dry_run": result.dry_run,
        }
        print(json.dumps(summary, default=str))
        return 1 if result.status == "failed" else 0

    except LockAlreadyHeld as e:
        logger.warning(f"Ingestion already in progress: {e}")
        print(json.dumps({"status": "locked", "message": str(e), "run_id": None}))
        return 2

    except Exception as e:
        logger.error(f"Ingestion failed: {e}", exc_info=True)
        print(json.dumps({"status": "failed", "message": str(e), "run_id": None}))
        return 1

    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(run())

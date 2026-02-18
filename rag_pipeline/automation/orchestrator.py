"""
Orchestrator - Main automated ingestion workflow logic.

Coordinates:
- Content fetching from SharePoint and external URLs
- Hash-based delta detection (skip unchanged documents)
- RAG pipeline processing (split paths: SP files vs URLs)
- Vector database ingestion via RAG EM API
- Database state tracking and error handling
"""

import os
import json
from datetime import datetime, timezone
from typing import List, Optional, Dict, Tuple
from dataclasses import dataclass, asdict
from sqlalchemy.orm import Session

from rag_pipeline.database.models import DocumentIngestionState
from rag_pipeline.automation.content_fetcher import fetch_content_sources, fetch_content_sources_stub
from rag_pipeline.sharepoint import SharePointGraphClient, SharePointItem, get_site_config
from rag_pipeline.automation.rag_client import store_document, delete_document
from rag_pipeline.automation.locking import DistributedLock, LockAlreadyHeld
from rag_pipeline.processing.text_extraction import extract_text_from_file, get_thinker_name
from rag_pipeline.processing.sliding_window import SlidingWindowParser
from rag_pipeline.main import run_pipeline
from rag_pipeline.output_json import generate_run_id, write_canonical_json
from rag_pipeline.scraping.scraper import scrape_url
from rag_pipeline.utils.logger import setup_logger

logger = setup_logger()

# Configuration
DEFAULT_LOCK_TIMEOUT_MINUTES = int(os.getenv("INGESTION_LOCK_TIMEOUT_MINUTES", "60"))
DEFAULT_MAX_RETRIES = int(os.getenv("INGESTION_MAX_RETRIES", "3"))


@dataclass
class IngestionResult:
    """Result of automated ingestion run."""
    status: str  # "completed" | "failed" | "locked"
    run_id: str
    documents_processed: int
    sections_ingested: int
    documents_skipped: int
    documents_failed: int
    processing_time_seconds: float
    errors: List[Dict]
    dry_run: bool = False

    def to_dict(self):
        """Convert to dict for JSON serialization."""
        return asdict(self)


class IngestionOrchestrator:
    """
    Orchestrates automated RAG ingestion workflow.

    Responsibilities:
    - Fetch content from all sources
    - Detect changed/new documents via hash comparison
    - Process documents through RAG pipeline
    - Ingest sections into vector database
    - Track state and handle errors
    """

    def __init__(self, db_session: Session, dry_run: bool = False):
        self.db = db_session
        self.dry_run = dry_run
        self.errors = []
        self.start_time = datetime.now(timezone.utc)
        self._sp_client = None  # Lazy-initialized SharePoint client

    def _get_sp_client(self) -> SharePointGraphClient:
        """Lazy-initialize SharePoint client for file downloads."""
        if self._sp_client is None:
            site_config = get_site_config()
            self._sp_client = SharePointGraphClient(
                site_hostname=site_config.hostname,
                site_path=site_config.path,
            )
        return self._sp_client

    def run(
        self,
        force_reprocess: bool = False,
        document_ids: Optional[List[str]] = None,
        modified_since: Optional[datetime] = None,
    ) -> IngestionResult:
        """
        Execute automated ingestion workflow.

        Args:
            force_reprocess: If True, ignore hashes and reprocess all documents
            document_ids: If provided, only process these specific document IDs
            modified_since: If provided, only fetch SharePoint files modified since this datetime
                          (external-urls.txt is always fetched regardless). For 7-day cron: datetime.now() - timedelta(days=7)

        Returns:
            IngestionResult with summary statistics
        """
        try:
            logger.info("Starting automated ingestion run")
            logger.info(f"Mode: {'DRY RUN' if self.dry_run else 'LIVE'}")
            logger.info(f"Force reprocess: {force_reprocess}")
            if document_ids:
                logger.info(f"Filtering to {len(document_ids)} document(s)")

            # Step 1: Fetch content from all sources
            if modified_since:
                logger.info(f"Fetching content from all sources (SharePoint files modified since {modified_since})...")
            else:
                logger.info("Fetching content from all sources...")
            sharepoint_items, external_urls = self._fetch_content(modified_since=modified_since)

            # Step 2: Delta detection - identify changed/new documents
            logger.info("Performing delta detection...")
            documents_to_process = self._detect_changes(
                sharepoint_items,
                external_urls,
                force_reprocess=force_reprocess,
                filter_ids=document_ids,
            )

            logger.info(f"Found {len(documents_to_process)} document(s) to process")

            if not documents_to_process:
                logger.info("No documents to process - all up to date")
                return self._build_result(
                    status="completed",
                    run_id=f"ingest_{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%SZ')}",
                    documents_processed=0,
                    sections_ingested=0,
                    documents_skipped=len(sharepoint_items) + len(external_urls),
                    documents_failed=0,
                )

            if self.dry_run:
                logger.info("DRY RUN - Would process the following documents:")
                for doc_info in documents_to_process:
                    logger.info(f"  - {doc_info['document_id']}: {doc_info['source_uri']}")
                return self._build_result(
                    status="completed",
                    run_id=f"ingest_dry_{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%SZ')}",
                    documents_processed=0,
                    sections_ingested=0,
                    documents_skipped=len(sharepoint_items) + len(external_urls) - len(documents_to_process),
                    documents_failed=0,
                )

            # Step 3: Process documents through RAG pipeline
            logger.info("Processing documents through RAG pipeline...")
            processed_documents = self._process_documents(documents_to_process)

            # Step 4: Ingest sections into vector database
            logger.info("Ingesting sections into RAG vector database...")
            ingestion_stats = self._ingest_to_rag(processed_documents)

            # Step 5: Build summary
            result = self._build_result(
                status="completed",
                run_id=processed_documents.get("run_id", "unknown"),
                documents_processed=ingestion_stats["documents_processed"],
                sections_ingested=ingestion_stats["sections_ingested"],
                documents_skipped=ingestion_stats["documents_skipped"],
                documents_failed=ingestion_stats["documents_failed"],
            )

            logger.info(f"Ingestion completed in {result.processing_time_seconds}s")
            logger.info(f"  Processed: {result.documents_processed} documents")
            logger.info(f"  Ingested: {result.sections_ingested} sections")
            logger.info(f"  Skipped: {result.documents_skipped} documents")
            logger.info(f"  Failed: {result.documents_failed} documents")

            return result

        except Exception as e:
            logger.error(f"Ingestion failed: {e}", exc_info=True)
            self.errors.append({
                "type": "fatal_error",
                "message": str(e),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

            return self._build_result(
                status="failed",
                run_id=f"ingest_failed_{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%SZ')}",
                documents_processed=0,
                sections_ingested=0,
                documents_skipped=0,
                documents_failed=0,
            )

    def _fetch_content(self, modified_since: Optional[datetime] = None) -> Tuple[List[SharePointItem], List[str]]:
        """Fetch content from all sources.
        
        Args:
            modified_since: Optional datetime to filter SharePoint files by modification date.
                          Files modified before this time are excluded (except external-urls.txt).
        """
        try:
            return fetch_content_sources(modified_since=modified_since)
        except Exception as e:
            logger.error(f"Content fetching failed: {e}")
            self.errors.append({
                "type": "content_fetch_error",
                "message": str(e),
            })
            return [], []

    def _detect_changes(
        self,
        sharepoint_items: List[SharePointItem],
        external_urls: List[str],
        force_reprocess: bool = False,
        filter_ids: Optional[List[str]] = None,
    ) -> List[Dict]:
        """
        Detect changed/new documents.

        For SharePoint files: uses lastModifiedDateTime from Graph API manifest —
        if the file was modified after our last processing, queue it. No download
        or hashing needed; the Graph API timestamp is authoritative.

        For external URLs: scrapes page → hashes text → compares to DB.
        No "last modified" metadata available, so hash is the only option.
        """
        documents_to_process = []

        # --- SharePoint files (timestamp-based) ---
        for sp_item in sharepoint_items:
            document_id = DocumentIngestionState.generate_document_id(
                title=sp_item.name,
                url=sp_item.url,
            )

            if filter_ids and document_id not in filter_ids:
                continue

            if self._sp_item_needs_processing(
                document_id=document_id,
                sp_item=sp_item,
                force_reprocess=force_reprocess,
            ):
                documents_to_process.append({
                    "document_id": document_id,
                    "source_type": "sharepoint",
                    "source_uri": sp_item.url,
                    "file_name": sp_item.name,
                    "download_url": sp_item.download_url,
                    "date_modified": sp_item.last_modified,
                })

            self._update_last_seen(document_id)

        # --- External URLs ---
        for url in external_urls:
            document_id = DocumentIngestionState.generate_document_id(
                title=url,
                url=url,
            )

            if filter_ids and document_id not in filter_ids:
                continue

            # Scrape URL for hashing (real delta detection)
            scraped_text = None
            try:
                scrape_result = scrape_url(url, follow_attachments=False)
                if scrape_result.get("error"):
                    logger.warning(f"Scrape failed for {url}: {scrape_result['error']}")
                    self._update_last_seen(document_id)
                    continue
                scraped_text = scrape_result.get("text", "")
            except Exception as e:
                logger.error(f"Failed to scrape {url}: {e}")
                self.errors.append({
                    "type": "scrape_error",
                    "document_id": document_id,
                    "message": str(e),
                })
                self._update_last_seen(document_id)
                continue

            if not scraped_text or len(scraped_text.strip()) < 100:
                logger.warning(f"Skipping {url}: insufficient scraped content")
                self._update_last_seen(document_id)
                continue

            if self._should_process_url(
                document_id=document_id,
                content=scraped_text,
                source_uri=url,
                force_reprocess=force_reprocess,
            ):
                documents_to_process.append({
                    "document_id": document_id,
                    "source_type": "url",
                    "source_uri": url,
                    "file_name": None,
                    "date_modified": None,
                    # Cached scraped text — avoids re-scraping in _process_documents
                    "_cached_text": scraped_text,
                })

            self._update_last_seen(document_id)

        return documents_to_process

    def _sp_item_needs_processing(
        self,
        document_id: str,
        sp_item: SharePointItem,
        force_reprocess: bool = False,
    ) -> bool:
        """
        Determine if a SharePoint file needs processing using Graph API's
        lastModifiedDateTime. No download or hashing required — the timestamp
        is authoritative (updates only on content/metadata changes, not views).
        """
        if force_reprocess:
            logger.debug(f"Force reprocess: {document_id}")
            return True

        existing = self.db.query(DocumentIngestionState).filter(
            DocumentIngestionState.document_id == document_id
        ).first()

        if not existing:
            logger.info(f"New SharePoint file: {sp_item.name}")
            return True

        if not existing.last_processed_at:
            logger.info(f"Never processed: {sp_item.name}")
            return True

        if sp_item.last_modified and sp_item.last_modified > existing.last_processed_at:
            logger.info(
                f"SharePoint file modified: {sp_item.name} "
                f"(modified={sp_item.last_modified}, last_processed={existing.last_processed_at})"
            )
            return True

        logger.debug(f"SharePoint file unchanged: {sp_item.name}")
        return False

    def _should_process_url(
        self,
        document_id: str,
        content: str,
        source_uri: str,
        force_reprocess: bool = False,
    ) -> bool:
        """Determine if a URL should be processed based on content hash comparison."""
        if force_reprocess:
            logger.debug(f"Force reprocess: {document_id}")
            return True

        existing = self.db.query(DocumentIngestionState).filter(
            DocumentIngestionState.document_id == document_id
        ).first()

        if not existing:
            logger.info(f"New URL: {source_uri}")
            return True

        new_hash = DocumentIngestionState.compute_content_hash(content)
        if new_hash != existing.content_hash:
            logger.info(f"URL content changed: {source_uri}")
            return True

        logger.debug(f"URL unchanged: {source_uri}")
        return False

    def _update_last_seen(self, document_id: str):
        """Update last_seen_at timestamp for document."""
        if self.dry_run:
            return

        try:
            existing = self.db.query(DocumentIngestionState).filter(
                DocumentIngestionState.document_id == document_id
            ).first()

            if existing:
                existing.last_seen_at = datetime.now(timezone.utc)
                self.db.commit()

        except Exception as e:
            logger.warning(f"Failed to update last_seen_at for {document_id}: {e}")
            self.db.rollback()

    def _process_documents(self, documents_to_process: List[Dict]) -> Dict:
        """
        Process documents through RAG pipeline.

        Splits into two paths:
        - SharePoint files: local text extraction → sliding window AI
        - External URLs: run_pipeline(urls=...) for scraping + AI

        Returns canonical JSON-shaped dict with merged documents.
        """
        run_id = f"ingest_{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%SZ')}"
        start_time = datetime.now(timezone.utc)

        sp_docs = [d for d in documents_to_process if d["source_type"] == "sharepoint"]
        url_docs = [d for d in documents_to_process if d["source_type"] == "url"]

        all_documents = []

        # --- Path A: SharePoint files (local processing) ---
        if sp_docs:
            sp_documents = self._process_sharepoint_files(sp_docs)
            all_documents.extend(sp_documents)

        # --- Path B: External URLs (run_pipeline) ---
        if url_docs:
            url_documents = self._process_urls(url_docs, run_id)
            all_documents.extend(url_documents)

        if not all_documents:
            return {
                "run_id": run_id,
                "documents": [],
                "stats": {},
            }

        # Write merged canonical JSON
        result = write_canonical_json(
            run_id=run_id,
            run_mode="ai_always",
            follow_links=False,
            triggered_by="automated_ingestion",
            documents=all_documents,
            warnings=[],
            start_time=start_time,
        )

        # Load the written JSON to return full structure
        output_path = result["output_path"]
        with open(output_path, "r") as f:
            pipeline_output = json.load(f)

        return pipeline_output

    def _process_sharepoint_files(self, sp_docs: List[Dict]) -> List[Dict]:
        """
        Process SharePoint files: download → extract text → sliding window AI.

        For each file:
        1. Download bytes via download_url
        2. Extract text (docx2txt / pdfplumber / utf-8)
        3. Save to cache/raw/
        4. Run through SlidingWindowParser.process_file()
        5. Build document dict for canonical JSON
        """
        os.makedirs("cache/raw", exist_ok=True)
        parser = SlidingWindowParser()
        documents = []

        for doc in sp_docs:
            file_name = doc["file_name"]
            download_url = doc.get("download_url")

            # Download file bytes
            try:
                if not download_url:
                    raise ValueError(f"No download_url for {file_name}")
                client = self._get_sp_client()
                file_bytes = client.download_file_content(download_url)
                extracted_text = extract_text_from_file(file_name, file_bytes)
            except Exception as e:
                logger.error(f"Failed to download/extract {file_name}: {e}")
                self.errors.append({
                    "type": "download_error",
                    "document_id": doc["document_id"],
                    "message": str(e),
                })
                documents.append({
                    "uri": doc["source_uri"],
                    "source_type": "sharepoint",
                    "cached_files": {},
                    "followed_from": None,
                    "sections": [],
                    "errors": [f"Download/extraction failed: {e}"],
                })
                continue

            if not extracted_text:
                logger.warning(f"No extracted text for {file_name}, skipping")
                documents.append({
                    "uri": doc["source_uri"],
                    "source_type": "sharepoint",
                    "cached_files": {},
                    "followed_from": None,
                    "sections": [],
                    "errors": ["No text extracted from file"],
                })
                continue

            try:
                # Save raw file bytes
                raw_path = os.path.join("cache/raw", file_name)
                with open(raw_path, "wb") as f:
                    f.write(file_bytes)

                # Save extracted text
                txt_path = os.path.join("cache/raw", os.path.splitext(file_name)[0] + ".txt")
                with open(txt_path, "w", encoding="utf-8") as f:
                    f.write(extracted_text)

                # Determine thinker_name and process
                thinker = get_thinker_name(file_name)
                count, sections = parser.process_file(txt_path, "", thinker_name=thinker)
                logger.info(f"Processed {file_name}: {len(sections)} sections")

                documents.append({
                    "uri": doc["source_uri"],
                    "source_type": "sharepoint",
                    "cached_files": {"raw_text": txt_path},
                    "followed_from": None,
                    "sections": sections,
                    "errors": [],
                })

            except Exception as e:
                logger.error(f"Failed to process SharePoint file {file_name}: {e}", exc_info=True)
                self.errors.append({
                    "type": "processing_error",
                    "document_id": doc["document_id"],
                    "message": str(e),
                })
                documents.append({
                    "uri": doc["source_uri"],
                    "source_type": "sharepoint",
                    "cached_files": {},
                    "followed_from": None,
                    "sections": [],
                    "errors": [f"Processing failed: {e}"],
                })

        return documents

    def _process_urls(self, url_docs: List[Dict], run_id: str) -> List[Dict]:
        """
        Process external URLs via run_pipeline().

        Uses cached scraped text where available to avoid re-scraping,
        but falls back to run_pipeline for the full AI extraction flow.
        """
        urls = [doc["source_uri"] for doc in url_docs]

        if not urls:
            return []

        try:
            result = run_pipeline(
                urls=urls,
                run_id=run_id,
                follow_links=False,
                run_mode="ai_always",
                triggered_by="automated_ingestion",
                tags=["automated"],
            )

            output_path = result["output_path"]
            with open(output_path, "r") as f:
                pipeline_output = json.load(f)

            # Normalize URL documents to match expected format for write_canonical_json
            normalized_docs = []
            for doc in pipeline_output.get("documents", []):
                normalized_doc = {
                    "uri": doc.get("source", {}).get("uri", doc.get("uri", "")),
                    "source_type": doc.get("source_type", "webpage"),
                    "cached_files": doc.get("cached_files", {}),
                    "followed_from": doc.get("followed_from"),
                    "sections": doc.get("sections", []),
                    "errors": doc.get("errors", []),
                }
                normalized_docs.append(normalized_doc)

            return normalized_docs

        except Exception as e:
            logger.error(f"Pipeline processing failed for URLs: {e}", exc_info=True)
            self.errors.append({
                "type": "pipeline_error",
                "message": str(e),
            })
            return []

    def _ingest_to_rag(self, pipeline_output: Dict) -> Dict:
        """
        Ingest processed sections into RAG vector database.

        Tracks all vector IDs per document in rag_vector_ids (JSON array)
        and cleans up stale vectors on re-ingestion.
        """
        stats = {
            "documents_processed": 0,
            "sections_ingested": 0,
            "documents_skipped": 0,
            "documents_failed": 0,
        }

        documents = pipeline_output.get("documents", [])

        for doc in documents:
            doc_id = doc["doc_id"]
            source_uri = doc["source"]["uri"]
            source_type = doc["source"]["type"]
            sections = doc.get("sections", [])

            if not sections:
                logger.warning(f"No sections to ingest for {doc_id}")
                stats["documents_skipped"] += 1
                continue

            logger.info(f"Ingesting {len(sections)} section(s) from {doc_id}")

            # Generate document_id for database lookup
            document_id = DocumentIngestionState.generate_document_id(
                title=source_uri,
                url=source_uri,
            )

            # Get or create database record
            db_record = self.db.query(DocumentIngestionState).filter(
                DocumentIngestionState.document_id == document_id
            ).first()

            # Compute content hash from all section text
            import hashlib
            all_content = "".join(s.get("text", "") for s in sections)
            content_hash = hashlib.sha256(all_content.encode("utf-8")).digest()

            if not db_record:
                db_record = DocumentIngestionState(
                    document_id=document_id,
                    content_hash=content_hash,
                    file_name=source_uri.split("/")[-1] if source_uri else None,
                    url=source_uri,
                    last_processed_at=datetime.now(timezone.utc),
                    last_content_update_at=datetime.now(timezone.utc),
                    rag_ingestion_status="processing",
                    sections_total=len(sections),
                )
                self.db.add(db_record)
                self.db.flush()
            else:
                # Update existing record
                db_record.content_hash = content_hash
                db_record.last_content_update_at = datetime.now(timezone.utc)
                db_record.rag_ingestion_status = "processing"
                db_record.sections_total = len(sections)
                db_record.last_processed_at = datetime.now(timezone.utc)
                self.db.flush()

            # Load old vector IDs for cleanup after successful re-ingestion
            old_vector_ids = set()
            if db_record.rag_vector_ids:
                try:
                    old_vector_ids = set(json.loads(db_record.rag_vector_ids))
                except (json.JSONDecodeError, TypeError):
                    logger.warning(f"Could not parse rag_vector_ids for {document_id}")
            # Fallback: include the single rag_vector_id if present
            if db_record.rag_vector_id and db_record.rag_vector_id not in old_vector_ids:
                old_vector_ids.add(db_record.rag_vector_id)

            # Ingest each section
            sections_succeeded = 0
            section_errors = []
            new_vector_ids = []

            for section in sections:
                try:
                    result = store_document(
                        title=section["section_id"],
                        content=section["text"],
                        metadata={
                            "doc_id": doc_id,
                            "section_id": section["section_id"],
                            "source_type": source_type,
                            "source_uri": source_uri,
                            "section_hash": section["section_hash"],
                        },
                    )

                    vector_id = result.get("vector_id")
                    new_vector_ids.append(vector_id)

                    # Keep first vector_id as representative (backward compat)
                    if sections_succeeded == 0:
                        db_record.rag_vector_id = vector_id
                        db_record.rag_namespace = result.get("namespace")

                    sections_succeeded += 1
                    stats["sections_ingested"] += 1

                except Exception as e:
                    logger.error(f"Failed to ingest section {section['section_id']}: {e}")
                    section_errors.append({
                        "section_id": section["section_id"],
                        "error": str(e),
                    })

            # Update database record with results
            db_record.sections_processed = sections_succeeded
            db_record.rag_last_ingested_at = datetime.now(timezone.utc)

            if sections_succeeded == len(sections):
                # All sections succeeded
                db_record.rag_ingestion_status = "completed"
                db_record.rag_error_message = None
                db_record.rag_retry_count = 0
                stats["documents_processed"] += 1

                # Store all new vector IDs as JSON array
                db_record.rag_vector_ids = json.dumps(new_vector_ids)

                # Delete stale old vectors that are not in the new set
                new_set = set(new_vector_ids)
                stale_ids = old_vector_ids - new_set
                if stale_ids:
                    logger.info(f"Cleaning up {len(stale_ids)} stale vector(s) for {document_id}")
                    for stale_id in stale_ids:
                        try:
                            delete_document(
                                vector_id=stale_id,
                                namespace=db_record.rag_namespace,
                            )
                            logger.info(f"Deleted stale vector: {stale_id}")
                        except Exception as e:
                            logger.warning(f"Failed to delete stale vector {stale_id}: {e}")

            elif sections_succeeded > 0:
                # Partial success — don't delete old vectors (keep them as fallback)
                db_record.rag_ingestion_status = "failed"
                db_record.rag_error_message = json.dumps(section_errors[:5])
                db_record.rag_retry_count += 1
                stats["documents_failed"] += 1

                if db_record.rag_retry_count >= DEFAULT_MAX_RETRIES:
                    db_record.rag_ingestion_status = "permanently_failed"
                    logger.error(f"Document {document_id} permanently failed after {DEFAULT_MAX_RETRIES} retries")

                self.errors.append({
                    "type": "partial_ingestion_failure",
                    "document_id": document_id,
                    "sections_succeeded": sections_succeeded,
                    "sections_total": len(sections),
                    "errors": section_errors,
                })
            else:
                # Total failure — keep old vectors (don't delete anything)
                db_record.rag_ingestion_status = "failed"
                db_record.rag_error_message = json.dumps(section_errors[:5])
                db_record.rag_retry_count += 1
                stats["documents_failed"] += 1

                if db_record.rag_retry_count >= DEFAULT_MAX_RETRIES:
                    db_record.rag_ingestion_status = "permanently_failed"

                self.errors.append({
                    "type": "total_ingestion_failure",
                    "document_id": document_id,
                    "error": "All sections failed to ingest",
                })

            # Update content hash
            full_text = "\n\n".join(s["text"] for s in sections)
            db_record.content_hash = DocumentIngestionState.compute_content_hash(full_text)
            db_record.last_content_update_at = datetime.now(timezone.utc)

            # Commit database changes for this document
            try:
                self.db.commit()
            except Exception as e:
                logger.error(f"Failed to commit database changes for {document_id}: {e}")
                self.db.rollback()
                stats["documents_failed"] += 1
                self.errors.append({
                    "type": "database_error",
                    "document_id": document_id,
                    "error": str(e),
                })

        return stats

    def _build_result(
        self,
        status: str,
        run_id: str,
        documents_processed: int,
        sections_ingested: int,
        documents_skipped: int,
        documents_failed: int,
    ) -> IngestionResult:
        """Build IngestionResult object."""
        end_time = datetime.now(timezone.utc)
        processing_time = (end_time - self.start_time).total_seconds()

        return IngestionResult(
            status=status,
            run_id=run_id,
            documents_processed=documents_processed,
            sections_ingested=sections_ingested,
            documents_skipped=documents_skipped,
            documents_failed=documents_failed,
            processing_time_seconds=round(processing_time, 2),
            errors=self.errors,
            dry_run=self.dry_run,
        )


def run_automated_ingestion(
    db_session: Session,
    force_reprocess: bool = False,
    document_ids: Optional[List[str]] = None,
    dry_run: bool = False,
) -> IngestionResult:
    """
    Run automated ingestion workflow.

    This is the main entry point for automated ingestion.

    Args:
        db_session: SQLAlchemy database session
        force_reprocess: Ignore hash comparison, reprocess all
        document_ids: Only process these specific document IDs
        dry_run: Report changes without ingesting

    Returns:
        IngestionResult with summary statistics

    Raises:
        LockAlreadyHeld: If another ingestion is already running
    """
    orchestrator = IngestionOrchestrator(
        db_session=db_session,
        dry_run=dry_run,
    )

    return orchestrator.run(
        force_reprocess=force_reprocess,
        document_ids=document_ids,
    )

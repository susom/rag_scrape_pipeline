"""
CRIP Pipeline - Main orchestration module.

Provides:
- run_pipeline(): Core pipeline function for programmatic use
- main(): CLI wrapper
"""

import os
import csv
import hashlib
from datetime import datetime, timezone
from typing import Literal

from rag_pipeline.scraping.scraper import scrape_url
from rag_pipeline.scraping.pdf_parser import process_pdfs
from rag_pipeline.storage.storage import StorageManager
from rag_pipeline.utils.logger import setup_logger
from rag_pipeline.processing.sliding_window import SlidingWindowParser
from rag_pipeline.processing.ai_client import DEFAULT_MODEL
from rag_pipeline.output_json import write_canonical_json, generate_run_id

logger = setup_logger()

# Follow mode type
FollowMode = Literal["none", "attachments"]


def run_pipeline(
    urls: list[str],
    run_id: str,
    follow_links: bool = True,
    follow_mode: FollowMode | None = None,
    run_mode: str = "ai_always",
    triggered_by: str = "main",
    tags: list[str] | None = None,
    model: str | None = None,
) -> dict:
    """
    Run the CRIP pipeline on a list of URLs.

    Args:
        urls: List of URLs to process
        run_id: Unique run identifier (from generate_run_id())
        follow_links: Legacy param - if False, sets follow_mode="none"
        follow_mode: "none" | "attachments" - controls what gets followed
            - "none": Don't follow any links
            - "attachments": Follow PDF/DOC/DOCX links found in main content only
        run_mode: "deterministic" | "ai_auto" | "ai_always"
        triggered_by: "web_api" | "cli" | "main"
        tags: Optional run-level tags
        model: AI model to use (defaults to gpt-4.1)

    Returns:
        Dict with keys: run_id, output_path, stats, warnings
    """
    start_time = datetime.now(timezone.utc)

    # Resolve follow_mode from legacy follow_links if not explicitly set
    if follow_mode is None:
        follow_mode = "attachments" if follow_links else "none"

    storage_mode = os.getenv("STORAGE_MODE", "local")
    storage = StorageManager(storage_mode)

    raw_dir = os.path.join("cache", "raw")
    os.makedirs(raw_dir, exist_ok=True)

    # Resolve model
    resolved_model = model or DEFAULT_MODEL

    documents = []
    warnings = []
    parser = SlidingWindowParser(model=resolved_model)

    for url in urls:
        logger.info(f"Processing URL: {url}")

        # --- Scrape the page ---
        follow_attachments = (follow_mode == "attachments")
        scrape_result = scrape_url(url, follow_attachments=follow_attachments)

        # --- Process the main page ---
        page_doc = process_page_content(
            url=url,
            scrape_result=scrape_result,
            parser=parser,
            storage=storage,
            raw_dir=raw_dir,
            warnings=warnings,
        )
        documents.append(page_doc)

        # --- Process attachments as separate documents ---
        if follow_mode == "attachments" and scrape_result["attachments"]:
            for attachment in scrape_result["attachments"]:
                attachment_doc = process_attachment(
                    attachment=attachment,
                    parent_url=url,
                    parser=parser,
                    storage=storage,
                    raw_dir=raw_dir,
                    warnings=warnings,
                )
                documents.append(attachment_doc)

    # --- Write canonical JSON output ---
    end_time = datetime.now(timezone.utc)
    result = write_canonical_json(
        run_id=run_id,
        run_mode=run_mode,
        follow_links=follow_links,
        triggered_by=triggered_by,
        documents=documents,
        warnings=warnings,
        start_time=start_time,
        end_time=end_time,
        tags=tags,
        model_hint=resolved_model,
    )

    # --- Write CSV report (operational artifact) ---
    report_rows = [
        {
            "url": doc["uri"],
            "source_type": doc["source_type"],
            "followed_from": doc.get("followed_from") or "",
            "section_count": len(doc["sections"]),
            "errors": ";".join(doc["errors"]),
        }
        for doc in documents
    ]
    write_report(report_rows)

    # --- Upload to GCS if configured ---
    storage.upload_artifacts()

    logger.info(f"Pipeline complete. Output: {result['output_path']}")
    return result


def process_page_content(
    url: str,
    scrape_result: dict,
    parser: SlidingWindowParser,
    storage: StorageManager,
    raw_dir: str,
    warnings: list,
) -> dict:
    """Process scraped page content into a document record."""
    doc_errors = []
    doc_sections = []

    if scrape_result["error"]:
        doc_errors.append(scrape_result["error"])
        warnings.append({"level": "error", "message": scrape_result["error"], "uri": url})

    cached_path = scrape_result["cached_path"]

    # Process through sliding window if we have content
    if cached_path and os.path.exists(cached_path):
        try:
            count, sections = parser.process_file(cached_path, "", thinker_name="WebPage")
            doc_sections.extend(sections)
            logger.info(f"Extracted {count} sections from {url}")
        except Exception as e:
            doc_errors.append(f"Processing failed: {e}")
            warnings.append({"level": "error", "message": f"Processing failed: {e}", "uri": url})

    return {
        "uri": url,
        "source_type": "url",
        "cached_files": {"raw_text": cached_path},
        "followed_from": None,
        "sections": doc_sections,
        "errors": doc_errors,
    }


def process_attachment(
    attachment: dict,
    parent_url: str,
    parser: SlidingWindowParser,
    storage: StorageManager,
    raw_dir: str,
    warnings: list,
) -> dict:
    """Process an attachment (PDF/DOCX) as a separate document."""
    attachment_url = attachment["url"]
    attachment_type = attachment["type"]  # "pdf", "doc", "docx"
    doc_errors = []
    doc_sections = []
    cached_path = None

    logger.info(f"Processing attachment: {attachment_url} (type={attachment_type}, from={parent_url})")

    try:
        if attachment_type == "pdf":
            # Use existing PDF parser
            text = process_pdfs(attachment_url)
            if text:
                filename = url_to_filename(attachment_url, ext="pdf.txt")
                cached_path = os.path.join(raw_dir, filename)
                storage.save_file(cached_path, text)

                # Process through sliding window
                count, sections = parser.process_file(cached_path, "", thinker_name="PDF")
                doc_sections.extend(sections)
                logger.info(f"Extracted {count} sections from PDF: {attachment_url}")
            else:
                doc_errors.append("PDF extraction returned empty content")

        elif attachment_type in ("doc", "docx"):
            # DOCX handling - download and parse
            import requests
            import tempfile
            try:
                import docx2txt
            except ImportError:
                doc_errors.append("docx2txt not installed - cannot process DOCX")
                docx2txt = None

            if docx2txt:
                resp = requests.get(attachment_url, timeout=30)
                resp.raise_for_status()

                with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
                    tmp.write(resp.content)
                    tmp_path = tmp.name

                text = docx2txt.process(tmp_path)
                os.unlink(tmp_path)

                if text:
                    filename = url_to_filename(attachment_url, ext="docx.txt")
                    cached_path = os.path.join(raw_dir, filename)
                    storage.save_file(cached_path, text)

                    count, sections = parser.process_file(cached_path, "", thinker_name="DOCX")
                    doc_sections.extend(sections)
                    logger.info(f"Extracted {count} sections from DOCX: {attachment_url}")
                else:
                    doc_errors.append("DOCX extraction returned empty content")

    except Exception as e:
        error_msg = f"Attachment processing failed: {e}"
        doc_errors.append(error_msg)
        warnings.append({"level": "warn", "message": error_msg, "uri": attachment_url})
        logger.error(error_msg)

    return {
        "uri": attachment_url,
        "source_type": attachment_type,
        "cached_files": {"raw_text": cached_path},
        "followed_from": parent_url,
        "sections": doc_sections,
        "errors": doc_errors,
    }


def main(urls: list[str] | None = None, follow_links: bool = True):
    """
    CLI entrypoint wrapper around run_pipeline().

    Args:
        urls: List of URLs to process. If None, reads from config/urls.txt
        follow_links: Whether to follow attachment links in main content
    """
    if urls is None:
        with open("config/urls.txt", "r") as f:
            urls = [line.strip() for line in f if line.strip()]

    if not urls:
        logger.error("No URLs to process")
        return

    run_id = generate_run_id(urls)
    result = run_pipeline(
        urls=urls,
        run_id=run_id,
        follow_links=follow_links,
        run_mode="ai_always",
        triggered_by="main",
    )

    print(f"\n{'='*60}")
    print(f"Run ID: {result['run_id']}")
    print(f"Output: {result['output_path']}")
    print(f"Documents: {result['stats']['documents_processed']}")
    print(f"Sections: {result['stats']['total_sections']}")
    print(f"Time: {result['stats']['processing_time_seconds']}s")
    if result['warnings']:
        print(f"Warnings: {len(result['warnings'])}")
    print(f"{'='*60}")


def write_report(report_rows, report_path="cache/report.csv"):
    """Write operational CSV report."""
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["url", "source_type", "followed_from", "section_count", "errors"]
        )
        writer.writeheader()
        for row in report_rows:
            writer.writerow(row)


def url_to_filename(url: str, ext: str = "txt") -> str:
    """Generate safe filename from URL with hash suffix."""
    basename = os.path.basename(url.rstrip("/"))
    if not basename or "." not in basename:
        basename = url
    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    safe_name = f"{basename[:40]}_{url_hash}.{ext}"
    return (
        safe_name.replace("/", "_")
        .replace("?", "_")
        .replace("&", "_")
        .replace("=", "_")
    )


if __name__ == "__main__":
    main()

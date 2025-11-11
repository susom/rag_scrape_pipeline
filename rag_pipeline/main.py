import os
import sys
import hashlib
import csv

from rag_pipeline.scraping.scraper import scrape_urls
from rag_pipeline.scraping.pdf_parser import process_pdfs
from rag_pipeline.storage.storage import StorageManager
from rag_pipeline.utils.logger import setup_logger
from rag_pipeline.processing.sliding_window import SlidingWindowParser

logger = setup_logger()


def main(urls: list[str] | None = None):
    storage_mode = os.getenv("STORAGE_MODE", "local")
    storage = StorageManager(storage_mode)

    raw_dir = os.path.join("cache", "raw")
    rag_ready_dir = os.path.join("cache", "rag_ready")
    os.makedirs(raw_dir, exist_ok=True)
    os.makedirs(rag_ready_dir, exist_ok=True)

    if urls is None:
        # fallback to file if not provided
        with open("config/urls.txt", "r") as f:
            urls = [line.strip() for line in f if line.strip()]

    report_rows = []

    for url in urls:
        logger.info(f"Scraping URL: {url}")
        errors = []
        html_content, pdf_links = scrape_urls(url)

        # --- Save raw HTML ---
        html_filename = ""
        html_path = ""
        if html_content:
            html_filename = url_to_filename(url, ext="html")
            html_path = os.path.join(raw_dir, html_filename)
            storage.save_file(html_path, html_content)
        else:
            errors.append("Failed to get HTML")

        # --- Save raw PDFs ---
        pdf_files = []
        for pdf_url in pdf_links:
            pdf_text = process_pdfs(pdf_url)
            pdf_filename = url_to_filename(pdf_url, ext="pdf.txt")
            pdf_path = os.path.join(raw_dir, pdf_filename)
            if not pdf_text:
                errors.append(f"Failed PDF extraction: {pdf_url}")
            storage.save_file(pdf_path, pdf_text)
            pdf_files.append(pdf_path)

        # --- Run sliding window on all raw files ---
        jsonl_outputs = []
        parser = SlidingWindowParser()
        raw_files = [f for f in [html_path] + pdf_files if f]
        for raw_file in raw_files:
            output_path = os.path.join(
                rag_ready_dir, os.path.basename(raw_file).replace(".txt", ".jsonl")
            )
            try:
                parser.process_file(raw_file, output_path, thinker_name="SourceDoc")
                jsonl_outputs.append(output_path)
            except Exception as e:
                errors.append(f"Sliding window failed: {raw_file} ({e})")

        report_rows.append({
            "url": url,
            "html_file": html_filename if html_content else "",
            "pdf_count": len(pdf_links),
            "pdf_files": ";".join(pdf_files),
            "jsonl_outputs": ";".join(jsonl_outputs),
            "errors": ";".join(errors)
        })

    write_report(report_rows)


def write_report(report_rows, report_path="cache/report.csv"):
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["url", "html_file", "pdf_count", "pdf_files", "jsonl_outputs", "errors"]
        )
        writer.writeheader()
        for row in report_rows:
            writer.writerow(row)


def url_to_filename(url: str, ext: str = "txt") -> str:
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

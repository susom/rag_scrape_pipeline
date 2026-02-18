"""
Content Fetcher - Unified content fetching from all sources.

Coordinates fetching from:
- SharePoint document library (manifest-only — no content download here)
- External URLs page (SharePoint page with URL list)
"""

import os
import re
from typing import List, Tuple, Optional
from datetime import datetime
from bs4 import BeautifulSoup
from rag_pipeline.sharepoint import SharePointGraphClient, SharePointItem, get_site_config
from rag_pipeline.utils.logger import setup_logger

logger = setup_logger()


def extract_urls_from_html(html: str) -> List[str]:
    """
    Extract all HTTP/HTTPS URLs from HTML content.

    Args:
        html: HTML content containing links

    Returns:
        List of unique URLs found in the HTML
    """
    if not html:
        return []

    try:
        soup = BeautifulSoup(html, "html.parser")

        # Extract from <a> tags
        urls = set()
        for link in soup.find_all("a", href=True):
            href = link["href"]
            if href.startswith("http://") or href.startswith("https://"):
                urls.add(href)

        # Also extract URLs from plain text (in case they're not linked)
        text = soup.get_text()
        url_pattern = r'https?://[^\s<>"{}|\\^`\[\]]+'
        text_urls = re.findall(url_pattern, text)
        urls.update(text_urls)

        return sorted(list(urls))

    except Exception as e:
        logger.error(f"Failed to extract URLs from HTML: {e}")
        return []


def fetch_content_sources(modified_since: Optional[datetime] = None) -> Tuple[List[SharePointItem], List[str]]:
    """
    Fetch content from all configured sources.

    Args:
        modified_since: Optional datetime to filter SharePoint files by modification date.
                       Files modified before this time are excluded (except external-urls.txt).

    Returns:
        Tuple of:
            - List of SharePointItem objects (manifest-only, from document library)
            - List of external URLs (from external-urls.txt file)

    Note:
        SharePoint items are manifest-only — they contain metadata and a
        download_url but no file content. The orchestrator downloads bytes
        when needed for hashing / text extraction.

        The file 'external-urls.txt' in Shared Documents is treated specially:
        - NOT processed through the RAG pipeline (not a document)
        - Always fetched regardless of modified_since date
        - URLs are extracted from its content and those URLs are scraped every run
    """
    sharepoint_items = []
    external_urls = []

    # Read automation-specific env vars for folder/drive
    folder_path = os.getenv("SHAREPOINT_FOLDER_PATH", "")
    drive_id = os.getenv("SHAREPOINT_DRIVE_ID") or None

    try:
        site_config = get_site_config()
        client = SharePointGraphClient(
            site_hostname=site_config.hostname,
            site_path=site_config.path,
        )

        # --- Fetch regular documents with date filter (if provided) ---
        try:
            # Get files modified since the cutoff (or all files if no cutoff)
            regular_items = client.get_document_manifest(
                folder_path=folder_path,
                modified_since=modified_since,
                drive_id=drive_id,
            )
            logger.info(f"Fetched {len(regular_items)} items from SharePoint manifest")

            # Separate external-urls.txt from regular documents
            special_url_file = next(
                (item for item in regular_items if item.name.lower() == "external-urls.txt"),
                None,
            )
            sharepoint_items = [
                item for item in regular_items if item.name.lower() != "external-urls.txt"
            ]
            logger.info(f"{len(sharepoint_items)} regular documents for processing")

        except Exception as e:
            logger.error(f"Failed to fetch regular SharePoint documents: {e}")
            regular_items = []

        # --- Always fetch external-urls.txt separately (bypass date filter) ---
        try:
            # Fetch ALL files to get external-urls.txt regardless of date
            all_items = client.get_document_manifest(
                folder_path=folder_path,
                modified_since=None,
                drive_id=drive_id,
            )
            
            # Find external-urls.txt
            url_file = None
            for item in all_items:
                if item.name.lower() == "external-urls.txt":
                    url_file = item
                    break

            if url_file:
                logger.info(f"Found external-urls.txt - extracting URLs (always fetched)")
                try:
                    import requests
                    resp = requests.get(url_file.download_url, timeout=30)
                    content = resp.text
                    # Extract URLs from text file content
                    file_urls = re.findall(r'https?://[^\s<>"\'\n]+', content)
                    external_urls.extend(file_urls)
                    logger.info(f"Extracted {len(file_urls)} URLs from external-urls.txt")
                except Exception as e:
                    logger.error(f"Failed to extract URLs from external-urls.txt: {e}")
            else:
                logger.warning("external-urls.txt not found in SharePoint")

        except Exception as e:
            logger.error(f"Failed to fetch external-urls.txt: {e}")

    except Exception as e:
        logger.error(f"Failed to initialize SharePoint client: {e}")

    return sharepoint_items, external_urls


def fetch_content_sources_stub() -> Tuple[List[SharePointItem], List[str]]:
    """
    Stub implementation that returns test data for manual testing.

    Use this during development/testing before SharePoint integration is ready.

    Returns:
        Test SharePointItem and URL for ingestion testing

    To revert to empty: Change back to `return [], []`
    """
    from datetime import datetime, timezone
    from rag_pipeline.sharepoint import SharePointItem

    logger.warning("Using stub content fetcher - returning TEST DATA for manual testing")

    # Test document (manifest-only — no content field)
    test_item = SharePointItem(
        sharepoint_id="test_doc_001",
        name="test_document.txt",
        item_type="file",
        url="https://sharepoint.example.com/sites/rag/test_document.txt",
        download_url="https://sharepoint.example.com/sites/rag/_api/download/test_document.txt",
        mime_type="text/plain",
        size=512,
        last_modified=datetime.now(timezone.utc),
    )

    # Test external URL (will be scraped and processed)
    test_urls = [
        "https://med.stanford.edu/irt.html"  # Replace with your test URL
    ]

    logger.info(f"Stub returning: 1 test item + {len(test_urls)} test URL(s)")

    return [test_item], test_urls

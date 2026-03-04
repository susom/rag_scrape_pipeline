"""
Content Fetcher - Unified content fetching from all sources.

Coordinates fetching from:
- SharePoint site pages (filtered by published + lastModifiedDateTime)
- External URLs page (always fetched regardless of date)
"""

import os
import re
from typing import List, Tuple, Optional, Dict, Any
from datetime import datetime, timezone
from dataclasses import dataclass
from rag_pipeline.sharepoint import SharePointGraphClient, get_site_config
from rag_pipeline.utils.logger import setup_logger

logger = setup_logger()


@dataclass
class SharePointPage:
    """
    Represents a SharePoint site page.
    """
    page_id: str
    title: str
    name: str
    url: str
    last_modified: Optional[datetime]
    publishing_level: str
    is_external_urls_page: bool = False


def extract_urls_from_text(text: str) -> List[str]:
    """
    Extract all HTTP/HTTPS URLs from plain text.

    Args:
        text: Text content containing URLs

    Returns:
        List of unique URLs found in the text
    """
    if not text:
        return []

    url_pattern = r'https?://[^\s<>"{}|\\^`\[\]]+'
    urls = re.findall(url_pattern, text)
    return sorted(list(set(urls)))


def fetch_content_sources(modified_since: Optional[datetime] = None) -> Tuple[List[SharePointPage], List[str]]:
    """
    Fetch content from all configured sources.

    Args:
        modified_since: Optional datetime to filter pages by modification date.
                       Pages modified before this time are excluded (except external URLs page).

    Returns:
        Tuple of:
            - List of SharePointPage objects (site pages to process)
            - List of external URLs (from external URLs page)

    Note:
        Regular pages: filtered by publishingState.level == "published" AND lastModifiedDateTime >= modified_since
        External URLs page: ALWAYS fetched regardless of date
    """
    sharepoint_pages = []
    external_urls = []

    # Get external URLs page ID from env
    external_urls_page_id = os.getenv("SHAREPOINT_EXTERNAL_URLS_PAGE_ID", "").strip()

    try:
        site_config = get_site_config()
        client = SharePointGraphClient(
            site_hostname=site_config.hostname,
            site_path=site_config.path,
        )

        # --- Fetch all site pages ---
        try:
            all_pages = list(client.get_site_pages(
                select=['id', 'title', 'name', 'webUrl', 'lastModifiedDateTime', 'publishingState']
            ))
            logger.info(f"Fetched {len(all_pages)} total pages from SharePoint")

        except Exception as e:
            logger.error(f"Failed to fetch site pages: {e}")
            return [], []

        # --- Separate external URLs page from regular pages ---
        regular_pages = []
        external_urls_page = None

        for page_data in all_pages:
            page_id = page_data.get("id", "")
            
            # Check if this is the external URLs page
            if page_id == external_urls_page_id:
                external_urls_page = page_data
                continue
            
            regular_pages.append(page_data)

        # --- Filter regular pages: published + date filter ---
        filtered_pages = []
        for page_data in regular_pages:
            # Check publishing state
            publishing = page_data.get("publishingState", {})
            level = publishing.get("level", "") if publishing else ""
            
            if level != "published":
                logger.debug(f"Skipping unpublished page: {page_data.get('title')} ({level})")
                continue
            
            # Check last modified date
            last_modified_str = page_data.get("lastModifiedDateTime")
            if last_modified_str:
                try:
                    last_modified = datetime.fromisoformat(last_modified_str.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    last_modified = None
            else:
                last_modified = None

            if modified_since and last_modified:
                if last_modified < modified_since:
                    logger.debug(f"Skipping old page: {page_data.get('title')} (modified {last_modified_str})")
                    continue

            # Parse last_modified for the SharePointPage object
            sharepoint_page = SharePointPage(
                page_id=page_data.get("id", ""),
                title=page_data.get("title", ""),
                name=page_data.get("name", ""),
                url=page_data.get("webUrl", ""),
                last_modified=last_modified,
                publishing_level=level,
            )
            filtered_pages.append(sharepoint_page)

        sharepoint_pages = filtered_pages
        logger.info(f"{len(sharepoint_pages)} published pages within date range for processing")

        # --- Always fetch external URLs page (bypass date filter) ---
        if external_urls_page:
            try:
                page_id = external_urls_page.get("id")
                logger.info(f"Fetching external URLs page: {external_urls_page.get('title')}")
                
                # Get text content from the page
                page_text = client.get_page_text_content(page_id)
                
                if page_text:
                    # Extract URLs from the text
                    urls = extract_urls_from_text(page_text)
                    external_urls.extend(urls)
                    logger.info(f"Extracted {len(urls)} URLs from external URLs page")
                else:
                    logger.warning(f"External URLs page returned empty content")

            except Exception as e:
                logger.error(f"Failed to fetch external URLs page: {e}")
        else:
            logger.warning(f"External URLs page not found (ID: {external_urls_page_id})")

    except Exception as e:
        logger.error(f"Failed to initialize SharePoint client: {e}")

    return sharepoint_pages, external_urls


def update_tracker_list(title: str, url: str, vector_id: str = None) -> bool:
    """
    Add an entry to the SharePoint ingestion tracker list.

    Args:
        title: Title of the page/URL
        url: The URL that was ingested
        vector_id: The vector ID from RAG ingestion (optional)

    Returns:
        True if successful, False otherwise
    """
    tracker_list_id = os.getenv("SHAREPOINT_TRACKER_LIST_ID", "").strip()
    if not tracker_list_id:
        logger.debug("No tracker list configured, skipping")
        return False

    try:
        site_config = get_site_config()
        client = SharePointGraphClient(
            site_hostname=site_config.hostname,
            site_path=site_config.path,
        )

        action = f"Ingested successfully"
        if vector_id:
            action = f"Ingested successfully (vector: {vector_id[:20]}...)"

        result = client.add_list_item(
            list_id=tracker_list_id,
            title=title,
            url=url,
            action=action,
        )
        logger.info(f"Added tracker entry: {title}")
        return True

    except Exception as e:
        logger.error(f"Failed to update tracker list: {e}")
        return False


def get_page_content(page_id: str) -> str:
    """
    Get the text content of a specific SharePoint page.
    
    Used by the orchestrator to process pages through the pipeline.

    Args:
        page_id: The SharePoint page ID

    Returns:
        Plain text content of the page
    """
    try:
        site_config = get_site_config()
        client = SharePointGraphClient(
            site_hostname=site_config.hostname,
            site_path=site_config.path,
        )
        return client.get_page_text_content(page_id)
    except Exception as e:
        logger.error(f"Failed to get page content for {page_id}: {e}")
        return ""


def fetch_content_sources_stub() -> Tuple[List[SharePointPage], List[str]]:
    """
    Stub implementation that returns test data for manual testing.

    Use this during development/testing before SharePoint integration is ready.

    Returns:
        Test SharePointPage and URL for ingestion testing
    """
    from datetime import datetime, timezone

    logger.warning("Using stub content fetcher - returning TEST DATA for manual testing")

    # Test page
    test_page = SharePointPage(
        page_id="test_page_001",
        title="Test Page",
        name="Test-Page.aspx",
        url="https://sharepoint.example.com/sites/RExI/SitePages/Test-Page.aspx",
        last_modified=datetime.now(timezone.utc),
        publishing_level="published",
    )

    # Test external URL
    test_urls = [
        "https://med.stanford.edu/irt.html"
    ]

    logger.info(f"Stub returning: 1 test page + {len(test_urls)} test URL(s)")

    return [test_page], test_urls

"""
Content Fetcher - Unified content fetching from all sources.

Coordinates fetching from:
- SharePoint site pages (filtered by published + lastModifiedDateTime)
- SharePoint document libraries (filtered by approval + lastModifiedDateTime)
- External URLs page or file (always fetched regardless of date when configured)
"""

import os
import re
from typing import List, Tuple, Optional, Dict, Any, Iterable
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


@dataclass
class SharePointFile:
    """
    Represents a SharePoint document library file.
    """
    file_id: str
    file_name: str
    url: str
    download_url: Optional[str]
    last_modified: Optional[datetime]
    library_name: Optional[str] = None
    parent_path: Optional[str] = None
    list_item_fields: Optional[dict] = None


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


def _default_library_prefixes() -> List[str]:
    return [f"Library {idx}" for idx in range(1, 8)]


def _library_matches(name: str, prefixes: List[str]) -> bool:
    return any(name.startswith(prefix) for prefix in prefixes)


def _normalize_approval_value(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return str(int(value))
    return str(value).strip()


def _is_approval_value_approved(value: Optional[str], field_name: str) -> bool:
    if value is None:
        return False
    value_norm = value.strip()
    if value_norm.lower() == "approved":
        return True
    if not value_norm.isdigit():
        return False
    numeric = int(value_norm)
    field_lower = field_name.lower()
    if "approval" in field_lower:
        return numeric == 3
    if "moderation" in field_lower:
        return numeric == 0
    return False


def _is_item_approved(fields: Optional[dict], approval_field: Optional[str]) -> bool:
    if not fields:
        return False

    if approval_field:
        value = _normalize_approval_value(fields.get(approval_field))
        return _is_approval_value_approved(value, approval_field)

    candidates = [
        "_ApprovalStatus",
        "ApprovalStatus",
        "approvalStatus",
        "ContentApprovalStatus",
        "_ModerationStatus",
        "OData__ModerationStatus",
        "ModerationStatus",
    ]
    for key in candidates:
        if key in fields:
            value = _normalize_approval_value(fields.get(key))
            return _is_approval_value_approved(value, key)

    return False


def _fetch_external_urls_file(
    client: SharePointGraphClient,
    drives: Iterable[dict],
    file_name: str,
    drive_name: Optional[str] = None,
) -> List[str]:
    if not file_name:
        return []

    candidate_drives = []
    if drive_name:
        candidate_drives = [drive for drive in drives if drive.get("name") == drive_name]
    else:
        candidate_drives = list(drives)

    for drive in candidate_drives:
        drive_id = drive.get("id")
        if not drive_id:
            continue
        try:
            items = client.get_drive_items(drive_id=drive_id, recursive=True)
            for item in items:
                if item.get("folder"):
                    continue
                if item.get("name") != file_name:
                    continue
                download_url = item.get("@microsoft.graph.downloadUrl")
                if not download_url:
                    logger.warning(f"External URLs file missing download URL: {file_name}")
                    return []
                content = client.download_file_content(download_url)
                text = content.decode("utf-8", errors="ignore")
                return extract_urls_from_text(text)
        except Exception as e:
            logger.warning(f"Failed to read external URLs file from drive {drive.get('name')}: {e}")

    logger.warning("External URLs file not found in configured drives")
    return []


def fetch_content_sources(
    modified_since: Optional[datetime] = None,
    site_name: Optional[str] = None,
) -> Tuple[List[SharePointPage], List[SharePointFile], List[str]]:
    """
    Fetch content from all configured sources.

    Args:
        modified_since: Optional datetime to filter pages by modification date.
                       Pages modified before this time are excluded (except external URLs page).
        site_name: Optional site name to fetch from (None for default site).

    Returns:
        Tuple of:
            - List of SharePointPage objects (site pages to process)
            - List of SharePointFile objects (document library files to process)
            - List of external URLs (from external URLs page or file)

    Note:
        Regular pages: filtered by publishingState.level == "published" AND lastModifiedDateTime >= modified_since
        External URLs page: ALWAYS fetched regardless of date (if configured for this site)
    """
    sharepoint_pages: List[SharePointPage] = []
    sharepoint_files: List[SharePointFile] = []
    external_urls: List[str] = []

    try:
        site_config = get_site_config(site_name)
        client = SharePointGraphClient(
            site_hostname=site_config.hostname,
            site_path=site_config.path,
            tenant_id=site_config.tenant_id,
            client_id=site_config.client_id,
            client_secret=site_config.client_secret,
        )
        content_source = (site_config.content_source or "site_pages").lower()

        if content_source == "site_pages":
            # Get external URLs page ID from env (site-specific or default)
            # Pattern: SHAREPOINT_SITE_{NAME}_EXTERNAL_URLS_PAGE_ID or SHAREPOINT_EXTERNAL_URLS_PAGE_ID
            if site_name and site_name != "default":
                external_urls_page_id = os.getenv(
                    f"SHAREPOINT_SITE_{site_name.upper()}_EXTERNAL_URLS_PAGE_ID", ""
                ).strip()
            else:
                external_urls_page_id = os.getenv("SHAREPOINT_EXTERNAL_URLS_PAGE_ID", "").strip()

            # --- Fetch all site pages ---
            try:
                all_pages = list(client.get_site_pages(
                    select=['id', 'title', 'name', 'webUrl', 'lastModifiedDateTime', 'publishingState']
                ))
                logger.info(f"Fetched {len(all_pages)} total pages from SharePoint")

            except Exception as e:
                logger.error(f"Failed to fetch site pages: {e}")
                return [], [], []

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
                        logger.warning("External URLs page returned empty content")

                except Exception as e:
                    logger.error(f"Failed to fetch external URLs page: {e}")
            else:
                if external_urls_page_id:
                    logger.warning(f"External URLs page not found (ID: {external_urls_page_id})")
                else:
                    logger.debug("No external URLs page configured for this site")

        elif content_source == "document_library":
            library_prefixes = site_config.library_prefixes or _default_library_prefixes()
            drives = list(client.get_drives())
            allowed_drives = [drive for drive in drives if _library_matches(drive.get("name", ""), library_prefixes)]

            if not allowed_drives:
                logger.warning("No document libraries matched configured prefixes")
            else:
                logger.info(f"Matched {len(allowed_drives)} document libraries for processing")

            for drive in allowed_drives:
                drive_id = drive.get("id")
                drive_name = drive.get("name", "")
                if not drive_id:
                    continue

                manifest = client.get_document_manifest(
                    drive_id=drive_id,
                    modified_since=modified_since,
                    include_fields=True,
                    library_name=drive_name,
                )

                for item in manifest:
                    if not _is_item_approved(item.list_item_fields, site_config.approval_field):
                        continue

                    sharepoint_files.append(SharePointFile(
                        file_id=item.sharepoint_id,
                        file_name=item.name,
                        url=item.url,
                        download_url=item.download_url,
                        last_modified=item.last_modified,
                        library_name=item.library_name,
                        parent_path=item.parent_path,
                        list_item_fields=item.list_item_fields,
                    ))

            if site_config.external_urls_file:
                urls = _fetch_external_urls_file(
                    client=client,
                    drives=drives,
                    file_name=site_config.external_urls_file,
                    drive_name=site_config.external_urls_drive,
                )
                external_urls.extend(urls)
                if urls:
                    logger.info(f"Extracted {len(urls)} URLs from external URLs file")
            else:
                logger.debug("No external URLs file configured for this site")

        else:
            logger.error(f"Unsupported content_source '{content_source}' for site {site_config.name}")

    except Exception as e:
        logger.error(f"Failed to initialize SharePoint client: {e}")

    return sharepoint_pages, sharepoint_files, external_urls


def _escape_odata_value(value: str) -> str:
    return value.replace("'", "''")


def _increment_version(current_value: Any) -> int:
    try:
        if current_value is None:
            return 1
        if isinstance(current_value, (int, float)):
            return int(current_value) + 1
        value_str = str(current_value).strip()
        return int(value_str) + 1
    except (ValueError, TypeError):
        return 1


def _resolve_tracker_field_names(
    client: SharePointGraphClient,
    list_id: str,
    site_name: Optional[str],
) -> Dict[str, Optional[str]]:
    display_names = {
        "content_section": "Content Section",
        "document_title": "Document Title",
        "version": "RExI Version",
        "summary": "Summary",
        "ingestion_date": "Ingestion Date",
    }

    overrides = {}
    if site_name and site_name != "default":
        prefix = f"SHAREPOINT_SITE_{site_name.upper()}_TRACKER_FIELD_"
        overrides = {
            "content_section": os.getenv(f"{prefix}CONTENT_SECTION", "").strip() or None,
            "document_title": os.getenv(f"{prefix}DOCUMENT_TITLE", "").strip() or None,
            "version": os.getenv(f"{prefix}VERSION", "").strip() or None,
            "summary": os.getenv(f"{prefix}SUMMARY", "").strip() or None,
            "ingestion_date": os.getenv(f"{prefix}INGESTION_DATE", "").strip() or None,
        }

    columns = list(client.get_list_columns(list_id))
    name_map = {
        (col.get("displayName") or "").strip().lower(): col.get("name")
        for col in columns
        if col.get("displayName")
    }
    internal_names = {col.get("name") for col in columns if col.get("name")}

    fallback_internal = {
        "content_section": ["LinkTitle", "ContentSectionHeader"],
        "document_title": ["Content", "DocumentTitle", "Title"],
        "version": ["RExIVersion"],
        "summary": ["Summary"],
        "ingestion_date": ["IngestionDate"],
    }

    resolved = {}
    for key, display_name in display_names.items():
        if overrides.get(key):
            resolved[key] = overrides[key]
            continue
        resolved[key] = name_map.get(display_name.lower())
        if not resolved[key]:
            for candidate in fallback_internal.get(key, []):
                if candidate in internal_names:
                    resolved[key] = candidate
                    break

    if resolved.get("content_section") in {"LinkTitle", "LinkTitleNoMenu"} and "Title" in internal_names:
        resolved["content_section"] = "Title"

    return resolved


def update_tracker_list(
    title: str,
    url: str,
    vector_id: str = None,
    site_name: Optional[str] = None,
    content_section: Optional[str] = None,
    document_title: Optional[str] = None,
    summary: Optional[str] = None,
    ingestion_date: Optional[str] = None,
    increment_version: bool = False,
) -> bool:
    """
    Add an entry to the SharePoint ingestion tracker list.

    Args:
        title: Title of the page/URL
        url: The URL that was ingested
        vector_id: The vector ID from RAG ingestion (optional)
        site_name: Optional site name (None for default site)

    Returns:
        True if successful, False otherwise
    """
    try:
        site_config = get_site_config(site_name)
        client = SharePointGraphClient(
            site_hostname=site_config.hostname,
            site_path=site_config.path,
            tenant_id=site_config.tenant_id,
            client_id=site_config.client_id,
            client_secret=site_config.client_secret,
        )

        # Site-specific or default tracker list ID/name
        tracker_list_id = ""
        tracker_list_name = ""
        if site_name and site_name != "default":
            tracker_list_id = os.getenv(
                f"SHAREPOINT_SITE_{site_name.upper()}_TRACKER_LIST_ID", ""
            ).strip()
            tracker_list_name = os.getenv(
                f"SHAREPOINT_SITE_{site_name.upper()}_TRACKER_LIST_NAME", ""
            ).strip()
        else:
            tracker_list_id = os.getenv("SHAREPOINT_TRACKER_LIST_ID", "").strip()
            tracker_list_name = os.getenv("SHAREPOINT_TRACKER_LIST_NAME", "").strip()

        if not tracker_list_id and tracker_list_name:
            tracker_list_id = client.get_list_by_name(tracker_list_name).get("id", "")

        if not tracker_list_id:
            logger.debug("No tracker list configured, skipping")
            return False

        action = "Ingested successfully"
        if vector_id:
            action = f"Ingested successfully (vector: {vector_id[:20]}...)"

        use_rich_fields = any([
            content_section,
            document_title,
            summary is not None,
            ingestion_date is not None,
            increment_version,
        ])
        if not use_rich_fields:
            client.add_list_item(
                list_id=tracker_list_id,
                title=title,
                url=url,
                action=action,
            )
            logger.info(f"Added tracker entry: {title}")
            return True

        field_names = _resolve_tracker_field_names(client, tracker_list_id, site_name)
        fields_to_set: Dict[str, Any] = {}

        doc_title_value = document_title or title
        if field_names.get("document_title"):
            fields_to_set[field_names["document_title"]] = doc_title_value
        else:
            fields_to_set["Title"] = doc_title_value

        if content_section and field_names.get("content_section"):
            fields_to_set[field_names["content_section"]] = content_section

        if summary is not None and field_names.get("summary"):
            fields_to_set[field_names["summary"]] = summary

        if ingestion_date is not None and field_names.get("ingestion_date"):
            fields_to_set[field_names["ingestion_date"]] = ingestion_date

        existing_item = None
        if doc_title_value and field_names.get("document_title"):
            filter_query = f"fields/{field_names['document_title']} eq '{_escape_odata_value(doc_title_value)}'"
            if content_section and field_names.get("content_section"):
                filter_query += f" and fields/{field_names['content_section']} eq '{_escape_odata_value(content_section)}'"
            try:
                items = list(client.get_list_items(
                    list_id=tracker_list_id,
                    max_items=2,
                    filter_query=filter_query,
                ))
                existing_item = items[0] if items else None
            except Exception as e:
                logger.warning(f"Tracker lookup failed, will create new entry: {e}")
                existing_item = None

            if not existing_item and content_section and field_names.get("content_section"):
                fallback_filter = f"fields/{field_names['document_title']} eq '{_escape_odata_value(doc_title_value)}'"
                try:
                    fallback_items = list(client.get_list_items(
                        list_id=tracker_list_id,
                        max_items=2,
                        filter_query=fallback_filter,
                    ))
                    if len(fallback_items) == 1:
                        existing_item = fallback_items[0]
                except Exception as e:
                    logger.warning(f"Tracker fallback lookup failed, will create new entry: {e}")

        if existing_item:
            current_fields = existing_item.get("fields", {})
            if increment_version and field_names.get("version"):
                fields_to_set[field_names["version"]] = str(_increment_version(
                    current_fields.get(field_names["version"])
                ))

            client.update_list_item_fields(
                list_id=tracker_list_id,
                item_id=existing_item.get("id"),
                fields=fields_to_set,
            )
            logger.info(f"Updated tracker entry: {doc_title_value}")
            return True

        if increment_version and field_names.get("version"):
            fields_to_set[field_names["version"]] = "1"

        client.add_list_item(
            list_id=tracker_list_id,
            fields=fields_to_set,
        )
        logger.info(f"Added tracker entry: {doc_title_value}")
        return True

    except Exception as e:
        logger.error(f"Failed to update tracker list: {e}")
        return False


def get_page_content(page_id: str, site_name: Optional[str] = None) -> str:
    """
    Get the text content of a specific SharePoint page.
    
    Used by the orchestrator to process pages through the pipeline.

    Args:
        page_id: The SharePoint page ID
        site_name: Optional site name (None for default site)

    Returns:
        Plain text content of the page
    """
    try:
        site_config = get_site_config(site_name)
        client = SharePointGraphClient(
            site_hostname=site_config.hostname,
            site_path=site_config.path,
            tenant_id=site_config.tenant_id,
            client_id=site_config.client_id,
            client_secret=site_config.client_secret,
        )
        return client.get_page_text_content(page_id)
    except Exception as e:
        logger.error(f"Failed to get page content for {page_id}: {e}")
        return ""


def fetch_content_sources_stub() -> Tuple[List[SharePointPage], List[SharePointFile], List[str]]:
    """
    Stub implementation that returns test data for manual testing.

    Use this during development/testing before SharePoint integration is ready.

    Returns:
        Test SharePointPage and URL for ingestion testing
    """
    from datetime import datetime, timezone

    logger.warning("Using stub content fetcher - returning TEST DATA for manual testing")

    # Test file
    test_file = SharePointFile(
        file_id="test_file_001",
        file_name="Test-Doc.pdf",
        url="https://sharepoint.example.com/sites/RExI/Shared%20Documents/Test-Doc.pdf",
        download_url="https://sharepoint.example.com/download/Test-Doc.pdf",
        last_modified=datetime.now(timezone.utc),
        library_name="Library 1: Prologue Document",
        parent_path="/drives/mock/root:/Prologue Document",
    )

    # Test external URL
    test_urls = [
        "https://med.stanford.edu/irt.html"
    ]

    logger.info(f"Stub returning: 1 test file + {len(test_urls)} test URL(s)")

    return [], [test_file], test_urls

"""
Content Fetcher - Unified content fetching from all sources.

Coordinates fetching from:
- SharePoint site pages (filtered by published + lastModifiedDateTime)
- SharePoint document libraries (filtered by approval + lastModifiedDateTime)
- External URLs page or file (always fetched regardless of date when configured)
"""

import os
import re
import html
import requests
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
    drive_id: Optional[str] = None
    created_at: Optional[datetime] = None
    last_modified: Optional[datetime] = None
    modified_by: Optional[str] = None
    created_by: Optional[str] = None
    approver: Optional[str] = None
    content_editor: Optional[str] = None
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


def _extract_approver_name(fields: Optional[dict]) -> Optional[str]:
    if not fields:
        return None

    candidates = [
        "_ApprovalRespondedBy",
        "ApprovalRespondedBy",
        "_ApprovalAssignedTo",
        "ApprovalAssignedTo",
    ]
    for key in candidates:
        value = fields.get(key)
        if not value:
            continue
        if isinstance(value, list) and value:
            entry = value[0]
            if isinstance(entry, dict):
                return entry.get("LookupValue") or entry.get("displayName") or entry.get("Email")
            return str(entry)
        if isinstance(value, dict):
            return value.get("LookupValue") or value.get("displayName") or value.get("Email")
        return str(value)

    return None


def _extract_field_value(fields: Optional[dict], field_name: Optional[str]) -> Optional[str]:
    if not fields or not field_name:
        return None
    value = fields.get(field_name)
    if value is None:
        return None
    if isinstance(value, list) and value:
        entry = value[0]
        if isinstance(entry, dict):
            return entry.get("LookupValue") or entry.get("displayName") or entry.get("Email")
        return str(entry)
    if isinstance(value, dict):
        return value.get("LookupValue") or value.get("displayName") or value.get("Email")
    return str(value)


def _resolve_library_field_name(
    client: SharePointGraphClient,
    drive_id: Optional[str],
    display_name: Optional[str],
) -> Optional[str]:
    if not drive_id or not display_name:
        return None
    try:
        list_info = client.get_drive_list(drive_id)
        list_id = list_info.get("id")
        if not list_id:
            return None
        columns = list(client.get_list_columns(list_id))
    except Exception as e:
        logger.warning(f"Failed to resolve library fields for drive {drive_id}: {e}")
        return None

    display_key = display_name.strip().lower()
    name_map = {
        (col.get("displayName") or "").strip().lower(): col.get("name")
        for col in columns
        if col.get("displayName")
    }
    internal_names = {col.get("name") for col in columns if col.get("name")}

    if display_name in internal_names:
        return display_name
    return name_map.get(display_key)


def _extract_last_content_editor(
    client: SharePointGraphClient,
    drive_id: Optional[str],
    file_id: Optional[str],
    approver_name: Optional[str],
    fallback_editor: Optional[str],
    max_activities: int = 20,
) -> Optional[str]:
    """
    Extract the last content editor using the drive item activities API.

    Activities track actual edits (including minor/draft versions) even when
    approval workflows collapse minor versions into major versions.
    Falls back to version history if activities are unavailable.
    """
    if fallback_editor and approver_name:
        if fallback_editor.strip().lower() != approver_name.strip().lower():
            return fallback_editor
    if not drive_id or not file_id:
        return fallback_editor
    if not approver_name:
        return fallback_editor

    approver_norm = approver_name.strip().lower()

    # Primary: use activities API (tracks actual edits, not just published versions)
    try:
        activities = list(client.get_drive_item_activities(
            drive_id=drive_id,
            item_id=file_id,
            max_items=max_activities,
        ))
        if activities:
            for activity in activities:
                action = activity.get("action", {})
                if "edit" not in action:
                    continue
                actor = activity.get("actor", {}).get("user", {}).get("displayName")
                if not actor:
                    continue
                if actor.strip().lower() == approver_norm:
                    continue
                return actor
    except Exception as e:
        logger.warning(f"Activities API failed for {file_id}, falling back to version history: {e}")

    # Fallback: version history (may miss minor versions collapsed by approval)
    try:
        versions = list(client.get_drive_item_versions(
            drive_id=drive_id,
            item_id=file_id,
            max_items=max_activities,
        ))
    except Exception as e:
        logger.warning(f"Failed to fetch version history for {file_id}: {e}")
        return fallback_editor

    if not versions:
        return fallback_editor

    def _parse_version_time(version: dict) -> Optional[datetime]:
        raw = version.get("lastModifiedDateTime")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None

    def _get_version_editor(version: dict) -> Optional[str]:
        info = version.get("lastModifiedBy") or {}
        if info.get("user"):
            return info["user"].get("displayName")
        if info.get("application"):
            return info["application"].get("displayName")
        return None

    default_time = datetime.min.replace(tzinfo=timezone.utc)
    for version in sorted(versions, key=lambda v: _parse_version_time(v) or default_time, reverse=True):
        editor = _get_version_editor(version)
        if not editor:
            continue
        if editor.strip().lower() == approver_norm:
            continue
        return editor

    return fallback_editor


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
            library_drive_ids = site_config.library_drive_ids or []
            content_editor_field = site_config.content_editor_field
            content_editor_fields_by_drive: Dict[str, Optional[str]] = {}
            if library_drive_ids:
                allowed_drives = [drive for drive in drives if drive.get("id") in library_drive_ids]
                missing = [drive_id for drive_id in library_drive_ids if drive_id not in {d.get("id") for d in allowed_drives}]
                if missing:
                    logger.warning(f"Configured drive IDs not found: {missing}")
            else:
                allowed_drives = [drive for drive in drives if _library_matches(drive.get("name", ""), library_prefixes)]

            if not allowed_drives:
                if library_drive_ids:
                    logger.warning("No document libraries matched configured drive IDs")
                else:
                    logger.warning("No document libraries matched configured prefixes")
            else:
                logger.info(f"Matched {len(allowed_drives)} document libraries for processing")

            for drive in allowed_drives:
                drive_id = drive.get("id")
                drive_name = drive.get("name", "")
                if not drive_id:
                    continue

                if drive_id not in content_editor_fields_by_drive and content_editor_field:
                    content_editor_fields_by_drive[drive_id] = _resolve_library_field_name(
                        client=client,
                        drive_id=drive_id,
                        display_name=content_editor_field,
                    )

                manifest = client.get_document_manifest(
                    drive_id=drive_id,
                    modified_since=modified_since,
                    include_fields=True,
                    library_name=drive_name,
                )

                for item in manifest:
                    if not _is_item_approved(item.list_item_fields, site_config.approval_field):
                        continue

                    approver_name = _extract_approver_name(item.list_item_fields)
                    content_editor = _extract_field_value(
                        item.list_item_fields,
                        content_editor_fields_by_drive.get(drive_id),
                    )
                    if not content_editor:
                        content_editor = _extract_last_content_editor(
                            client=client,
                            drive_id=drive_id,
                            file_id=item.sharepoint_id,
                            approver_name=approver_name,
                            fallback_editor=item.last_modified_by,
                        )
                    sharepoint_files.append(SharePointFile(
                        file_id=item.sharepoint_id,
                        file_name=item.name,
                        url=item.url,
                        download_url=item.download_url,
                        drive_id=item.drive_id,
                        created_at=item.created_at,
                        last_modified=item.last_modified,
                        modified_by=item.last_modified_by,
                        created_by=item.created_by,
                        approver=approver_name,
                        content_editor=content_editor,
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


def _normalize_tracker_doc_title(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        return str(value).strip() or None
    text = re.sub(r"<[^>]+>", " ", value)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


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
        "document_link": "Document Link",
        "modified_by": "Modify By",
        "document_modified": "Document Modified",
        "document_modified_by": "Last Editor",
        "document_created": "Document Created",
        "approver": "Approver",
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
            "document_link": os.getenv(f"{prefix}DOCUMENT_LINK", "").strip() or None,
            "modified_by": os.getenv(f"{prefix}MODIFIED_BY", "").strip() or None,
            "document_modified": os.getenv(f"{prefix}DOCUMENT_MODIFIED", "").strip() or None,
            "document_modified_by": os.getenv(f"{prefix}DOCUMENT_MODIFIED_BY", "").strip() or None,
            "document_created": os.getenv(f"{prefix}DOCUMENT_CREATED", "").strip() or None,
            "approver": os.getenv(f"{prefix}APPROVER", "").strip() or None,
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
        "document_link": ["DocumentLink", "Document_x0020_Link", "Link", "Url", "URL"],
        "modified_by": ["ModifyBy", "Modify_x0020_By"],
        "document_modified": ["DocumentModified", "Document_x0020_Modified"],
        "document_modified_by": ["DocumentModifiedBy", "Document_x0020_Modified_x0020_By"],
        "document_created": ["DocumentCreated", "Document_x0020_Created"],
        "approver": ["Approver"],
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

    title_field = resolved.get("document_title")
    title_is_rich = False
    if title_field:
        for col in columns:
            if col.get("name") == title_field:
                text_info = col.get("text") or {}
                title_is_rich = text_info.get("textType") == "richText"
                break
    resolved["document_title_is_rich_text"] = title_is_rich

    return resolved


def update_tracker_list(
    title: str,
    url: str,
    vector_id: str = None,
    site_name: Optional[str] = None,
    content_section: Optional[str] = None,
    document_title: Optional[str] = None,
    document_link_text: Optional[str] = "Go to Page",
    modified_by: Optional[str] = None,
    document_modified: Optional[str] = None,
    document_modified_by: Optional[str] = None,
    document_created: Optional[str] = None,
    approver: Optional[str] = None,
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
            url,
            modified_by,
            document_modified,
            document_modified_by,
            document_created,
            approver,
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
        link_field_name = field_names.get("document_link")

        doc_title_raw = document_title or title
        title_is_rich = bool(field_names.get("document_title_is_rich_text"))
        doc_title_value = doc_title_raw
        doc_title_html = None
        if title_is_rich and url:
            doc_title_html = (
                f"<a href=\"{html.escape(url, quote=True)}\">"
                f"{html.escape(doc_title_raw or '', quote=True)}</a>"
            )
            doc_title_value = doc_title_html

        if field_names.get("document_title"):
            fields_to_set[field_names["document_title"]] = doc_title_value
        else:
            fields_to_set["Title"] = doc_title_value

        if content_section and field_names.get("content_section"):
            fields_to_set[field_names["content_section"]] = content_section

        if modified_by and field_names.get("modified_by"):
            fields_to_set[field_names["modified_by"]] = modified_by
        if document_modified and field_names.get("document_modified"):
            fields_to_set[field_names["document_modified"]] = document_modified
        if document_modified_by and field_names.get("document_modified_by"):
            fields_to_set[field_names["document_modified_by"]] = document_modified_by
        if document_created and field_names.get("document_created"):
            fields_to_set[field_names["document_created"]] = document_created
        if approver and field_names.get("approver"):
            fields_to_set[field_names["approver"]] = approver

        if url and link_field_name and not title_is_rich:
            fields_to_set[link_field_name] = {
                "Url": url,
                "Description": document_link_text or "Go to Page",
            }

        if summary is not None and field_names.get("summary"):
            fields_to_set[field_names["summary"]] = summary

        if ingestion_date is not None and field_names.get("ingestion_date"):
            fields_to_set[field_names["ingestion_date"]] = ingestion_date

        existing_item = None
        if doc_title_raw and field_names.get("document_title"):
            doc_title_norm = _normalize_tracker_doc_title(doc_title_raw)
            matches = []
            try:
                items = list(client.get_list_items(
                    list_id=tracker_list_id,
                    max_items=500,
                ))

                # Primary match: by source URL in the Document Link field (most stable identifier)
                if url and link_field_name:
                    url_norm = url.rstrip("/").lower()
                    for item in items:
                        fields = item.get("fields", {})
                        item_link = fields.get(link_field_name)
                        item_url = None
                        if isinstance(item_link, dict):
                            item_url = (item_link.get("Url") or "").rstrip("/").lower()
                        elif isinstance(item_link, str):
                            item_url = item_link.rstrip("/").lower()
                        if item_url and item_url == url_norm:
                            matches.append(item)

                # If Document Title is rich text (contains URL), also match by extracting href
                if not matches and url and title_is_rich:
                    url_norm = url.rstrip("/").lower()
                    title_field = field_names.get("document_title")
                    if title_field:
                        for item in items:
                            fields = item.get("fields", {})
                            raw_val = fields.get(title_field) or ""
                            href_match = re.search(r'href=["\']([^"\']+)["\']', raw_val)
                            if href_match:
                                item_url = href_match.group(1).rstrip("/").lower()
                                if item_url == url_norm:
                                    matches.append(item)

                # Fallback match: by normalized document title (no content_section filter)
                if not matches:
                    for item in items:
                        fields = item.get("fields", {})
                        item_title = _normalize_tracker_doc_title(fields.get(field_names["document_title"]))
                        if item_title and item_title == doc_title_norm:
                            matches.append(item)
            except Exception as e:
                logger.warning(f"Tracker lookup failed, will create new entry: {e}")
                matches = []

            if matches:
                def _item_sort_key(candidate: dict) -> str:
                    return candidate.get("createdDateTime") or ""
                matches = sorted(matches, key=_item_sort_key, reverse=True)
                existing_item = matches[0]
                if len(matches) > 1:
                    site_id = client.get_site_id()
                    for extra in matches[1:]:
                        extra_id = extra.get("id")
                        if not extra_id:
                            continue
                        try:
                            client._make_request("DELETE", f"/sites/{site_id}/lists/{tracker_list_id}/items/{extra_id}")
                            logger.info(f"Deleted duplicate tracker entry: {extra_id}")
                        except Exception as e:
                            logger.warning(f"Failed to delete duplicate tracker entry {extra_id}: {e}")

        if existing_item:
            current_fields = existing_item.get("fields", {})
            if increment_version and field_names.get("version"):
                fields_to_set[field_names["version"]] = str(_increment_version(
                    current_fields.get(field_names["version"])
                ))

            try:
                client.update_list_item_fields(
                    list_id=tracker_list_id,
                    item_id=existing_item.get("id"),
                    fields=fields_to_set,
                )
            except requests.exceptions.HTTPError as e:
                if link_field_name and link_field_name in fields_to_set and e.response is not None and e.response.status_code == 400:
                    logger.warning("Document Link update failed (400). Retrying without Document Link.")
                    fields_to_set = {key: value for key, value in fields_to_set.items() if key != link_field_name}
                    if fields_to_set:
                        client.update_list_item_fields(
                            list_id=tracker_list_id,
                            item_id=existing_item.get("id"),
                            fields=fields_to_set,
                        )
                else:
                    raise
            logger.info(f"Updated tracker entry: {doc_title_value}")
            return True

        if increment_version and field_names.get("version"):
            fields_to_set[field_names["version"]] = "1"

        try:
            client.add_list_item(
                list_id=tracker_list_id,
                fields=fields_to_set,
            )
        except requests.exceptions.HTTPError as e:
            if link_field_name and link_field_name in fields_to_set and e.response is not None and e.response.status_code == 400:
                logger.warning("Document Link update failed (400). Retrying without Document Link.")
                fields_to_set = {key: value for key, value in fields_to_set.items() if key != link_field_name}
                if fields_to_set:
                    client.add_list_item(
                        list_id=tracker_list_id,
                        fields=fields_to_set,
                    )
            else:
                raise
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

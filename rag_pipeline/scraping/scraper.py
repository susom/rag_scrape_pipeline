"""
Web scraper for RPP pipeline.
Extracts main content and attachment links from web pages.
"""

import os
import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from rag_pipeline.utils.logger import setup_logger
from rag_pipeline.scraping.pdf_parser import process_pdfs

logger = setup_logger()

# Selectors for main content area (ordered by specificity)
# These target the actual content, not nav/footer/chrome
MAIN_CONTENT_SELECTORS = [
    # Stanford Drupal theme specific
    ".su-wysiwyg-text",
    "#main-content",
    "main.page-content",
    "#page-content",
    # Generic fallbacks
    "article.content",
    "div.content-main",
    "main[role='main']",
    "article",
    "main",
    # Last resort - but we'll still strip nav/footer
    "body",
]

# File extensions considered as "attachments"
ATTACHMENT_EXTENSIONS = {".pdf", ".doc", ".docx"}


def clean_html(html: str) -> str:
    """
    Convert HTML to clean text, removing navigation cruft.
    Preserves table structure as plain text.
    """
    soup = BeautifulSoup(html, "lxml")

    # Remove cruft elements
    for tag in soup(["header", "footer", "nav", "aside", "script", "style", "noscript", "meta", "link"]):
        tag.decompose()

    # Convert tables to readable text format
    for table in soup.find_all("table"):
        rows = []
        for tr in table.find_all("tr"):
            cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
            rows.append(" | ".join(cells))
        table.replace_with(BeautifulSoup("\n".join(rows) + "\n", "lxml"))

    # Get text with reasonable spacing
    text = soup.get_text(separator="\n", strip=True)

    # Clean up excessive whitespace
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text


def save_text_locally(url: str, text: str) -> str:
    """Save cleaned text content to cache."""
    safe_name = url.replace("https://", "").replace("http://", "").replace("/", "_")[:80]
    os.makedirs("cache/raw", exist_ok=True)
    path = os.path.join("cache/raw", f"{safe_name}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    logger.info(f"Saved text: {path} ({len(text)} chars)")
    return path


def find_main_content_element(soup: BeautifulSoup) -> BeautifulSoup | None:
    """
    Find the main content element using our selector list.
    Returns the element or None if not found.
    """
    for selector in MAIN_CONTENT_SELECTORS:
        element = soup.select_one(selector)
        if element and element.get_text(strip=True):
            logger.info(f"Main content found with selector: '{selector}'")
            return element
    return None


def extract_attachment_links(content_element: BeautifulSoup, base_url: str) -> list[dict]:
    """
    Extract attachment links (PDF, DOC, DOCX) from within the main content element only.
    Does NOT extract links from nav, footer, or other chrome.

    Returns list of dicts: [{"url": "...", "type": "pdf", "text": "link text"}, ...]
    """
    attachments = []
    seen_urls = set()

    if content_element is None:
        return attachments

    for a in content_element.find_all("a", href=True):
        href = a["href"].strip()
        if not href:
            continue

        # Check if it's an attachment by extension
        href_lower = href.lower()
        ext = None
        for attachment_ext in ATTACHMENT_EXTENSIONS:
            if href_lower.endswith(attachment_ext):
                ext = attachment_ext.lstrip(".")
                break

        if ext is None:
            continue

        # Resolve relative URLs
        full_url = urljoin(base_url, href)

        # Deduplicate
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        link_text = a.get_text(strip=True) or ""
        attachments.append({
            "url": full_url,
            "type": ext,
            "text": link_text,
        })

    logger.info(f"Found {len(attachments)} attachment(s) in main content")
    return attachments


def scrape_page(url: str, session: requests.Session) -> dict:
    """
    Scrape a single URL and extract main content + attachment links.

    Returns dict:
    {
        "url": str,
        "text": str | None,        # cleaned text content
        "cached_path": str | None, # path to cached text file
        "attachments": [{"url", "type", "text"}, ...],
        "error": str | None
    }
    """
    result = {
        "url": url,
        "text": None,
        "cached_path": None,
        "attachments": [],
        "error": None,
    }

    try:
        resp = session.get(url, timeout=15)
        resp.raise_for_status()

        # Check if this is a PDF (by URL extension or Content-Type header)
        content_type = resp.headers.get("Content-Type", "").lower()
        is_pdf = url.lower().endswith(".pdf") or "application/pdf" in content_type

        if is_pdf:
            # Handle PDF separately using PDF parser
            logger.info(f"Detected PDF at {url}, using PDF parser")
            pdf_text = process_pdfs(url)

            if pdf_text:
                result["text"] = pdf_text
                result["cached_path"] = save_text_locally(url, pdf_text)
                logger.info(f"Parsed PDF {url}: {len(pdf_text)} chars")
            else:
                logger.warning(f"PDF parser returned empty text for {url}")
                result["error"] = "PDF parsing returned no text"

            # PDFs don't have attachments to extract
            return result

        # Handle HTML pages
        soup = BeautifulSoup(resp.text, "lxml")

        # Find main content element
        main_element = find_main_content_element(soup)

        if main_element:
            # Extract attachment links from main content ONLY
            result["attachments"] = extract_attachment_links(main_element, url)

            # Clean the main content to text
            html_content = str(main_element)
            clean_text = clean_html(html_content)
        else:
            # Fallback: use whole page but still clean it
            logger.warning(f"No main content selector matched for {url}, using full page")
            clean_text = clean_html(resp.text)
            # Don't extract attachments from full page - too risky

        if clean_text:
            result["text"] = clean_text
            result["cached_path"] = save_text_locally(url, clean_text)

        logger.info(f"Scraped {url}: {len(clean_text)} chars, {len(result['attachments'])} attachments")

    except Exception as e:
        logger.error(f"Error scraping {url}: {e}")
        result["error"] = str(e)

    return result


def scrape_url(url: str, follow_attachments: bool = True) -> dict:
    """
    Scrape a URL and optionally discover attachments in main content.

    Args:
        url: The URL to scrape
        follow_attachments: If True, return attachment URLs found in main content

    Returns:
        {
            "url": str,
            "text": str | None,
            "cached_path": str | None,
            "attachments": [{"url", "type", "text"}, ...],  # empty if follow_attachments=False
            "error": str | None
        }
    """
    logger.info(f"Starting scrape for: {url} (follow_attachments={follow_attachments})")
    session = requests.Session()

    result = scrape_page(url, session)

    if not follow_attachments:
        result["attachments"] = []

    return result


# Legacy function for backwards compatibility
def scrape_urls(url: str, follow_links: bool = True) -> tuple[str, list[str]]:
    """
    Legacy wrapper for backwards compatibility.

    Returns:
        tuple of (text_content: str, pdf_urls: list[str])
    """
    result = scrape_url(url, follow_attachments=follow_links)

    text = result["text"] or ""
    pdf_urls = [a["url"] for a in result["attachments"] if a["type"] == "pdf"]

    return text, pdf_urls

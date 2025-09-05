import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from rag_pipeline.utils.logger import setup_logger

logger = setup_logger()

# Add your preferred selectors to try in order
CONTENT_SELECTORS = [
    "main#page-content",
    "div.main-region",
    "div#main-content",
    "article",
    "section.content",
]

def scrape_urls(url: str):
    logger.info(f"Starting scrape for: {url}")
    try:
        resp = requests.get(url)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        html_content = None
        for selector in CONTENT_SELECTORS:
            element = soup.select_one(selector)
            if element and element.get_text(strip=True):
                logger.info(f"Content found with selector '{selector}' for: {url}")
                html_content = str(element)
                break

        if not html_content:
            logger.warning(f"No content found in selectors for {url}, saving full HTML snapshot")
            html_content = resp.text

        pdf_links = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.lower().endswith(".pdf"):
                pdf_links.append(urljoin(url, href))
        logger.info(f"Detected {len(pdf_links)} PDF links on {url}")

        return html_content, pdf_links
    except Exception as e:
        logger.error(f"Error scraping {url}: {e}")
        return None, []

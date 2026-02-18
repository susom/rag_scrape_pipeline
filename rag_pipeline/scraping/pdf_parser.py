import requests
import pdfplumber
import io
from rag_pipeline.utils.logger import setup_logger

logger = setup_logger()

def process_pdfs(pdf_url: str) -> str:
    logger.info(f"Starting PDF processing: {pdf_url}")
    try:
        resp = requests.get(pdf_url, timeout=30)
        resp.raise_for_status()
        with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
            texts = []
            for i, page in enumerate(pdf.pages):
                text = page.extract_text() or ""
                texts.append(text)
                logger.info(f"Extracted page {i+1}/{len(pdf.pages)} from PDF")
        return "\n".join(texts)
    except Exception as e:
        logger.error(f"Error processing PDF {pdf_url}: {e}")
        return ""

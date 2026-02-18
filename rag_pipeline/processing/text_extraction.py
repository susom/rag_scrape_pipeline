"""
Shared text extraction utilities.

Extracts text from uploaded files (PDF, DOCX, TXT) into plain text.
Used by both web.py (upload endpoint) and automation/orchestrator.py (SharePoint files).
"""

import io
import os
import tempfile

import pdfplumber

try:
    import docx2txt
except ImportError:
    docx2txt = None

from rag_pipeline.utils.logger import setup_logger

logger = setup_logger()


def extract_text_from_file(filename: str, file_bytes: bytes) -> str:
    """
    Extract plain text from a file based on its extension.

    Args:
        filename: Original filename (used to determine type via extension).
        file_bytes: Raw file content as bytes.

    Returns:
        Extracted plain text.

    Raises:
        ValueError: If the file type is unsupported or extraction fails.
    """
    filename_lower = filename.lower()

    if filename_lower.endswith(".txt"):
        return file_bytes.decode("utf-8", errors="ignore")

    elif filename_lower.endswith(".docx"):
        if docx2txt is None:
            raise ValueError("docx2txt is not installed — cannot process DOCX files")
        # docx2txt.process() needs a file path, so write to a temp file
        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name
        try:
            text = docx2txt.process(tmp_path)
        finally:
            os.unlink(tmp_path)
        return text or ""

    elif filename_lower.endswith(".pdf"):
        try:
            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                pages = [page.extract_text() or "" for page in pdf.pages]
                return "\n\n".join(pages)
        except Exception as e:
            raise ValueError(f"PDF parsing failed: {e}") from e

    else:
        raise ValueError(f"Unsupported file type: {filename}")


def get_thinker_name(filename: str) -> str:
    """
    Map a filename to the appropriate thinker_name for source-aware prompts.

    Args:
        filename: Original filename.

    Returns:
        Thinker name string: "DOCX", "PDF", or "default".
    """
    filename_lower = filename.lower()
    if filename_lower.endswith(".docx"):
        return "DOCX"
    elif filename_lower.endswith(".pdf"):
        return "PDF"
    else:
        return "default"

"""
RPP Web API - RAG Preparation Pipeline
Primary interface for the RAG preparation tool.
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Body, UploadFile, File, Form, Depends, Query
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.openapi.utils import get_openapi
from typing import List
import json
import os
import re
import time
import pdfplumber
import io
import hashlib

try:
    import docx2txt
except ImportError:
    docx2txt = None

from rag_pipeline.main import run_pipeline
from rag_pipeline.output_json import generate_run_id, write_canonical_json, RPP_VERSION
from rag_pipeline.utils.logger import setup_logger
from rag_pipeline.processing.sliding_window import SlidingWindowParser
from rag_pipeline.processing.ai_client import AVAILABLE_MODELS, DEFAULT_MODEL
from rag_pipeline.scraping.scraper import scrape_url
from rag_pipeline.database import init_db, check_connection, get_db
from sqlalchemy.orm import Session
from datetime import datetime, timezone

logger = setup_logger()


# OpenAPI Tags for documentation
tags_metadata = [
    {
        "name": "Health",
        "description": "Health check and status endpoints",
    },
    {
        "name": "Database",
        "description": "Database connection and status endpoints",
    },
    {
        "name": "SharePoint - Sites",
        "description": "SharePoint site configuration and status",
    },
    {
        "name": "SharePoint - Pages",
        "description": "Operations on SharePoint site pages",
    },
    {
        "name": "SharePoint - Lists",
        "description": "Operations on SharePoint lists and list items",
    },
    {
        "name": "SharePoint - List Drive",
        "description": "Operations on document libraries associated with lists",
    },
    {
        "name": "SharePoint - Drive",
        "description": "Operations on the default site document library (drive)",
    },
    {
        "name": "SharePoint - Search",
        "description": "Search operations across SharePoint content",
    },
    {
        "name": "Pipeline",
        "description": "RAG document processing pipeline operations",
    },
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler for startup/shutdown events."""
    # Startup
    logger.info("Starting RAG Preparation Pipeline...")

    # Initialize database
    db_status = check_connection()
    if db_status["connected"]:
        logger.info(f"Database connected: {db_status['database']} (user: {db_status['user']})")
        init_db()  # Create tables if they don't exist
    else:
        logger.warning(f"Database not connected: {db_status['error']}")

    yield

    # Shutdown
    logger.info("Shutting down RAG Preparation Pipeline...")


app = FastAPI(
    title="RPP - RAG Preparation Pipeline",
    description="""
## RAG Preparation Pipeline API

This API provides endpoints for:
- **SharePoint Integration**: Access SharePoint sites, pages, lists, and document libraries via Microsoft Graph API
- **Document Processing**: Process and prepare documents for RAG (Retrieval-Augmented Generation)
- **Database Management**: Track document ingestion state

### SharePoint Configuration
SharePoint sites are configured via environment variables:
- `SHAREPOINT_SITE_HOSTNAME`: SharePoint hostname (e.g., `contoso.sharepoint.com`)
- `SHAREPOINT_SITE_PATH`: Site path (e.g., `/sites/MySite`)

Multiple sites can be configured using the naming pattern:
- `SHAREPOINT_SITE_{NAME}_HOSTNAME`
- `SHAREPOINT_SITE_{NAME}_PATH`

### Authentication
SharePoint credentials are stored in Google Cloud Secret Manager:
- `SHAREPOINT_CLIENT_ID`
- `SHAREPOINT_CLIENT_SECRET`
- `SHAREPOINT_TENANT_ID`
    """,
    version=RPP_VERSION,
    openapi_tags=tags_metadata,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# Link following configuration
MAX_FOLLOWED_URLS_PER_DOC = 20  # Maximum URLs to follow per uploaded document
URL_FOLLOW_DELAY_SECONDS = 2     # Delay between processing each followed URL (rate limiting)
MIN_CONTENT_LENGTH_SCRAPED = 100  # Minimum chars after scraping (pre-AI check)
MIN_CONTENT_LENGTH_AI = 200       # Minimum chars after AI extraction (post-AI check)

# Bot detection keywords (case-insensitive)
BOT_DETECTION_KEYWORDS = [
    "captcha",
    "bot test",
    "request access",
    "are you a robot",
    "verify you are human",
    "automated scraping",
    "recaptcha",
    "cloudflare",
    "access denied",
    "403 forbidden",
    "enable javascript to continue",
    "please enable cookies",
]

# Default prompts
DEFAULT_SYSTEM_PROMPT = """You are a content extraction assistant. Your job is to extract the main, relevant content from the provided text while removing any navigation, boilerplate, or irrelevant elements. Output ONLY the extracted content - no explanations, no commentary, no JSON wrapping. Preserve important information like dates, names, numbers, and structured data (tables). If the content is already clean, return it as-is without modification."""

DEFAULT_USER_TEMPLATE = """Extract the main content from this text. Remove any website navigation, headers, footers, or boilerplate. Keep all substantive information including tables, lists, dates, and names.

--- BEGIN TEXT ---
{window_text}
--- END TEXT ---"""


def load_prompts():
    """Load prompts from config file or return defaults."""
    config_path = "config/sliding_window_prompts.json"
    system_prompt = DEFAULT_SYSTEM_PROMPT
    user_template = DEFAULT_USER_TEMPLATE

    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            loaded_system = cfg.get("system", "").strip()
            loaded_user = cfg.get("user_template", "").strip()

            # Only use if not corrupted
            if loaded_system and "Ã" not in loaded_system:
                system_prompt = loaded_system
            if loaded_user and "Ã" not in loaded_user:
                user_template = loaded_user
        except Exception:
            pass

    return system_prompt, user_template


def save_prompts(system: str, user_template: str):
    """Save prompts to config file."""
    os.makedirs("config", exist_ok=True)
    with open("config/sliding_window_prompts.json", "w", encoding="utf-8") as f:
        json.dump({"system": system, "user_template": user_template}, f, ensure_ascii=False, indent=2)


def is_bot_detection_page(text: str) -> bool:
    """
    Check if the scraped text appears to be a bot detection/CAPTCHA page.

    Args:
        text: The scraped text content

    Returns:
        True if text matches bot detection patterns, False otherwise
    """
    if not text:
        return False

    text_lower = text.lower()

    # Check for bot detection keywords
    for keyword in BOT_DETECTION_KEYWORDS:
        if keyword in text_lower:
            # Additional heuristic: if keyword appears and content is short, likely a bot page
            if len(text.strip()) < 2000:
                return True

    return False


def extract_urls_from_text(text: str) -> list[str]:
    """
    Extract unique http/https URLs from text.

    Returns:
        List of unique URLs found in the text.
    """
    # Regex pattern for http/https URLs
    # Matches URLs with proper structure, avoiding common false positives
    url_pattern = r'https?://[^\s<>"{}|\\^`\[\]]+'

    urls = re.findall(url_pattern, text)

    # Remove trailing punctuation that's likely not part of the URL
    cleaned_urls = []
    for url in urls:
        url = url.rstrip('.,;:!?)')
        cleaned_urls.append(url)

    # Deduplicate while preserving order
    seen = set()
    unique_urls = []
    for url in cleaned_urls:
        if url not in seen:
            seen.add(url)
            unique_urls.append(url)

    return unique_urls


@app.get("/", response_class=HTMLResponse)
def home():
    system_prompt, user_template = load_prompts()

    # Generate model options for dropdown
    model_options = "\n".join([
        f'                <option value="{m}"{"selected" if m == DEFAULT_MODEL else ""}>{m}</option>'
        for m in AVAILABLE_MODELS
    ])

    return f"""
<!DOCTYPE html>
<html>
<head>
    <title>RPP - RAG Preparation Pipeline</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }}
        .container {{ max-width: 900px; margin: 0 auto; }}
        h1 {{ color: #333; margin-bottom: 5px; }}
        .subtitle {{ color: #666; margin-bottom: 30px; }}
        .card {{ background: white; border-radius: 8px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        .card h3 {{ margin-top: 0; color: #444; }}
        textarea {{ width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 4px; font-family: monospace; font-size: 13px; }}
        button {{ background: #0066cc; color: white; border: none; padding: 12px 24px; border-radius: 4px; cursor: pointer; font-size: 14px; }}
        button:hover {{ background: #0052a3; }}
        button:disabled {{ background: #ccc; cursor: not-allowed; }}
        label {{ display: block; margin-bottom: 8px; font-weight: 500; }}
        select {{ padding: 8px 12px; border: 1px solid #ddd; border-radius: 4px; font-size: 14px; min-width: 150px; }}
        .checkbox-label {{ display: inline; font-weight: normal; }}
        input[type="file"] {{ margin: 10px 0; }}
        .result {{ margin-top: 15px; padding: 15px; background: #e8f5e9; border-radius: 4px; display: none; }}
        .result.error {{ background: #ffebee; }}
        .result a {{ color: #0066cc; }}
        details {{ margin-top: 15px; }}
        summary {{ cursor: pointer; color: #666; }}
        .stats {{ font-family: monospace; font-size: 12px; color: #666; }}
        hr {{ border: none; border-top: 1px solid #eee; margin: 20px 0; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>RPP</h1>
        <p class="subtitle">RAG Preparation Pipeline v{RPP_VERSION}</p>

        <details>
            <summary>Customize AI Extraction Prompts (Advanced)</summary>
            <div class="card" style="margin-top: 10px;">
                <label>System Prompt:</label>
                <textarea id="system" rows="4">{system_prompt}</textarea>

                <label style="margin-top: 15px;">User Prompt Template:</label>
                <textarea id="user_template" rows="6">{user_template}</textarea>
                <p style="font-size: 12px; color: #666;">Use {{window_text}} as placeholder for the content chunk.</p>
            </div>
        </details>

        <div class="card">
            <h3>Process URLs</h3>
            <label>Enter URLs (one per line or comma-separated):</label>
            <textarea id="urls" rows="4" placeholder="https://example.com/page1&#10;https://example.com/page2"></textarea>

            <div style="margin: 15px 0; display: flex; gap: 20px; align-items: center;">
                <div>
                    <label for="model" style="margin-bottom: 4px;">AI Model:</label>
                    <select id="model">
{model_options}
                    </select>
                </div>
                <div style="display: flex; align-items: center; gap: 8px;">
                    <input type="checkbox" id="follow_links" checked>
                    <label class="checkbox-label" for="follow_links">Follow attachments (PDF/DOCX) in main content</label>
                </div>
            </div>

            <button id="runBtn" onclick="runPipeline()">Run Pipeline</button>
            <div id="urlResult" class="result"></div>
        </div>

        <div class="card">
            <h3>Upload Documents</h3>
            <p style="color: #666; margin-top: 0;">Supports PDF, DOCX, or TXT files (select multiple files for batch processing)</p>
            <input type="file" id="fileInput" accept=".pdf,.docx,.txt" multiple>

            <div style="margin: 15px 0; display: flex; align-items: center; gap: 8px;">
                <input type="checkbox" id="follow_doc_links">
                <label class="checkbox-label" for="follow_doc_links">Follow web links found in documents (1 level deep)</label>
            </div>

            <p style="color: #f57c00; font-size: 13px; margin: 10px 0; display: none;" id="linkFollowWarning">
                ⚠️ Link following can take significant time (2s delay per URL, max 20 URLs/file).
                Large batches with link following may take 30+ minutes. Consider processing in smaller batches.
            </p>

            <button onclick="uploadFile()">Upload & Process</button>
            <div id="uploadResult" class="result"></div>
        </div>
    </div>

    <script>
        // Show/hide link following warning based on checkbox and file count
        function updateLinkFollowWarning() {{
            const followCheckbox = document.getElementById('follow_doc_links');
            const fileInput = document.getElementById('fileInput');
            const warning = document.getElementById('linkFollowWarning');

            if (followCheckbox.checked && fileInput.files.length > 3) {{
                warning.style.display = 'block';
            }} else {{
                warning.style.display = 'none';
            }}
        }}

        // Attach event listeners
        document.addEventListener('DOMContentLoaded', function() {{
            document.getElementById('follow_doc_links').addEventListener('change', updateLinkFollowWarning);
            document.getElementById('fileInput').addEventListener('change', updateLinkFollowWarning);
        }});

        async function runPipeline() {{
            const btn = document.getElementById('runBtn');
            const resultDiv = document.getElementById('urlResult');
            btn.disabled = true;
            btn.textContent = 'Processing...';
            resultDiv.style.display = 'none';

            const urlText = document.getElementById('urls').value;
            const urls = urlText.split(/[,\\n]/).map(u => u.trim()).filter(u => u.length > 0);

            if (urls.length === 0) {{
                alert('Please enter at least one URL');
                btn.disabled = false;
                btn.textContent = 'Run Pipeline';
                return;
            }}

            try {{
                const res = await fetch('/run', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{
                        urls: urls,
                        model: document.getElementById('model').value,
                        system: document.getElementById('system').value,
                        user_template: document.getElementById('user_template').value,
                        follow_links: document.getElementById('follow_links').checked
                    }})
                }});

                const data = await res.json();

                if (res.ok) {{
                    resultDiv.className = 'result';
                    resultDiv.innerHTML = `
                        <strong>Success!</strong><br>
                        <a href="/download/${{data.run_id}}" target="_blank">Download JSON</a><br>
                        <div class="stats">
                            Run ID: ${{data.run_id}}<br>
                            Model: ${{data.model || 'gpt-4.1'}}<br>
                            Documents: ${{data.stats.documents_processed}}<br>
                            Sections: ${{data.stats.total_sections}}<br>
                            Time: ${{data.stats.processing_time_seconds}}s
                        </div>
                    `;
                }} else {{
                    resultDiv.className = 'result error';
                    resultDiv.innerHTML = `<strong>Error:</strong> ${{data.detail || 'Unknown error'}}`;
                }}
                resultDiv.style.display = 'block';
            }} catch (e) {{
                resultDiv.className = 'result error';
                resultDiv.innerHTML = `<strong>Error:</strong> ${{e.message}}`;
                resultDiv.style.display = 'block';
            }}

            btn.disabled = false;
            btn.textContent = 'Run Pipeline';
        }}

        async function uploadFile() {{
            const fileInput = document.getElementById('fileInput');
            const resultDiv = document.getElementById('uploadResult');

            if (!fileInput.files.length) {{
                alert('Please select at least one file');
                return;
            }}

            const fileCount = fileInput.files.length;
            const followLinks = document.getElementById('follow_doc_links').checked;

            resultDiv.style.display = 'none';

            // Show detailed message based on options
            let processingMsg = `Processing ${{fileCount}} file(s)...`;
            if (followLinks) {{
                processingMsg += `<br><small style="color: #666;">Extracting and following web links from documents (this may take longer)...</small>`;
            }}

            resultDiv.innerHTML = processingMsg;
            resultDiv.className = 'result';
            resultDiv.style.display = 'block';

            const formData = new FormData();

            // Append all selected files
            for (let i = 0; i < fileInput.files.length; i++) {{
                formData.append('files', fileInput.files[i]);
            }}

            formData.append('model', document.getElementById('model').value);
            formData.append('system', document.getElementById('system').value);
            formData.append('user_template', document.getElementById('user_template').value);
            formData.append('follow_doc_links', document.getElementById('follow_doc_links').checked);

            try {{
                const res = await fetch('/upload', {{ method: 'POST', body: formData }});
                const data = await res.json();

                if (res.ok) {{
                    const uploadedFiles = fileCount;
                    const totalDocs = data.stats.documents_processed;
                    const followedDocs = totalDocs - uploadedFiles;

                    let statsHtml = `
                        <strong>Success!</strong><br>
                        <a href="/download/${{data.run_id}}" target="_blank">Download JSON</a><br>
                        <div class="stats">
                            Run ID: ${{data.run_id}}<br>
                            Model: ${{data.model || 'gpt-4.1'}}<br>
                            Documents: ${{totalDocs}}`;

                    if (followLinks && followedDocs > 0) {{
                        statsHtml += ` (${{uploadedFiles}} uploaded + ${{followedDocs}} followed URLs)`;
                    }}

                    statsHtml += `<br>
                            Sections: ${{data.stats.total_sections}}<br>
                            Time: ${{data.stats.processing_time_seconds}}s
                        </div>
                    `;

                    resultDiv.className = 'result';
                    resultDiv.innerHTML = statsHtml;
                }} else {{
                    resultDiv.className = 'result error';
                    resultDiv.innerHTML = `<strong>Error:</strong> ${{data.detail || 'Unknown error'}}`;
                }}
            }} catch (e) {{
                resultDiv.className = 'result error';
                resultDiv.innerHTML = `<strong>Error:</strong> ${{e.message}}`;
            }}
        }}
    </script>
</body>
</html>
"""


@app.post("/run")
def run_scrape(payload: dict = Body(...)):
    """Process URLs through the pipeline."""
    urls = payload.get("urls", [])
    model = payload.get("model", DEFAULT_MODEL)
    system = payload.get("system", "")
    user_template = payload.get("user_template", "")
    follow_links = str(payload.get("follow_links", "true")).lower() == "true"
    tags = payload.get("tags", [])

    if not urls:
        raise HTTPException(status_code=400, detail="No URLs provided")

    # Validate model
    if model not in AVAILABLE_MODELS:
        model = DEFAULT_MODEL

    # Save custom prompts if provided
    if system or user_template:
        save_prompts(system or DEFAULT_SYSTEM_PROMPT, user_template or DEFAULT_USER_TEMPLATE)

    run_id = generate_run_id(urls)
    logger.info(f"Starting pipeline run {run_id} for {len(urls)} URLs with model={model}")

    try:
        result = run_pipeline(
            urls=urls,
            run_id=run_id,
            follow_links=follow_links,
            run_mode="ai_always",
            triggered_by="web_api",
            tags=tags if tags else None,
            model=model,
        )
        logger.info(f"Pipeline completed: {result['output_path']}")

        return {
            "status": "completed",
            "run_id": result["run_id"],
            "output_path": result["output_path"],
            "stats": result["stats"],
            "warnings": result["warnings"],
            "model": model,
        }
    except Exception as e:
        logger.error(f"Pipeline failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/upload")
def upload_file(
    files: List[UploadFile] = File(...),
    model: str = Form(DEFAULT_MODEL),
    system: str = Form(""),
    user_template: str = Form(""),
    follow_doc_links: str = Form("false")
):
    """Upload and process multiple documents (PDF, DOCX, or TXT)."""
    os.makedirs("cache/raw", exist_ok=True)

    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")

    # Validate model
    if model not in AVAILABLE_MODELS:
        model = DEFAULT_MODEL

    # Save custom prompts if provided
    if system or user_template:
        save_prompts(system or DEFAULT_SYSTEM_PROMPT, user_template or DEFAULT_USER_TEMPLATE)

    # Convert follow_doc_links to boolean
    follow_doc_links_bool = str(follow_doc_links).lower() == "true"
    logger.info(f"Link following for uploaded documents: {follow_doc_links_bool}")

    # Generate run_id for all files
    filenames = [f.filename for f in files]
    run_id = generate_run_id(filenames)
    start_time = datetime.now(timezone.utc)
    logger.info(f"Starting batch upload for {len(files)} file(s) with run_id={run_id}")

    parser = SlidingWindowParser(model=model)
    documents = []

    # Process each file
    for file in files:
        # Save uploaded file
        file_content = file.file.read()
        file_path = os.path.join("cache/raw", file.filename)
        with open(file_path, "wb") as f:
            f.write(file_content)
        logger.info(f"Uploaded file: {file_path}")

        # Extract text based on file type
        filename_lower = file.filename.lower()
        text = ""

        try:
            if filename_lower.endswith(".txt"):
                text = file_content.decode("utf-8", errors="ignore")

            elif filename_lower.endswith(".docx"):
                if docx2txt is None:
                    raise HTTPException(status_code=500, detail="docx2txt not installed")
                text = docx2txt.process(file_path)

            elif filename_lower.endswith(".pdf"):
                try:
                    with pdfplumber.open(io.BytesIO(file_content)) as pdf:
                        pages = [page.extract_text() or "" for page in pdf.pages]
                        text = "\n\n".join(pages)
                except Exception as e:
                    logger.error(f"PDF parsing failed for {file.filename}: {e}")
                    documents.append({
                        "uri": f"file://{file.filename}",
                        "source_type": "pdf",
                        "cached_files": {},
                        "followed_from": None,
                        "sections": [],
                        "errors": [f"PDF parsing failed: {e}"],
                    })
                    continue

            else:
                logger.warning(f"Unsupported file type: {file.filename}")
                documents.append({
                    "uri": f"file://{file.filename}",
                    "source_type": "unknown",
                    "cached_files": {},
                    "followed_from": None,
                    "sections": [],
                    "errors": ["Unsupported file type. Use PDF, DOCX, or TXT."],
                })
                continue

            if not text.strip():
                logger.warning(f"No text extracted from {file.filename}")
                documents.append({
                    "uri": f"file://{file.filename}",
                    "source_type": filename_lower.split(".")[-1],
                    "cached_files": {},
                    "followed_from": None,
                    "sections": [],
                    "errors": ["Could not extract any text from file"],
                })
                continue

            # Save extracted text
            txt_path = file_path.rsplit(".", 1)[0] + ".txt"
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(text)

            # Determine thinker_name based on file type for source-aware prompts
            if filename_lower.endswith(".docx"):
                thinker_name = "DOCX"
            elif filename_lower.endswith(".pdf"):
                thinker_name = "PDF"
            else:
                thinker_name = "default"

            # Process through sliding window + AI
            try:
                count, sections = parser.process_file(txt_path, "", thinker_name=thinker_name)
                logger.info(f"Processed {file.filename}: {len(sections)} sections")
            except Exception as e:
                logger.error(f"Processing failed for {file.filename}: {e}")
                documents.append({
                    "uri": f"file://{file.filename}",
                    "source_type": filename_lower.split(".")[-1],
                    "cached_files": {"raw_text": txt_path},
                    "followed_from": None,
                    "sections": [],
                    "errors": [f"Processing failed: {e}"],
                })
                continue

            # Add document to collection
            file_uri = f"file://{file.filename}"
            documents.append({
                "uri": file_uri,
                "source_type": filename_lower.split(".")[-1],
                "cached_files": {"raw_text": txt_path},
                "followed_from": None,
                "sections": sections,
                "errors": [],
            })

            # Follow URLs found in document if enabled
            if follow_doc_links_bool and text:
                extracted_urls = extract_urls_from_text(text)
                logger.info(f"Found {len(extracted_urls)} URL(s) in {file.filename}")

                # Apply rate limiting: cap max URLs to follow
                if len(extracted_urls) > MAX_FOLLOWED_URLS_PER_DOC:
                    logger.warning(
                        f"Document has {len(extracted_urls)} URLs. Limiting to first {MAX_FOLLOWED_URLS_PER_DOC} "
                        f"to prevent excessive API usage."
                    )
                    extracted_urls = extracted_urls[:MAX_FOLLOWED_URLS_PER_DOC]

                for idx, url in enumerate(extracted_urls):
                    try:
                        logger.info(f"Following URL from {file.filename}: {url}")

                        # Scrape the URL (no attachment following - only 1 level deep)
                        scrape_result = scrape_url(url, follow_attachments=False)

                        # Skip if scraping failed
                        if scrape_result.get("error"):
                            logger.warning(f"Skipping {url}: {scrape_result['error']}")
                            continue

                        # Get the raw, cleaned text from the scraper
                        url_text = scrape_result.get("text", "")

                        # Skip if no content extracted (pre-AI check)
                        if not url_text or len(url_text.strip()) < MIN_CONTENT_LENGTH_SCRAPED:
                            logger.warning(
                                f"Skipping {url}: No meaningful content extracted after scraping "
                                f"({len(url_text.strip())} chars < {MIN_CONTENT_LENGTH_SCRAPED})"
                            )
                            continue

                        # Skip if bot detection page (CAPTCHA, access denied, etc.)
                        if is_bot_detection_page(url_text):
                            logger.warning(
                                f"Skipping {url}: Detected bot detection/CAPTCHA page "
                                f"(keywords: {', '.join([k for k in BOT_DETECTION_KEYWORDS if k in url_text.lower()][:3])})"
                            )
                            continue

                        # Save the scraped text
                        url_safe_name = url.replace("https://", "").replace("http://", "").replace("/", "_")[:80]
                        url_txt_path = os.path.join("cache/raw", f"{url_safe_name}_followed.txt")
                        with open(url_txt_path, "w", encoding="utf-8") as f:
                            f.write(url_text)

                        # Process through AI with WebPage prompts (aggressive filtering)
                        # Followed web links are web pages - treat them like Path 1 (web URL scrape)
                        try:
                            count, url_sections = parser.process_file(url_txt_path, "", thinker_name="WebPage")

                            # Skip if AI processing resulted in no sections or minimal content (post-AI check)
                            # NOTE: Use "text" field, not "content" (original bug)
                            total_content = sum(len(s.get("text", "")) for s in url_sections)
                            if not url_sections or total_content < MIN_CONTENT_LENGTH_AI:
                                logger.warning(
                                    f"Skipping {url}: AI processing produced insufficient content "
                                    f"({total_content} chars < {MIN_CONTENT_LENGTH_AI}, likely bot page/error/junk)"
                                )
                                continue

                            logger.info(f"Successfully processed followed URL {url}: {len(url_sections)} sections")

                            # Add followed URL as a document
                            documents.append({
                                "uri": url,
                                "source_type": "webpage",
                                "cached_files": {"raw_text": url_txt_path},
                                "followed_from": file_uri,
                                "sections": url_sections,
                                "errors": [],
                            })

                        except Exception as e:
                            logger.warning(f"AI processing failed for {url}: {e}, skipping")
                            continue

                    except Exception as e:
                        logger.warning(f"Failed to process {url}: {e}, skipping")
                        continue

                    # Rate limiting: sleep between URL requests (except after last one)
                    if idx < len(extracted_urls) - 1:
                        logger.debug(f"Rate limiting: sleeping {URL_FOLLOW_DELAY_SECONDS}s before next URL")
                        time.sleep(URL_FOLLOW_DELAY_SECONDS)

        except Exception as e:
            logger.error(f"Unexpected error processing {file.filename}: {e}")
            documents.append({
                "uri": f"file://{file.filename}",
                "source_type": filename_lower.split(".")[-1] if "." in filename_lower else "unknown",
                "cached_files": {},
                "followed_from": None,
                "sections": [],
                "errors": [f"Unexpected error: {e}"],
            })

    # Write single canonical JSON with all documents
    result = write_canonical_json(
        run_id=run_id,
        run_mode="ai_always",
        follow_links=False,
        triggered_by="web_api",
        documents=documents,
        warnings=[],
        start_time=start_time,
        model_hint=model,
    )

    logger.info(f"Batch upload complete: {result['output_path']}")

    return {
        "status": "completed",
        "run_id": result["run_id"],
        "output_path": result["output_path"],
        "stats": result["stats"],
        "warnings": result["warnings"],
        "model": model,
    }


@app.get("/download/{run_id}", tags=["Pipeline"])
def download_output(run_id: str):
    """
    Download the canonical JSON output for a run.

    Returns the processed RAG-ready JSON file for a given run ID.
    """
    file_path = os.path.join("cache", "rag_ready", f"{run_id}.json")

    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail=f"Output not found for run_id: {run_id}")

    return FileResponse(
        file_path,
        media_type="application/json",
        filename=f"{run_id}.json"
    )


@app.get("/health", tags=["Health"])
def health_check():
    """
    Health check endpoint.

    Returns the current health status of the API, including database connection status.
    """
    db_status = check_connection()
    return {
        "health": "ok",
        "version": RPP_VERSION,
        "database": {
            "connected": db_status["connected"],
            "database": db_status.get("database"),
            "error": db_status.get("error")
        }
    }


@app.get("/db/status", tags=["Database"])
def database_status():
    """
    Get detailed database connection status.

    Returns connection information including:
    - Database name
    - User
    - MySQL version
    - List of tables
    """
    db_status = check_connection()
    return db_status


# ==================== SharePoint API Endpoints ====================

from rag_pipeline.sharepoint import SharePointGraphClient, get_site_config_manager, get_site_config
from typing import Optional

# Cache for SharePoint clients (keyed by site name)
_sharepoint_clients: dict[str, SharePointGraphClient] = {}


def get_sharepoint_client(site: Optional[str] = None) -> SharePointGraphClient:
    """
    Get or create SharePoint client instance for a specific site.

    Args:
        site: Site name (None for default site)

    Returns:
        SharePointGraphClient configured for the specified site
    """
    global _sharepoint_clients

    site_name = (site or "default").lower().strip()

    # Return cached client if available
    if site_name in _sharepoint_clients:
        return _sharepoint_clients[site_name]

    # Get site configuration
    try:
        site_config = get_site_config(site_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Create new client
    try:
        client = SharePointGraphClient(
            site_hostname=site_config.hostname,
            site_path=site_config.path,
        )
        _sharepoint_clients[site_name] = client
        return client
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to initialize SharePoint client: {e}")


@app.get("/sharepoint/sites", tags=["SharePoint - Sites"])
def list_configured_sites():
    """
    List all configured SharePoint sites.

    Returns all sites configured via environment variables.
    Sites can be configured using:
    - Default: `SHAREPOINT_SITE_HOSTNAME`, `SHAREPOINT_SITE_PATH`
    - Named: `SHAREPOINT_SITE_{NAME}_HOSTNAME`, `SHAREPOINT_SITE_{NAME}_PATH`
    """
    manager = get_site_config_manager()
    sites = manager.list_sites()
    return {
        "count": len(sites),
        "sites": [
            {
                "name": s.name,
                "hostname": s.hostname,
                "path": s.path,
                "url": s.full_url,
            }
            for s in sites
        ]
    }


@app.get("/sharepoint/status", tags=["SharePoint - Sites"])
def sharepoint_status(
    site: Optional[str] = Query(None, description="Site name (uses 'default' if not specified)")
):
    """
    Check SharePoint connection status.

    Tests the connection to a SharePoint site and returns site information.
    """
    try:
        client = get_sharepoint_client(site)
        site_info = client.get_site_info()
        site_config = get_site_config(site)
        return {
            "connected": True,
            "site_name": site_config.name,
            "site_id": site_info.get("id"),
            "display_name": site_info.get("displayName"),
            "site_url": site_info.get("webUrl"),
            "error": None
        }
    except HTTPException:
        raise
    except Exception as e:
        return {
            "connected": False,
            "site_name": site or "default",
            "site_id": None,
            "display_name": None,
            "site_url": None,
            "error": str(e)
        }


@app.get("/sharepoint/pages", tags=["SharePoint - Pages"])
def list_sharepoint_pages(
    site: Optional[str] = Query(None, description="Site name (uses 'default' if not specified)"),
    max_items: int = Query(100, description="Maximum number of pages to return"),
):
    """
    List all site pages from SharePoint.

    Returns a list of all pages in the specified SharePoint site.
    """
    try:
        client = get_sharepoint_client(site)
        pages = list(client.get_site_pages(max_items=max_items))
        return {
            "site": site or "default",
            "count": len(pages),
            "pages": pages
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch pages: {e}")


@app.get("/sharepoint/pages/{page_id}", tags=["SharePoint - Pages"])
def get_sharepoint_page(
    page_id: str,
    site: Optional[str] = Query(None, description="Site name (uses 'default' if not specified)"),
):
    """
    Get a specific SharePoint page by ID.

    Returns the page metadata and properties.
    """
    try:
        client = get_sharepoint_client(site)
        page = client.get_page_by_id(page_id)
        return page
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch page: {e}")


@app.get("/sharepoint/pages/{page_id}/content", tags=["SharePoint - Pages"])
def get_sharepoint_page_content(
    page_id: str,
    site: Optional[str] = Query(None, description="Site name (uses 'default' if not specified)"),
):
    """
    Get the content (web parts) of a SharePoint page.

    Returns the page with its canvas layout and web parts.
    """
    try:
        client = get_sharepoint_client(site)
        page = client.get_page_with_content(page_id)
        web_parts = client.get_page_content(page_id)
        return {
            "page": page,
            "webParts": web_parts
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch page content: {e}")


@app.get("/sharepoint/lists", tags=["SharePoint - Lists"])
def list_sharepoint_lists(
    site: Optional[str] = Query(None, description="Site name (uses 'default' if not specified)"),
    max_items: int = Query(100, description="Maximum number of lists to return"),
):
    """
    List all lists from the SharePoint site.

    Returns all SharePoint lists including document libraries.
    """
    try:
        client = get_sharepoint_client(site)
        lists = list(client.get_lists(max_items=max_items))
        return {
            "site": site or "default",
            "count": len(lists),
            "lists": lists
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch lists: {e}")


@app.get("/sharepoint/lists/{list_id}", tags=["SharePoint - Lists"])
def get_sharepoint_list(
    list_id: str,
    site: Optional[str] = Query(None, description="Site name (uses 'default' if not specified)"),
):
    """
    Get a specific SharePoint list by ID.

    Returns list metadata and properties.
    """
    try:
        client = get_sharepoint_client(site)
        lst = client.get_list_by_id(list_id)
        return lst
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch list: {e}")


@app.get("/sharepoint/lists/{list_id}/items", tags=["SharePoint - Lists"])
def get_sharepoint_list_items(
    list_id: str,
    site: Optional[str] = Query(None, description="Site name (uses 'default' if not specified)"),
    max_items: int = Query(100, description="Maximum number of items to return"),
    filter_query: Optional[str] = Query(None, description="OData filter query (e.g., 'fields/Status eq \\'Active\\'')"),
):
    """
    Get items from a SharePoint list.

    Returns list items with their field values expanded.
    """
    try:
        client = get_sharepoint_client(site)
        items = list(client.get_list_items(
            list_id=list_id,
            max_items=max_items,
            filter_query=filter_query,
        ))
        return {
            "site": site or "default",
            "count": len(items),
            "items": items
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch list items: {e}")


@app.get("/sharepoint/lists/{list_id}/drive", tags=["SharePoint - List Drive"])
def get_sharepoint_list_drive(
    list_id: str,
    site: Optional[str] = Query(None, description="Site name (uses 'default' if not specified)"),
):
    """
    Get the drive associated with a list (for document libraries).

    Returns drive metadata for a SharePoint list that is a document library.
    """
    try:
        client = get_sharepoint_client(site)
        drive = client.get_list_drive(list_id)
        return drive
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch list drive: {e}")


@app.get("/sharepoint/lists/{list_id}/drive/root", tags=["SharePoint - List Drive"])
def get_sharepoint_list_drive_root(
    list_id: str,
    site: Optional[str] = Query(None, description="Site name (uses 'default' if not specified)"),
):
    """
    Get the root folder of a list's drive (document library).

    Returns the root folder metadata for a document library.
    """
    try:
        client = get_sharepoint_client(site)
        drive_root = client.get_list_drive_root(list_id)
        return drive_root
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch list drive root: {e}")


@app.get("/sharepoint/lists/{list_id}/drive/children", tags=["SharePoint - List Drive"])
def get_sharepoint_list_drive_children(
    list_id: str,
    site: Optional[str] = Query(None, description="Site name (uses 'default' if not specified)"),
    folder_path: str = Query("", description="Path to subfolder (empty for root)"),
    max_items: int = Query(100, description="Maximum number of items to return"),
    recursive: bool = Query(False, description="Whether to recursively get items from subfolders"),
):
    """
    Get children (files and folders) from a list's drive root.

    Equivalent to Graph API: `/sites/{SiteID}/lists/{ListID}/drive/root/children`

    Returns files and folders from a document library list.
    """
    try:
        client = get_sharepoint_client(site)
        items = list(client.get_list_drive_children(
            list_id=list_id,
            folder_path=folder_path,
            max_items=max_items,
            recursive=recursive,
        ))
        return {
            "site": site or "default",
            "list_id": list_id,
            "folder_path": folder_path or "/",
            "count": len(items),
            "items": items
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch list drive children: {e}")


@app.get("/sharepoint/drive", tags=["SharePoint - Drive"])
def get_sharepoint_drive_root(
    site: Optional[str] = Query(None, description="Site name (uses 'default' if not specified)"),
):
    """
    Get the drive root (default document library) information.

    Returns metadata for the default document library root folder.
    """
    try:
        client = get_sharepoint_client(site)
        drive_root = client.get_drive_root()
        return drive_root
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch drive root: {e}")


@app.get("/sharepoint/drives", tags=["SharePoint - Drive"])
def list_sharepoint_drives(
    site: Optional[str] = Query(None, description="Site name (uses 'default' if not specified)"),
):
    """
    List all drives (document libraries) in the SharePoint site.

    Returns all document libraries available in the site.
    """
    try:
        client = get_sharepoint_client(site)
        drives = list(client.get_drives())
        return {
            "site": site or "default",
            "count": len(drives),
            "drives": drives
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch drives: {e}")


@app.get("/sharepoint/drive/items", tags=["SharePoint - Drive"])
def get_sharepoint_drive_items(
    site: Optional[str] = Query(None, description="Site name (uses 'default' if not specified)"),
    folder_path: str = Query("", description="Path to folder (empty for root)"),
    max_items: int = Query(100, description="Maximum number of items to return"),
    recursive: bool = Query(False, description="Whether to recursively get items from subfolders"),
    drive_id: Optional[str] = Query(None, description="Specific drive ID (uses default drive if not specified)"),
):
    """
    Get items from the document library.

    Returns files and folders from the default or specified document library.
    """
    try:
        client = get_sharepoint_client(site)
        items = list(client.get_drive_items(
            drive_id=drive_id,
            folder_path=folder_path,
            max_items=max_items,
            recursive=recursive,
        ))
        return {
            "site": site or "default",
            "count": len(items),
            "items": items
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch drive items: {e}")


@app.get("/sharepoint/search", tags=["SharePoint - Search"])
def search_sharepoint(
    query: str = Query(..., description="Search query string"),
    site: Optional[str] = Query(None, description="Site name (uses 'default' if not specified)"),
    max_items: int = Query(100, description="Maximum number of results"),
    entity_types: str = Query("driveItem,listItem", description="Comma-separated entity types to search"),
):
    """
    Search for content in SharePoint.

    Searches across files, list items, and other content in the SharePoint site.

    **Entity Types:**
    - `driveItem`: Files and folders
    - `listItem`: List items
    - `site`: Sites
    """
    try:
        client = get_sharepoint_client(site)
        types = [t.strip() for t in entity_types.split(",")]
        results = client.search(query=query, entity_types=types, max_items=max_items)
        return {
            "site": site or "default",
            "count": len(results),
            "results": results
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Search failed: {e}")


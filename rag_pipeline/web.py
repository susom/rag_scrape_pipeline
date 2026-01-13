"""
RPP Web API - RAG Preparation Pipeline
Primary interface for the RAG preparation tool.
"""

from fastapi import FastAPI, HTTPException, Body, UploadFile, File, Form
from fastapi.responses import HTMLResponse, FileResponse
from typing import List
import json
import os
import pdfplumber
import io

try:
    import docx2txt
except ImportError:
    docx2txt = None

from rag_pipeline.main import run_pipeline
from rag_pipeline.output_json import generate_run_id, write_canonical_json, RPP_VERSION
from rag_pipeline.utils.logger import setup_logger
from rag_pipeline.processing.sliding_window import SlidingWindowParser
from rag_pipeline.processing.ai_client import AVAILABLE_MODELS, DEFAULT_MODEL
from datetime import datetime, timezone

app = FastAPI(title="RPP - RAG Preparation Pipeline")
logger = setup_logger()

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
            <br>
            <button onclick="uploadFile()">Upload & Process</button>
            <div id="uploadResult" class="result"></div>
        </div>
    </div>

    <script>
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
            resultDiv.style.display = 'none';
            resultDiv.innerHTML = `Processing ${{fileCount}} file(s)...`;
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

            try {{
                const res = await fetch('/upload', {{ method: 'POST', body: formData }});
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
    user_template: str = Form("")
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
            documents.append({
                "uri": f"file://{file.filename}",
                "source_type": filename_lower.split(".")[-1],
                "cached_files": {"raw_text": txt_path},
                "followed_from": None,
                "sections": sections,
                "errors": [],
            })

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


@app.get("/download/{run_id}")
def download_output(run_id: str):
    """Download the canonical JSON output for a run."""
    file_path = os.path.join("cache", "rag_ready", f"{run_id}.json")

    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail=f"Output not found for run_id: {run_id}")

    return FileResponse(
        file_path,
        media_type="application/json",
        filename=f"{run_id}.json"
    )


@app.get("/health")
def health_check():
    return {"health": "ok", "version": RPP_VERSION}

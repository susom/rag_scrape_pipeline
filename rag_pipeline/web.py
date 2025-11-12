from fastapi import FastAPI, HTTPException, BackgroundTasks, Body
from rag_pipeline.main import main as run_pipeline
from rag_pipeline.utils.logger import setup_logger
import os

app = FastAPI(title="RAG Scrape Pipeline API")
logger = setup_logger()

@app.get("/")
def root():
    return {"status": "ok", "message": "RAG Pipeline service running"}

@app.post("/run")
def run_scrape(payload: dict = Body(...)):
    urls = payload.get("urls", [])
    if not urls:
        raise HTTPException(status_code=400, detail="No URLs provided")

    logger.info(f"Running pipeline synchronously for {len(urls)} URLs")

    os.makedirs("config", exist_ok=True)
    with open("config/urls.txt", "w") as f:
        f.write("\n".join(urls))

    run_pipeline(urls)

    logger.info("Pipeline completed successfully")
    return {"status": "completed", "url_count": len(urls)}


@app.get("/health")
def health_check():
    return {"health": "ok"}

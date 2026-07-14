FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PORT=8080 \
    TIKTOKEN_CACHE_DIR=/app/tiktoken_cache

WORKDIR /app

RUN apt-get update && apt-get install -y \
    build-essential \
    libpoppler-cpp-dev \
    pkg-config \
    python3-dev \
  && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download tiktoken's cl100k_base encoding at build time so the runtime
# pod never reaches out to openaipublic.blob.core.windows.net (blocked by the
# locked-down egress network policy). The baked cache is read-only at runtime.
RUN mkdir -p "$TIKTOKEN_CACHE_DIR" \
  && python -c "import tiktoken; tiktoken.get_encoding('cl100k_base'); tiktoken.encoding_for_model('gpt-4')"

COPY . .
RUN mkdir -p cache

CMD ["uvicorn", "rag_pipeline.web:app", "--host", "0.0.0.0", "--port", "8080"]


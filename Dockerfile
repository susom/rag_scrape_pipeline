# Dockerfile

FROM python:3.10-slim

# Set working directory
WORKDIR /app

# Install system dependencies (for PDF parsing libraries if needed)
RUN apt-get update && apt-get install -y \
    build-essential \
    libpoppler-cpp-dev \
    pkg-config \
    python3-dev \
  && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files, including config and cache folders
COPY . .

# Ensure cache directory exists (optional, will be created by Python as well)
RUN mkdir -p cache

# Default command to run your main Python script (adjust as needed)
CMD ["python", "-m", "rag_pipeline.cli"]



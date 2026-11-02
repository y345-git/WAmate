# Use lightweight official Python runtime
FROM python:3.12-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive

# Install system dependencies:
# - ffmpeg: required by whatsmeow/neonize for audio/video media operations
# - libmagic1: required for python-magic file MIME detection
# - sqlite3: SQLite database tools
# - build-essential & curl: build utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    ffmpeg \
    libmagic1 \
    sqlite3 \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy dependency specifications first to leverage Docker layer caching
COPY requirements.txt .

# Install Python packages
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . .

# Create directory for persistent database and session files
RUN mkdir -p /app/data

# Volume for session state and SQLite database
VOLUME ["/app/data"]

# Default entrypoint
CMD ["python", "main.py"]

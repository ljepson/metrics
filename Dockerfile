FROM python:3.12-slim

WORKDIR /app

# Install system dependencies for psycopg2
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY app.py .

# Run as non-root user
RUN useradd -m -u 1000 metrics && chown -R metrics:metrics /app
USER metrics

# Expose port
EXPOSE 8090

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import requests; requests.get('http://127.0.0.1:8090/health', timeout=5)"

# Run application
CMD ["python", "app.py"]

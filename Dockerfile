FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    HTTPS_ENABLED=0

WORKDIR /app

# System libraries for OpenCV headless, audio post-processing, and CUPS printing
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libgl1 \
    ffmpeg \
    cups-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd -m -u 1000 appuser \
    && mkdir -p /app/shared_output/docs /app/config /app/logs \
    && chown -R appuser:appuser /app

USER appuser

VOLUME ["/app/shared_output", "/app/config", "/app/logs"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\") or \"8000\"}/api/public/info', timeout=3)" || exit 1

CMD ["python", "monitoring_server.py"]

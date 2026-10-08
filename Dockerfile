# syntax=docker/dockerfile:1
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SYNC_DATA_DIR=/data \
    TZ=Etc/UTC

# tzdata: the scheduler and the COROS upload timezone both follow $TZ.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY static ./static

# Run unprivileged. /data is pre-created so a fresh named volume inherits this ownership.
RUN useradd --system --uid 10001 --create-home --home-dir /home/app app \
 && mkdir -p /data \
 && chown app:app /data \
 && chmod 700 /data
USER app

VOLUME ["/data"]
EXPOSE 8484

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8484/healthz', timeout=4).status == 200 else 1)"]

# Single process on purpose: the scheduler lives in-process, so never add --workers.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8484", "--proxy-headers", "--forwarded-allow-ips", "*"]

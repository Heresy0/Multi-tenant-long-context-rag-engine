FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Keep the official Debian repositories and certificate checks enabled.
# Retry interrupted downloads; the timeout applies to connections and stalled reads.
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=5 -o Acquire::https::Timeout=60 -o APT::Update::Error-Mode=any update \
    && apt-get -o Acquire::Retries=5 -o Acquire::https::Timeout=60 install --no-install-recommends -y \
        poppler-utils \
        tesseract-ocr \
        tesseract-ocr-chi-sim \
        tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system app \
    && useradd \
        --system \
        --gid app \
        --home-dir /app \
        app

COPY requirements.txt requirements.txt

RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY backend backend
COPY migrations migrations
COPY scripts scripts
COPY alembic.ini alembic.ini

RUN mkdir -p /app/data/documents \
    && chown -R app:app /app

USER app

EXPOSE 8000

HEALTHCHECK \
    --interval=30s \
    --timeout=5s \
    --start-period=20s \
    --retries=3 \
    CMD python -c \
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=3)"

CMD ["python", "-m","uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]

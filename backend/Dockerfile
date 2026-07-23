ARG BASE_IMAGE_REGISTRY=docker.io/library
FROM ${BASE_IMAGE_REGISTRY}/python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --system aegis \
    && useradd --system --gid aegis --home-dir /app aegis

COPY app ./app
COPY scripts ./scripts
COPY alembic.ini ./
COPY migrations ./migrations
COPY prompts ./prompts
COPY sample_data ./sample_data
RUN mkdir -p /app/uploads /app/qdrant_data \
    && chown -R aegis:aegis /app

USER aegis
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --proxy-headers --forwarded-allow-ips='*'"]

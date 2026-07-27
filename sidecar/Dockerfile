# Sidecar-контейнер: embeddings/tokenization proxy поруч із mcp.
# Легкий образ — лише HTTP-сервер (Starlette/uvicorn) + клієнт Voyage.
# Build context — корінь репо: `docker compose --profile voyage up --build`.
FROM python:3.12-slim

WORKDIR /app

COPY sidecar/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Лише те, що потрібно sidecar: спільний клієнт + сам сервіс.
COPY voyage_client.py ./
COPY sidecar/embeddings_service.py ./

# Том для кешу embeddings (переживає рестарт, якщо змонтувати).
RUN mkdir -p /cache

# Non-root (не працюємо від root у контейнері).
RUN useradd --create-home --uid 10002 sidecar && chown -R sidecar:sidecar /app /cache
USER sidecar

ENV ADD_EMBED_MODEL=voyage-4-lite \
    ADD_EMBED_DIM=1024 \
    EMB_HOST=0.0.0.0 \
    EMB_PORT=8100 \
    EMB_CACHE_FILE=/cache/embeddings.json \
    LOG_LEVEL=INFO

EXPOSE 8100

CMD ["python", "embeddings_service.py"]

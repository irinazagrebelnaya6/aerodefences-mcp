# MCP-сервер aerodefences. За замовчуванням піднімає HTTP-транспорт,
# щоб контейнер був повноцінним мережевим сервісом (деплой локально/у хмару).
FROM python:3.12-slim

WORKDIR /app

# Репродуковані білди: ставимо СТРОГО з lock-файла з перевіркою хешів,
# а не з плаваючих версій. Оновлення lock: `uv pip compile pyproject.toml
# -o requirements.lock --generate-hashes`.
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

# Код і локальні знання для RAG.
COPY server_aerodefences.py rag_index.py ./
COPY ad_config.py ad_metrics.py ad_db.py ad_security.py ./
COPY ad_resources.py ad_prompts.py ad_tools_read.py ad_tools_write.py ad_tools_rag.py ./
COPY knowledge/ ./knowledge/

# Non-root користувач (не працюємо від root у контейнері).
RUN useradd --create-home --uid 10001 app && chown -R app:app /app
USER app

# Мережевий транспорт усередині контейнера + прод-дефолти.
# ADD_ROLE=viewer — deny-by-default; підвищення лише через JWT-claims.
# JWT-ключі (ADD_JWT_JWKS_URI / ADD_JWT_PUBLIC_KEY) обовʼязкові для HTTP —
# передаються під час запуску (compose/секрети), у образ НЕ вшиваються.
ENV ADD_TRANSPORT=http \
    ADD_HTTP_HOST=0.0.0.0 \
    ADD_HTTP_PORT=8000 \
    ADD_METRICS_PORT=9100 \
    ADD_LOG_LEVEL=INFO \
    ADD_LOG_FORMAT=json \
    ADD_ROLE=viewer

EXPOSE 8000 9100

CMD ["python", "server_aerodefences.py"]

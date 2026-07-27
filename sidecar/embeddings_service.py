"""
Sidecar-сервіс embeddings (мікросервісний патерн Sidecar).

Окремий контейнер поруч із mcp: тримає клієнт Voyage AI + кеш (токенізація/
кешування винесені з основного сервера). mcp звертається сюди по HTTP замість
прямого виклику Voyage — тож логіка ембедингів має власний масштаб, лог і кеш.

Ендпоінти:
  POST /embed   {texts:[...], input_type:"document"|"query"} -> {vectors, model, dim}
  GET  /health  -> {status, model, dim}

Кеш (лише для документів; запити — одноразові, їх кешує semantic cache у mcp)
дисковий, ключ sha256(model|dim|text) — переживає рестарт контейнера, якщо
змонтувати том на CACHE_FILE.
"""

from __future__ import annotations

import hashlib
import logging
import os
import pathlib

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from voyage_client import EmbeddingsError, VoyageClient, cache_load, cache_save

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("embeddings-sidecar")

MODEL = os.getenv("ADD_EMBED_MODEL", "voyage-4-lite")
DIM = int(os.getenv("ADD_EMBED_DIM", "1024"))
CACHE_FILE = pathlib.Path(os.getenv("EMB_CACHE_FILE", "/cache/embeddings.json"))

_api_key = os.getenv("VOYAGE_API_KEY") or ""
if not _api_key:
    # Fail-safe: sidecar без ключа не має сенсу (як HTTP-mcp без JWT).
    raise SystemExit("embeddings-sidecar: VOYAGE_API_KEY is required")

client = VoyageClient(api_key=_api_key, model=MODEL, dim=DIM)


async def embed(request: Request) -> JSONResponse:
    body = await request.json()
    texts = body.get("texts") or []
    input_type = body.get("input_type", "document")
    try:
        if input_type == "document":
            vectors = await _embed_documents_cached(texts)
        else:
            vectors = await client.embed(texts, input_type=input_type)
    except EmbeddingsError as e:
        # назовні — нейтральний код, без ключа/деталей у тілі
        log.warning("embed failed: %s", e)
        return JSONResponse({"error": "embeddings backend unavailable"}, status_code=502)
    return JSONResponse({"vectors": vectors, "model": MODEL, "dim": DIM})


async def _embed_documents_cached(texts: list[str]) -> list[list[float]]:
    """Документи кешуємо на диску: платимо Voyage лише за нові тексти."""
    prefix = f"{MODEL}|{DIM}|"
    cache = cache_load(CACHE_FILE)
    keys = [prefix + hashlib.sha256(t.encode("utf-8")).hexdigest() for t in texts]
    missing_idx = [i for i, k in enumerate(keys) if k not in cache]
    if missing_idx:
        fresh = await client.embed([texts[i] for i in missing_idx], input_type="document")
        for i, vec in zip(missing_idx, fresh):
            cache[keys[i]] = vec
        cache_save(CACHE_FILE, cache)
        log.info("embedded %d/%d docs (%d cached)",
                 len(missing_idx), len(texts), len(texts) - len(missing_idx))
    return [cache[k] for k in keys]


async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "model": MODEL, "dim": DIM})


app = Starlette(routes=[
    Route("/embed", embed, methods=["POST"]),
    Route("/health", health, methods=["GET"]),
])


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.getenv("EMB_HOST", "0.0.0.0"),
        port=int(os.getenv("EMB_PORT", "8100")),
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
    )

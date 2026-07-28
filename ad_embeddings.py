"""
Embeddings-шар mcp-сервера: семантичний бекенд для RAG.

Клієнт embeddings обирається за конфігом (див. RAG_EMBEDDINGS_PLAN.md §4):
  - **прямий режим** — `VoyageClient` б'є Voyage AI API напряму, з дисковим
    кешем документів у процесі mcp;
  - **sidecar-режим** (`ADD_EMBEDDINGS_URL`) — `SidecarClient` ходить по HTTP
    до ОКРЕМОГО контейнера `embeddings` (patternи Sidecar): той тримає клієнт
    Voyage + кеш (токенізація/кешування винесені з mcp у власний сервіс).

`VoyageBackend` реалізує протокол SearchBackend (rag_index.py). Помилки
мережі/ключа підіймаються як EmbeddingsError — RagIndex деградує на TF-IDF.
"""

from __future__ import annotations

import pathlib

import httpx

from ad_config import config, log
from rag_index import _Doc
from voyage_client import (
    EmbeddingsError,
    VoyageClient,
    cache_load,
    cache_save,
    dot,
)

# Реекспорт для сумісності з наявними тестами/імпортами.
_dot = dot
__all__ = [
    "VoyageBackend", "VoyageClient", "SidecarClient", "EmbeddingsError",
    "make_embeddings_client", "_dot",
]

CACHE_FILE = pathlib.Path(__file__).parent / ".cache" / "rag_embeddings.json"


class SidecarClient:
    """HTTP-клієнт до контейнера-sidecar `embeddings` (той самий інтерфейс
    `.embed(texts, input_type)`, що й VoyageClient — тож VoyageBackend не
    відрізняє джерело)."""

    def __init__(self, base_url: str, model: str, dim: int, timeout: float = 35.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.dim = dim
        self._timeout = timeout

    async def embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        if not texts:
            return []
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/embed",
                    json={"texts": texts, "input_type": input_type},
                )
        except httpx.HTTPError as e:
            raise EmbeddingsError(f"sidecar unreachable: {type(e).__name__}") from e
        if resp.status_code != 200:
            raise EmbeddingsError(f"sidecar http {resp.status_code}")
        return resp.json()["vectors"]


def _primary_provider() -> "tuple[str, VoyageClient | SidecarClient]":
    """Основний embeddings-провайдер (sidecar або прямий Voyage), або
    EmbeddingsError, якщо джерело не задано."""
    if config.embeddings_url:
        return ("sidecar", SidecarClient(
            config.embeddings_url, config.embed_model, config.embed_dim))
    if config.voyage_api_key:
        return ("voyage", VoyageClient(
            api_key=config.voyage_api_key, model=config.embed_model, dim=config.embed_dim))
    raise EmbeddingsError(
        "embeddings-джерело не задано: потрібен VOYAGE_API_KEY або ADD_EMBEDDINGS_URL"
    )


def make_embeddings_client():
    """Клієнт embeddings за конфігом (спільно для voyage/qdrant бекендів).

    `ADD_EMBED_GATEWAY=on` → API Gateway (маршрутизація до провайдерів із
    фолбеком, `ad_gateway.py`); інакше — єдиний основний провайдер."""
    if config.embed_gateway:
        from ad_gateway import build_gateway
        return build_gateway()
    return _primary_provider()[1]


class VoyageBackend:
    """Семантичний бекенд RAG: embeddings (Voyage напряму або через sidecar)
    + косинус у памʼяті."""

    name = "voyage"

    def __init__(self, client: VoyageClient | SidecarClient | None = None) -> None:
        self.client = client if client is not None else make_embeddings_client()
        # У sidecar-режимі кеш тримає сам sidecar → локальний дисковий вимикаємо.
        self._local_cache = not isinstance(self.client, SidecarClient)
        if isinstance(self.client, SidecarClient):
            log.info("embeddings via sidecar %s", config.embeddings_url)
        self.docs: list[_Doc] = []
        self._vecs: list[list[float]] = []

    async def build(self, docs: list[_Doc]) -> None:
        if not self._local_cache:
            # sidecar сам кешує — просто просимо ембеддинги документів
            texts = [d.index_text() for d in docs]
            self._vecs = await self.client.embed(texts, input_type="document")
            self.docs = docs
            return

        # прямий режим: локальний дисковий кеш. Ключ включає модель і розмірність
        # (зміна будь-чого з них робить старі вектори непорівнюваними).
        prefix = f"{self.client.model}|{self.client.dim}|"
        cache = cache_load(CACHE_FILE)
        keys = [prefix + d.content_hash() for d in docs]

        missing = [(k, d) for k, d in zip(keys, docs) if k not in cache]
        if missing:
            texts = [d.index_text() for _, d in missing]
            vectors = await self.client.embed(texts, input_type="document")
            for (k, _), vec in zip(missing, vectors):
                cache[k] = vec
            cache_save(CACHE_FILE, cache)
            log.info(
                "voyage: embedded %d/%d documents (%d from cache)",
                len(missing), len(docs), len(docs) - len(missing),
            )

        self._vecs = [cache[k] for k in keys]
        self.docs = docs

    async def scores(
        self, question: str, query_vec: list[float] | None = None
    ) -> list[tuple[float, _Doc]]:
        # semantic cache вже міг порахувати вектор — не ембедимо вдруге
        q_vec = query_vec if query_vec is not None else await self.embed_query(question)
        return [
            (score, d)
            for d, vec in zip(self.docs, self._vecs)
            if (score := dot(q_vec, vec)) > 0
        ]

    async def embed_query(self, question: str) -> list[float]:
        """Вектор запиту. Окремим методом — його ж використовує
        semantic cache (Фаза E), щоб не ембедити двічі."""
        return (await self.client.embed([question], input_type="query"))[0]

    def extra_status(self) -> dict:
        via = "sidecar" if isinstance(self.client, SidecarClient) else "direct"
        return {"model": self.client.model, "dim": self.client.dim, "via": via}

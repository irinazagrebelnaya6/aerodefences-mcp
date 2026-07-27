"""
Embeddings-шар: клієнт Voyage AI + семантичний бекенд для RAG.

Voyage AI — рекомендований embeddings-провайдер в екосистемі Anthropic
(власного embeddings API Anthropic не має). Модель `voyage-4-lite` —
багатомовна (укр ✓), вектори нормовані до довжини 1, тож косинусна
схожість == скалярний добуток (чистий Python, без numpy).

Мікросервісний патерн тут — «зовнішній LLM-сервіс за тонким клієнтом»:
уся мережа інкапсульована в VoyageClient (таймаути, ретраї, безпечні
помилки без витоку ключа); бекенд ним лише користується. Fallback на
TF-IDF при недоступності робить RagIndex (див. rag_index.py).

Фаза C: дисковий кеш ембедінгів документів (.cache/rag_embeddings.json),
ключ — sha256(model|dim|text), тож повторні build-и не платять за
незмінені документи ані грошима, ані latency.
"""

from __future__ import annotations

import asyncio
import json
import pathlib

import httpx

from ad_config import config, log
from rag_index import _Doc

VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"
CACHE_FILE = pathlib.Path(__file__).parent / ".cache" / "rag_embeddings.json"

# Ліміт Voyage API — до 1000 текстів на запит; наш корпус (~80 документів)
# влазить в один батч, але тримаємо константу явною.
BATCH_SIZE = 128


class EmbeddingsError(RuntimeError):
    """Помилка embeddings-шару. Текст безпечний для логів (без ключа)."""


class VoyageClient:
    """Мінімальний async-клієнт Voyage AI: один POST-ендпоінт.

    Свідомо без пакета `voyageai` — філософія проєкту «мінімум залежностей»,
    httpx уже є (транзитивно від fastmcp).
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        dim: int,
        timeout: float = 30.0,
        retries: int = 2,
    ) -> None:
        self._api_key = api_key
        self.model = model
        self.dim = dim
        self._timeout = timeout
        self._retries = retries

    async def embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        """Ембедить пачку текстів. `input_type`: 'document' | 'query'
        (Voyage додає різні префікси — це важливо для якості retrieval)."""
        if not texts:
            return []
        out: list[list[float]] = []
        for i in range(0, len(texts), BATCH_SIZE):
            out.extend(await self._embed_batch(texts[i:i + BATCH_SIZE], input_type))
        return out

    async def _embed_batch(
        self, texts: list[str], input_type: str
    ) -> list[list[float]]:
        payload = {
            "input": texts,
            "model": self.model,
            "input_type": input_type,
            "output_dimension": self.dim,
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        last_err: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(VOYAGE_URL, json=payload, headers=headers)
                if resp.status_code in (429,) or resp.status_code >= 500:
                    # ретрайабельні: rate limit / збій провайдера
                    last_err = EmbeddingsError(f"voyage http {resp.status_code}")
                    await asyncio.sleep(0.5 * (2 ** attempt))
                    continue
                if resp.status_code != 200:
                    # 4xx (крім 429) — ретраї не допоможуть
                    raise EmbeddingsError(f"voyage http {resp.status_code}")
                data = resp.json()["data"]
                # API повертає з index — сортуємо для гарантії порядку
                data.sort(key=lambda item: item["index"])
                return [item["embedding"] for item in data]
            except httpx.HTTPError as e:
                # мережевий збій — без деталей URL/ключа в тексті
                last_err = EmbeddingsError(f"voyage network error: {type(e).__name__}")
                await asyncio.sleep(0.5 * (2 ** attempt))
        raise last_err or EmbeddingsError("voyage: unknown error")


def _dot(a: list[float], b: list[float]) -> float:
    """Вектори Voyage нормовані → dot == косинус. ~80 документів × 1024
    float — чистого Python достатньо, numpy не потрібен."""
    return sum(x * y for x, y in zip(a, b))


# ── Дисковий кеш ембедінгів документів (Фаза C) ─────────────────────────
def _cache_load() -> dict[str, list[float]]:
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _cache_save(cache: dict[str, list[float]]) -> None:
    try:
        CACHE_FILE.parent.mkdir(exist_ok=True)
        CACHE_FILE.write_text(json.dumps(cache), encoding="utf-8")
    except OSError as e:  # кеш — оптимізація, не критичний шлях
        log.warning("embeddings cache not saved: %s", type(e).__name__)


class VoyageBackend:
    """Семантичний бекенд RAG: embeddings Voyage AI + косинус у памʼяті.

    Реалізує протокол SearchBackend (rag_index.py). Помилки мережі/ключа
    підіймаються як EmbeddingsError — RagIndex деградує на TF-IDF.
    """

    name = "voyage"

    def __init__(self, client: VoyageClient | None = None) -> None:
        if client is None:
            if not config.voyage_api_key:
                # fail-safe, як із JWT: бекенд без ключа не створюється
                raise EmbeddingsError(
                    "ADD_RAG_BACKEND=voyage вимагає VOYAGE_API_KEY"
                )
            client = VoyageClient(
                api_key=config.voyage_api_key,
                model=config.embed_model,
                dim=config.embed_dim,
            )
        self.client = client
        self.docs: list[_Doc] = []
        self._vecs: list[list[float]] = []

    async def build(self, docs: list[_Doc]) -> None:
        # Кеш-ключ включає модель і розмірність: зміна будь-чого з них
        # робить старі вектори непорівнюваними.
        prefix = f"{self.client.model}|{self.client.dim}|"
        cache = _cache_load()
        keys = [prefix + d.content_hash() for d in docs]

        missing = [(k, d) for k, d in zip(keys, docs) if k not in cache]
        if missing:
            texts = [d.index_text() for _, d in missing]
            vectors = await self.client.embed(texts, input_type="document")
            for (k, _), vec in zip(missing, vectors):
                cache[k] = vec
            _cache_save(cache)
            log.info(
                "voyage: embedded %d/%d documents (%d from cache)",
                len(missing), len(docs), len(docs) - len(missing),
            )

        self._vecs = [cache[k] for k in keys]
        self.docs = docs

    async def scores(self, question: str) -> list[tuple[float, _Doc]]:
        q_vec = await self.embed_query(question)
        return [
            (score, d)
            for d, vec in zip(self.docs, self._vecs)
            if (score := _dot(q_vec, vec)) > 0
        ]

    async def embed_query(self, question: str) -> list[float]:
        """Вектор запиту. Окремим методом — його ж використовує
        semantic cache (Фаза E), щоб не ембедити двічі."""
        return (await self.client.embed([question], input_type="query"))[0]

    def extra_status(self) -> dict:
        return {"model": self.client.model, "dim": self.client.dim}

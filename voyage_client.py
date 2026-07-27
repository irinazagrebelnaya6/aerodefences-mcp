"""
Легкий клієнт Voyage AI + утиліти кешу embeddings.

Навмисно БЕЗ залежностей від fastmcp/ad_config — лише httpx + stdlib. Це
дозволяє використовувати модуль з ДВОХ місць:
  1) у процесі mcp-сервера (`ad_embeddings.py`) — прямий режим;
  2) у ОКРЕМОМУ контейнері-sidecar (`sidecar/embeddings_service.py`), який
     тримає клієнт Voyage + кеш і віддає embeddings по HTTP.

Саме цей спільний модуль робить винесення логіки токенізації/кешування у
sidecar можливим без дублювання коду.
"""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib

import httpx

log = logging.getLogger("voyage_client")

VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"

# Ліміт Voyage API — до 1000 текстів на запит; наш корпус (~80 документів)
# влазить в один батч, але тримаємо константу явною.
BATCH_SIZE = 128


class EmbeddingsError(RuntimeError):
    """Помилка embeddings-шару. Текст безпечний для логів (без ключа)."""


class VoyageClient:
    """Мінімальний async-клієнт Voyage AI: один POST-ендпоінт.

    Свідомо без пакета `voyageai` — філософія проєкту «мінімум залежностей»,
    httpx уже є.
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


def dot(a: list[float], b: list[float]) -> float:
    """Вектори Voyage нормовані → dot == косинус. ~80 документів × 1024
    float — чистого Python достатньо, numpy не потрібен."""
    return sum(x * y for x, y in zip(a, b))


# ── Дисковий кеш ембедінгів (ключ передається явно, щоб кеш був спільною
#    утилітою для mcp і sidecar) ─────────────────────────────────────────
def cache_load(path: pathlib.Path) -> dict[str, list[float]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def cache_save(path: pathlib.Path, cache: dict[str, list[float]]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache), encoding="utf-8")
    except OSError as e:  # кеш — оптимізація, не критичний шлях
        log.warning("embeddings cache not saved: %s", type(e).__name__)

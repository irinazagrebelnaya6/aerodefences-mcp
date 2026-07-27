"""
Semantic Cache (мікросервісний патерн для LLM) поверх Redis.

Проблема: користувачі ставлять СЕМАНТИЧНО однакові, але ТЕКСТУАЛЬНО різні
запити («скільки коштує X» ≈ «яка ціна X»). Точний кеш їх не ловить → кожен
перефраз = зайвий дорогий виклик embeddings API.

Рішення: кешуємо за СЕНСОМ. Вектор запиту (уже порахований voyage-бекендом —
без додаткових викликів) шукаємо серед збережених; якщо косинус ≥ поріг —
повертаємо збережений результат retrieval, не торкаючись індексу.

Чому Redis, а не in-memory: кеш СПІЛЬНИЙ між репліками (на відміну від
in-memory індексу). Інфраструктура вже є — `ADD_REDIS_URL` для сесій.

Інвалідація (каталог змінюється!):
  - TTL (`ADD_SEMCACHE_TTL`);
  - повне скидання простору `semcache:*` на КОЖНІЙ write-операції — робиться
    в єдиному барʼєрі `Database.run_write` (Event-Driven: «catalog.updated»).
Поріг свідомо високий: false positive (відповідь на ІНШЕ питання) гірший за miss.

Працює лише коли: `ADD_SEMCACHE=on`, заданий `ADD_REDIS_URL`, і бекенд —
`voyage` (для TF-IDF вектори не переносяться між перебудовами → сенсу немає).
"""

from __future__ import annotations

import asyncio
import hashlib
import json

from ad_config import config, log

_NS = "semcache:"


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


class SemanticCache:
    """Redis-backed семантичний кеш для результатів `ask_catalog`."""

    def __init__(self, redis_client, ttl: int, threshold: float) -> None:
        self._r = redis_client
        self._ttl = ttl
        self._threshold = threshold

    def _key(self, question: str) -> str:
        # ключ за текстом (унікальність запису); пошук — за вектором
        return _NS + hashlib.sha256(question.encode("utf-8")).hexdigest()

    async def get(self, query_vec: list[float]) -> list[dict] | None:
        """Найближчий збережений запит із косинусом ≥ поріг, інакше None.

        Вектори Voyage нормовані → dot == косинус. Скан простору `semcache:*`
        лінійний за розміром кешу — прийнятно для нашого масштабу; за великого
        кешу тут доречний RediSearch KNN.
        """
        best_score, best_results = 0.0, None
        for key in await asyncio.to_thread(self._r.scan_iter, f"{_NS}*"):
            raw = await asyncio.to_thread(self._r.get, key)
            if not raw:
                continue
            entry = json.loads(raw)
            score = _dot(query_vec, entry["vec"])
            if score > best_score:
                best_score, best_results = score, entry["results"]
        if best_results is not None and best_score >= self._threshold:
            log.info("semcache hit (score=%.3f)", best_score)
            return best_results
        return None

    async def put(
        self, question: str, query_vec: list[float], results: list[dict]
    ) -> None:
        payload = json.dumps({"vec": query_vec, "results": results}, default=str)
        await asyncio.to_thread(
            self._r.set, self._key(question), payload, self._ttl
        )

    async def flush(self) -> int:
        """Скинути весь простір кешу (виклик на write). Повертає к-сть ключів."""
        keys = list(await asyncio.to_thread(self._r.scan_iter, f"{_NS}*"))
        if keys:
            await asyncio.to_thread(self._r.delete, *keys)
        return len(keys)


# ── Синглтон і фабрика ──────────────────────────────────────────────────
_instance: SemanticCache | None = None
_resolved = False


def _redis_client():
    """Ліниво піднімає redis-клієнт з ADD_REDIS_URL (той самий, що для сесій)."""
    import os
    url = os.getenv("ADD_REDIS_URL")
    if not url:
        return None
    import redis
    return redis.Redis.from_url(url, decode_responses=True)


def get_semcache() -> SemanticCache | None:
    """Кеш, якщо ADD_SEMCACHE=on і є Redis; інакше None (фіча вимкнена)."""
    global _instance, _resolved
    if _resolved:
        return _instance
    _resolved = True
    if config.semcache:
        client = _redis_client()
        if client is not None:
            _instance = SemanticCache(
                client, ttl=config.semcache_ttl, threshold=config.semcache_threshold
            )
        else:
            log.warning("ADD_SEMCACHE=on, але ADD_REDIS_URL не задано — кеш вимкнено")
    return _instance


async def invalidate_semcache() -> None:
    """Скинути кеш після зміни каталогу (Event-Driven інвалідація).

    Викликається з єдиного write-барʼєра. Мовчазний no-op, якщо кеш вимкнено.
    """
    sc = get_semcache()
    if sc is None:
        return
    try:
        n = await sc.flush()
        if n:
            log.info("semcache invalidated: %d entries dropped", n)
    except Exception as e:  # noqa: BLE001 — кеш не критичний для запису
        log.warning("semcache flush failed: %s", type(e).__name__)

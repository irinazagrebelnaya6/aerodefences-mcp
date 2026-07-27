"""Тести Semantic Cache БЕЗ Redis і БЕЗ мережі.

Redis мокаємо мінімальним in-memory фейком (get/set/scan_iter/delete),
тож CI зелений без сервера. Перевіряємо: влучання за схожим вектором,
промах під порогом, і Event-Driven інвалідацію (flush).
"""

from ad_semcache import SemanticCache


class FakeRedis:
    """Мінімальний in-memory Redis: рівно те, що використовує SemanticCache."""

    def __init__(self):
        self.store: dict[str, str] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, val, ttl=None):
        self.store[key] = val

    def scan_iter(self, pattern):
        prefix = pattern.rstrip("*")
        return [k for k in list(self.store) if k.startswith(prefix)]

    def delete(self, *keys):
        for k in keys:
            self.store.pop(k, None)


def _unit(*vals) -> list[float]:
    norm = sum(v * v for v in vals) ** 0.5 or 1.0
    return [v / norm for v in vals]


RESULTS = [{"doc_id": "db:product:x", "score": 1.0, "snippet": "..."}]


async def test_hit_on_similar_vector():
    sc = SemanticCache(FakeRedis(), ttl=3600, threshold=0.93)
    await sc.put("яка ціна X", _unit(1.0, 0.05, 0.0), RESULTS)

    # майже той самий напрям → косинус вище порога
    hit = await sc.get(_unit(1.0, 0.1, 0.0))
    assert hit == RESULTS


async def test_miss_below_threshold():
    sc = SemanticCache(FakeRedis(), ttl=3600, threshold=0.93)
    await sc.put("яка ціна X", _unit(1.0, 0.0, 0.0), RESULTS)

    # ортогональний запит → нижче порога → промах
    assert await sc.get(_unit(0.0, 1.0, 0.0)) is None


async def test_empty_cache_returns_none():
    sc = SemanticCache(FakeRedis(), ttl=3600, threshold=0.93)
    assert await sc.get(_unit(1.0, 0.0, 0.0)) is None


async def test_flush_invalidates_all():
    r = FakeRedis()
    sc = SemanticCache(r, ttl=3600, threshold=0.93)
    await sc.put("q1", _unit(1.0, 0.0), RESULTS)
    await sc.put("q2", _unit(0.0, 1.0), RESULTS)
    r.store["other:key"] = "keep-me"  # не наш простір — не чіпати

    dropped = await sc.flush()
    assert dropped == 2
    assert "other:key" in r.store  # інвалідація б'є лише semcache:*
    assert await sc.get(_unit(1.0, 0.0)) is None

"""Тести embeddings-бекенда БЕЗ мережі.

Voyage-клієнт мокаємо детерміновано (фейкові вектори), тож CI зелений і без
`VOYAGE_API_KEY`. Перевіряємо: контракт результату збігається з TF-IDF,
семантичний скоринг ранжує правильно, дисковий кеш не ходить у мережу двічі,
а RagIndex деградує на TF-IDF, коли Voyage падає.
"""

import os

import pytest

import rag_index
from ad_embeddings import EmbeddingsError, VoyageBackend, VoyageClient


class FakeVoyageClient:
    """Детермінований бекенд-без-мережі: bag-of-words → фіксований вектор.

    Рахує, скільки разів реально «ходив у мережу», щоб перевірити кеш.
    """

    model = "fake-model"
    dim = 8

    _VOCAB = ["can", "шина", "камера", "тепловізор", "живлення",
              "gnss", "навігація", "політ"]

    def __init__(self):
        self.calls = 0

    def _vec(self, text: str) -> list[float]:
        t = text.lower()
        raw = [float(t.count(w)) for w in self._VOCAB]
        norm = sum(x * x for x in raw) ** 0.5 or 1.0
        return [x / norm for x in raw]

    async def embed(self, texts, input_type):
        self.calls += 1
        return [self._vec(t) for t in texts]


def _doc(doc_id, title, text, source="db"):
    return rag_index._Doc(doc_id=doc_id, source=source, title=title, text=text)


CORPUS = [
    _doc("db:product:fieldsense-can", "FieldSense CAN", "датчик по CAN шина"),
    _doc("db:product:thermix", "ThermIX", "камера тепловізор нічна зйомка"),
    _doc("db:product:geonav", "GeoNav Dual", "gnss навігація політ"),
]


async def test_voyage_backend_ranks_semantically(tmp_path, monkeypatch):
    monkeypatch.setattr("ad_embeddings.CACHE_FILE", tmp_path / "emb.json")
    backend = VoyageBackend(client=FakeVoyageClient())
    await backend.build(CORPUS)

    scored = await backend.scores("камера тепловізор")
    scored.sort(key=lambda x: x[0], reverse=True)
    assert scored, "мали б бути влучання"
    assert scored[0][1].doc_id == "db:product:thermix"


async def test_result_contract_matches_tfidf(tmp_path, monkeypatch):
    """search() повертає той самий набір полів для обох бекендів."""
    monkeypatch.setattr("ad_embeddings.CACHE_FILE", tmp_path / "emb.json")
    idx = rag_index.RagIndex()
    idx.backend = VoyageBackend(client=FakeVoyageClient())
    await idx.backend.build(CORPUS)
    idx.docs = CORPUS
    idx.ready = True

    results = await idx.search("камера тепловізор", k=2)
    assert results
    assert {"doc_id", "source", "title", "score", "snippet"} <= set(results[0])


async def test_disk_cache_avoids_second_network_call(tmp_path, monkeypatch):
    monkeypatch.setattr("ad_embeddings.CACHE_FILE", tmp_path / "emb.json")
    client = FakeVoyageClient()

    b1 = VoyageBackend(client=client)
    await b1.build(CORPUS)
    calls_after_first = client.calls
    assert calls_after_first >= 1

    # другий build того самого корпусу — усе з кешу, жодного мережевого виклику
    b2 = VoyageBackend(client=client)
    await b2.build(CORPUS)
    assert client.calls == calls_after_first


async def test_missing_key_raises(monkeypatch):
    # config — frozen dataclass; підміняємо посилання на нього в ad_embeddings
    from types import SimpleNamespace
    monkeypatch.setattr(
        "ad_embeddings.config",
        SimpleNamespace(voyage_api_key=None, embed_model="x", embed_dim=8),
    )
    with pytest.raises(EmbeddingsError):
        VoyageBackend()  # без ключа й без клієнта — fail-safe


async def test_ragindex_falls_back_to_tfidf_on_voyage_failure(monkeypatch):
    """Якщо voyage-бекенд падає на build — індекс деградує на TF-IDF,
    а не валиться (fail-open на читання)."""
    class Boom:
        name = "voyage"
        async def build(self, docs):
            raise EmbeddingsError("simulated outage")
        async def scores(self, q):  # pragma: no cover
            return []
        def extra_status(self):  # pragma: no cover
            return {}

    monkeypatch.setattr(rag_index, "_make_backend", lambda name: Boom())

    async def fake_query(sql, params=()):
        return []  # порожня БД: корпус = лише файли knowledge/

    idx = rag_index.RagIndex()
    st = await idx.build(fake_query)
    assert st["ready"] is True
    assert st["backend"] == "tfidf"  # деградували, а не впали


# ── Опційний інтеграційний тест: реальний Voyage API ────────────────────
@pytest.mark.skipif(
    not os.getenv("VOYAGE_API_KEY"),
    reason="live Voyage test needs VOYAGE_API_KEY",
)
async def test_voyage_live_smoke(tmp_path, monkeypatch):
    """Живий виклик: два семантично близькі укр-запити мають дати
    вищий косинус, ніж далекий. Пропускається без ключа."""
    monkeypatch.setattr("ad_embeddings.CACHE_FILE", tmp_path / "emb.json")
    from ad_config import config
    from ad_embeddings import _dot
    client = VoyageClient(
        api_key=config.voyage_api_key,
        model=config.embed_model,
        dim=config.embed_dim,
    )
    vecs = await client.embed(
        ["тепловізійна камера", "камера з тепловізором", "модуль живлення"],
        input_type="query",
    )
    assert _dot(vecs[0], vecs[1]) > _dot(vecs[0], vecs[2])

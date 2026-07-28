"""Тести Qdrant-бекенда БЕЗ мережі.

httpx і клієнт embeddings мокаємо, тож CI зелений без Qdrant і без ключа.
Перевіряємо: build робить upsert, scores парсить відповідь Qdrant у _Doc,
а RagIndex деградує на TF-IDF, коли Qdrant недоступний.
"""

import pytest

import ad_qdrant
import rag_index
from ad_qdrant import QdrantBackend
from voyage_client import EmbeddingsError


class FakeEmbedClient:
    """Детермінований embed-клієнт (без Voyage): bag-of-words → вектор."""
    _VOCAB = ["can", "камера", "тепловізор", "живлення"]

    async def embed(self, texts, input_type):
        out = []
        for t in texts:
            tl = t.lower()
            raw = [float(tl.count(w)) for w in self._VOCAB]
            norm = sum(x * x for x in raw) ** 0.5 or 1.0
            out.append([x / norm for x in raw])
        return out


class FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload


class FakeHttp:
    """Мінімальний async httpx.AsyncClient: запамʼятовує виклики, віддає задане."""
    def __init__(self, search_result=None, record=None):
        self._search = search_result or []
        self._rec = record if record is not None else {}

    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def delete(self, url):
        self._rec.setdefault("delete", []).append(url)
        return FakeResp(200)

    async def put(self, url, json=None):
        self._rec.setdefault("put", []).append((url, json))
        return FakeResp(200, {"result": True})

    async def post(self, url, json=None):
        self._rec.setdefault("post", []).append((url, json))
        return FakeResp(200, {"result": self._search})


def _doc(doc_id, title, text):
    return rag_index._Doc(doc_id=doc_id, source="db", title=title, text=text)


CORPUS = [
    _doc("db:product:thermix", "ThermIX", "камера тепловізор"),
    _doc("db:product:fieldsense-can", "FieldSense CAN", "датчик can шина"),
]


async def test_build_upserts_points(monkeypatch):
    rec = {}
    monkeypatch.setattr(ad_qdrant.httpx, "AsyncClient",
                        lambda *a, **k: FakeHttp(record=rec))
    b = QdrantBackend(embed_client=FakeEmbedClient(), base_url="http://qdrant:6333")
    await b.build(CORPUS)

    # створення колекції (PUT) + upsert точок (PUT ?points)
    puts = rec["put"]
    assert any("/collections/aerodefences_rag" in u for u, _ in puts)
    upsert = [j for u, j in puts if "points" in u][0]
    assert len(upsert["points"]) == 2
    assert upsert["points"][0]["payload"]["doc_id"] == "db:product:thermix"
    assert b.extra_status()["vector_db"] == "qdrant"
    assert b.extra_status()["points"] == 2


async def test_scores_parses_qdrant_hits(monkeypatch):
    search_result = [
        {"score": 0.97, "payload": {"doc_id": "db:product:thermix", "source": "db",
                                    "title": "ThermIX", "text": "камера тепловізор"}},
    ]
    monkeypatch.setattr(ad_qdrant.httpx, "AsyncClient",
                        lambda *a, **k: FakeHttp(search_result=search_result))
    b = QdrantBackend(embed_client=FakeEmbedClient(), base_url="http://qdrant:6333")
    scored = await b.scores("камера тепловізор")
    assert scored and scored[0][0] == 0.97
    assert scored[0][1].doc_id == "db:product:thermix"


async def test_upsert_and_remove_by_stable_id(monkeypatch):
    from ad_qdrant import _pid
    rec = {}
    monkeypatch.setattr(ad_qdrant.httpx, "AsyncClient",
                        lambda *a, **k: FakeHttp(record=rec))
    b = QdrantBackend(embed_client=FakeEmbedClient(), base_url="http://qdrant:6333")

    await b.upsert([_doc("db:product:thermix", "ThermIX", "оптична камера")])
    upsert = [j for u, j in rec["put"] if "points" in u][0]
    # точка має СТАБІЛЬНИЙ id (з doc_id), не позиційний
    assert upsert["points"][0]["id"] == _pid("db:product:thermix")

    await b.remove(["db:product:thermix"])
    delete = rec["post"][0][1]
    assert delete["points"] == [_pid("db:product:thermix")]


def test_pid_is_stable_and_uint():
    from ad_qdrant import _pid
    a = _pid("db:product:thermix")
    assert a == _pid("db:product:thermix")     # детермінований
    assert a != _pid("db:product:fieldsense")  # різні doc_id → різні id
    assert isinstance(a, int) and a >= 0


async def test_missing_url_raises(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr("ad_qdrant.config", SimpleNamespace(qdrant_url=None, embed_dim=4))
    with pytest.raises(EmbeddingsError):
        QdrantBackend(embed_client=FakeEmbedClient())


async def test_ragindex_falls_back_when_qdrant_down(monkeypatch):
    """Qdrant недоступний на build → індекс деградує на TF-IDF (fail-open)."""
    class Boom:
        name = "qdrant"
        async def build(self, docs):
            raise EmbeddingsError("qdrant http 502")
        async def scores(self, q, query_vec=None):  # pragma: no cover
            return []
        def extra_status(self):  # pragma: no cover
            return {}

    monkeypatch.setattr(rag_index, "_make_backend", lambda name: Boom())

    async def fake_query(sql, params=()):
        return []

    idx = rag_index.RagIndex()
    st = await idx.build(fake_query)
    assert st["ready"] is True
    assert st["backend"] == "tfidf"

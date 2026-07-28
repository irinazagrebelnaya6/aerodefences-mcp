"""Тести інкрементального reindex (точкове оновлення/видалення без rebuild)."""

import rag_index
from rag_index import RagIndex, TfidfBackend, _Doc


def _doc(slug, text):
    return _Doc(f"db:product:{slug}", "db", slug, text)


CORPUS = [
    _doc("thermix", "камера тепловізор нічна зйомка"),
    _doc("fieldsense", "датчик can шина"),
]


async def _ready_index():
    b = TfidfBackend()
    await b.build(CORPUS)
    idx = RagIndex()
    idx.backend = b
    idx.docs = list(CORPUS)
    idx.ready = True
    idx._recount()
    return idx


# ── бекенд-рівень: TfidfBackend.upsert / remove ─────────────────────────
async def test_tfidf_upsert_replaces_existing():
    b = TfidfBackend()
    await b.build(CORPUS)
    await b.upsert([_doc("thermix", "оптична камера високої роздільної здатності")])
    assert len(b.docs) == 2  # не додало дубль
    thermix = next(d for d in b.docs if d.doc_id == "db:product:thermix")
    assert "оптична" in thermix.text


async def test_tfidf_upsert_appends_new_and_remove():
    b = TfidfBackend()
    await b.build(CORPUS)
    await b.upsert([_doc("newprod", "новий товар gnss навігація")])
    assert len(b.docs) == 3
    await b.remove(["db:product:newprod"])
    assert len(b.docs) == 2
    assert all(d.doc_id != "db:product:newprod" for d in b.docs)


# ── RagIndex.reindex_product ────────────────────────────────────────────
async def test_reindex_updates_doc(monkeypatch):
    idx = await _ready_index()
    updated = _doc("thermix", "додано нову характеристику магнітометр компас")

    async def fake_collect(query, slug):
        return updated if slug == "thermix" else None
    monkeypatch.setattr(rag_index, "collect_product", fake_collect)

    res = await idx.reindex_product("thermix", query=None)
    assert res["reindexed"] is True and res["removed"] is False
    # пошук бачить оновлений текст
    hits = await idx.search("магнітометр компас", k=2)
    assert hits and hits[0]["doc_id"] == "db:product:thermix"


async def test_reindex_removes_when_gone(monkeypatch):
    idx = await _ready_index()

    async def fake_collect(query, slug):
        return None  # продукт зник із БД
    monkeypatch.setattr(rag_index, "collect_product", fake_collect)

    res = await idx.reindex_product("thermix", query=None)
    assert res["removed"] is True
    assert all(d.doc_id != "db:product:thermix" for d in idx.docs)
    assert idx.status()["documents"] == 1


async def test_reindex_cold_index_noop():
    idx = RagIndex()  # не побудований
    res = await idx.reindex_product("thermix", query=None)
    assert res["reindexed"] is False

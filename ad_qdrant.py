"""
Qdrant vector-DB бекенд для RAG (мікросервісний компонент «vector database»).

На відміну від `TfidfBackend`/`VoyageBackend` (косинус у памʼяті процесу),
цей бекенд зберігає вектори в ОКРЕМІЙ vector-DB (контейнер Qdrant) і робить
ANN-пошук на її боці. Для нашого масштабу (~80 документів) це надлишково, але
демонструє повноцінну мікросервісну архітектуру: retrieval винесено у
спеціалізоване сховище.

Свідомо БЕЗ пакета `qdrant-client` — через REST по httpx (філософія проєкту
«мінімум залежностей», як із Voyage). Embeddings бере той самий клієнт, що й
VoyageBackend (`make_embeddings_client`): прямий Voyage або sidecar.
"""

from __future__ import annotations

import httpx

from ad_config import config, log
from ad_embeddings import make_embeddings_client
from rag_index import _Doc
from voyage_client import EmbeddingsError

COLLECTION = "aerodefences_rag"
# Скільки кандидатів тягнемо з Qdrant; RagIndex.search вже ріже до потрібного k.
SEARCH_LIMIT = 25


class QdrantBackend:
    """Реалізує протокол SearchBackend (rag_index.py) поверх Qdrant REST."""

    name = "qdrant"

    def __init__(self, embed_client=None, base_url: str | None = None) -> None:
        url = (base_url or config.qdrant_url or "").rstrip("/")
        if not url:
            raise EmbeddingsError("ADD_RAG_BACKEND=qdrant вимагає ADD_QDRANT_URL")
        self._url = url
        # той самий вибір джерела embeddings, що й у VoyageBackend
        self.client = embed_client if embed_client is not None else make_embeddings_client()
        self.dim = config.embed_dim
        self._points = 0

    async def build(self, docs: list[_Doc]) -> None:
        vectors = await self.client.embed([d.index_text() for d in docs], "document")
        async with httpx.AsyncClient(timeout=30.0) as h:
            # Ідемпотентно: перестворюємо колекцію (rebuild замінює вміст).
            await h.delete(f"{self._url}/collections/{COLLECTION}")
            r = await h.put(
                f"{self._url}/collections/{COLLECTION}",
                json={"vectors": {"size": self.dim, "distance": "Cosine"}},
            )
            _raise(r)
            points = [
                {
                    "id": i,
                    "vector": vec,
                    "payload": {
                        "doc_id": d.doc_id, "source": d.source,
                        "title": d.title, "text": d.text,
                    },
                }
                for i, (d, vec) in enumerate(zip(docs, vectors))
            ]
            r = await h.put(
                f"{self._url}/collections/{COLLECTION}/points?wait=true",
                json={"points": points},
            )
            _raise(r)
        self._points = len(docs)
        log.info("qdrant: upserted %d points into '%s'", self._points, COLLECTION)

    async def scores(
        self, question: str, query_vec: list[float] | None = None
    ) -> list[tuple[float, _Doc]]:
        q = query_vec if query_vec is not None else await self.embed_query(question)
        async with httpx.AsyncClient(timeout=15.0) as h:
            r = await h.post(
                f"{self._url}/collections/{COLLECTION}/points/search",
                json={"vector": q, "limit": SEARCH_LIMIT, "with_payload": True},
            )
        _raise(r)
        out: list[tuple[float, _Doc]] = []
        for hit in r.json().get("result", []):
            p = hit["payload"]
            out.append((
                hit["score"],
                _Doc(p["doc_id"], p["source"], p["title"], p["text"]),
            ))
        return out

    async def embed_query(self, question: str) -> list[float]:
        """Вектор запиту — його ж переиспользує semantic cache (без 2-го виклику)."""
        return (await self.client.embed([question], "query"))[0]

    def extra_status(self) -> dict:
        return {"vector_db": "qdrant", "collection": COLLECTION,
                "points": self._points, "dim": self.dim}


def _raise(resp: httpx.Response) -> None:
    """Помилку Qdrant піднімаємо як EmbeddingsError → RagIndex деградує на TF-IDF."""
    if resp.status_code >= 300:
        raise EmbeddingsError(f"qdrant http {resp.status_code}")

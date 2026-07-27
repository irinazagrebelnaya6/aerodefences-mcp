"""
Локальний RAG-шар над каталогом `aerodefences`.

Ідея: MCP-сервер не «генерує» відповідь сам — він грає роль RETRIEVER'а.
Він збирає корпус із ДВОХ джерел:
  1) База даних MySQL (products + specs + faqs + use_cases) — динамічні дані;
  2) Локальні файли `knowledge/*.md` — статичні політики/глосарій/regламент.
далі індексує їх і на запит повертає top-k релевантних фрагментів. Генерацію
(звʼязний текст відповіді) робить сама LLM-хост, спираючись ВИКЛЮЧНО на
повернені фрагменти (grounding).

Архітектура (див. RAG_EMBEDDINGS_PLAN.md):
  - збір корпусу (`collect_corpus`) — спільний для всіх бекендів;
  - бекенди пошуку взаємозамінні (`ADD_RAG_BACKEND`):
      * `TfidfBackend`  — TF-IDF + косинус, чистий Python, офлайн, дефолт;
      * `VoyageBackend` — семантичні embeddings через Voyage AI API
        (див. `ad_embeddings.py`), з fallback на TF-IDF при недоступності;
  - `RagIndex` — тонкий фасад над обраним бекендом.
"""

from __future__ import annotations

import hashlib
import math
import pathlib
import re
from collections import Counter
from typing import Awaitable, Callable, Protocol

from ad_config import config, log

KNOWLEDGE_DIR = pathlib.Path(__file__).parent / "knowledge"

# Файл синонімів — це КОНФІГ, а не knowledge-документ для цитування.
# Його не індексуємо як звичайний файл; замість цього вписуємо його правила
# в документи товарів (див. _load_synonyms / collect_corpus).
SYNONYMS_FILE = "synonyms.md"


def _load_synonyms() -> list[tuple[list[str], str]]:
    """Читає knowledge/synonyms.md → список (тригери, укр-синоніми).

    Рядок правила: `тригер1, тригер2 => синоніми`. Порожній файл/відсутній —
    повертає []. RAG працює й без синонімів (тоді просто без збагачення).
    """
    path = KNOWLEDGE_DIR / SYNONYMS_FILE
    if not path.exists():
        return []
    rules: list[tuple[list[str], str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=>" not in line:
            continue
        left, right = line.split("=>", 1)
        triggers = [t.strip().lower() for t in left.split(",") if t.strip()]
        synonyms = right.strip()
        if triggers and synonyms:
            rules.append((triggers, synonyms))
    return rules

# Токен: латиниця, цифри та кирилиця (укр. літери включно).
_TOKEN_RE = re.compile(r"[0-9A-Za-zА-Яа-яІіЇїЄєҐґ]+")

# Дуже короткий стоп-лист (укр/анг) — прибирає шум, лишає суть.
_STOP = {
    "the", "and", "for", "with", "that", "this", "are", "was", "you", "our",
    "від", "для", "що", "як", "чи", "це", "той", "які", "при", "над", "про",
    "the", "a", "an", "of", "to", "in", "is", "it", "on", "or",
    "і", "та", "в", "на", "з", "до", "по", "за", "у", "а", "не", "є",
}


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "") if t.lower() not in _STOP]


class _Doc:
    """Один проіндексований фрагмент корпусу."""

    __slots__ = ("doc_id", "source", "title", "text")

    def __init__(self, doc_id: str, source: str, title: str, text: str):
        self.doc_id = doc_id      # напр. "db:product:skymodule-x1"
        self.source = source      # "db" | "file"
        self.title = title
        self.text = text

    def index_text(self) -> str:
        """Текст, що йде в індекс (заголовок теж несе сигнал)."""
        return f"{self.title} {self.text}"

    def content_hash(self) -> str:
        """Стабільний ключ вмісту — для кешу ембедінгів (Фаза C)."""
        return hashlib.sha256(self.index_text().encode("utf-8")).hexdigest()


# ────────────────────────────────────────────────────────────────────────
# Збір корпусу — СПІЛЬНИЙ для всіх бекендів (БД + файли knowledge/).
# ────────────────────────────────────────────────────────────────────────
async def collect_corpus(
    query: Callable[..., Awaitable[list[dict]]]
) -> list[_Doc]:
    """Зібрати корпус: продукти з БД + секції knowledge/*.md.

    `query` — та сама async-функція read-доступу з сервера, тож RAG
    не дублює конфіг БД і ходить у неї єдиним шляхом.
    """
    docs: list[_Doc] = []
    docs.extend(await _collect_db(query))
    docs.extend(_collect_files())
    return docs


async def _collect_db(
    query: Callable[..., Awaitable[list[dict]]]
) -> list[_Doc]:
    """Один документ на продукт: назва + описи + специфікації + FAQ +
    категорія + вписані укр-синоніми (щоб англомовні описи знаходились за
    українськими запитами)."""
    synonyms = _load_synonyms()
    products = await query(
        "SELECT p.id, p.slug, p.name, p.subtitle, p.short_description, "
        "p.long_description, p.key_advantage, "
        "c.slug AS cat_slug, c.name AS cat_name "
        "FROM products p LEFT JOIN categories c ON c.id = p.category_id"
    )
    out: list[_Doc] = []
    for p in products:
        pid = p["id"]
        specs = await query(
            "SELECT spec_name, spec_value FROM product_specs WHERE product_id=%s",
            (pid,),
        )
        faqs = await query(
            "SELECT question, answer FROM product_faqs WHERE product_id=%s",
            (pid,),
        )
        parts = [
            p.get("cat_name") or "",
            p.get("subtitle") or "",
            p.get("key_advantage") or "",
            p.get("short_description") or "",
            p.get("long_description") or "",
            " ".join(f"{s['spec_name']}: {s['spec_value']}" for s in specs),
            " ".join(f"{f['question']} {f['answer']}" for f in faqs),
        ]
        text = "\n".join(x for x in parts if x)

        # ── збагачення синонімами ──
        # haystack = категорія + текст; якщо тригер правила знайдено,
        # додаємо укр-синоніми у ПОШУКОВИЙ текст (у snippet вони не лізуть,
        # бо додаємо в кінець, а snippet береться з початку).
        hay = f"{p.get('cat_slug') or ''} {text}".lower()
        hay_tokens = set(_tokenize(hay))
        extra: list[str] = []
        for triggers, syn in synonyms:
            for trg in triggers:
                hit = (trg in hay) if " " in trg else (trg in hay_tokens)
                if hit:
                    extra.append(syn)
                    break
        if extra:
            text = text + "\n[синоніми] " + " ".join(extra)

        out.append(
            _Doc(
                doc_id=f"db:product:{p['slug']}",
                source="db",
                title=p["name"],
                text=text,
            )
        )
    return out


def _collect_files() -> list[_Doc]:
    """Локальні файли знань: кожен .md ріжемо на секції за заголовками '## '."""
    out: list[_Doc] = []
    if not KNOWLEDGE_DIR.exists():
        return out
    for path in sorted(KNOWLEDGE_DIR.glob("*.md")):
        if path.name == SYNONYMS_FILE:
            continue  # конфіг синонімів не цитуємо як knowledge
        text = path.read_text(encoding="utf-8")
        # розбиваємо за заголовками рівня 2
        chunks = re.split(r"(?m)^##\s+", text)
        base = path.stem
        for i, chunk in enumerate(chunks):
            chunk = chunk.strip()
            if not chunk:
                continue
            title_line, _, body = chunk.partition("\n")
            out.append(
                _Doc(
                    doc_id=f"file:{base}#{i}",
                    source="file",
                    title=f"{base} — {title_line.strip('# ').strip()}",
                    text=body.strip() or title_line,
                )
            )
    return out


# ────────────────────────────────────────────────────────────────────────
# Бекенди пошуку (взаємозамінні; обираються через ADD_RAG_BACKEND).
# ────────────────────────────────────────────────────────────────────────
class SearchBackend(Protocol):
    """Контракт бекенда: збудуватись із корпусу та скорити запит."""

    name: str

    async def build(self, docs: list[_Doc]) -> None: ...
    async def scores(self, question: str) -> list[tuple[float, _Doc]]: ...
    def extra_status(self) -> dict: ...


class TfidfBackend:
    """TF-IDF + косинус: чистий Python, детерміновано, працює офлайн.

    Дефолтний бекенд і fallback для `voyage` (див. RagIndex.build).
    """

    name = "tfidf"

    def __init__(self) -> None:
        self.docs: list[_Doc] = []
        self.idf: dict[str, float] = {}
        self._vecs: list[dict[str, float]] = []

    async def build(self, docs: list[_Doc]) -> None:
        # IDF по всьому корпусу
        n = len(docs) or 1
        df: Counter[str] = Counter()
        tokenized: list[list[str]] = []
        for d in docs:
            toks = _tokenize(d.index_text())
            tokenized.append(toks)
            df.update(set(toks))
        self.idf = {t: math.log((1 + n) / (1 + c)) + 1.0 for t, c in df.items()}

        # TF-IDF вектор кожного документа (нормований)
        self._vecs = [self._vectorize(toks) for toks in tokenized]
        self.docs = docs

    def _vectorize(self, toks: list[str]) -> dict[str, float]:
        if not toks:
            return {}
        tf = Counter(toks)
        max_tf = max(tf.values())
        vec = {
            t: (0.5 + 0.5 * c / max_tf) * self.idf.get(t, 1.0)
            for t, c in tf.items()
        }
        norm = math.sqrt(sum(w * w for w in vec.values())) or 1.0
        return {t: w / norm for t, w in vec.items()}

    async def scores(self, question: str) -> list[tuple[float, _Doc]]:
        q_vec = self._vectorize(_tokenize(question))
        if not q_vec:
            return []
        scored: list[tuple[float, _Doc]] = []
        for d, vec in zip(self.docs, self._vecs):
            # косинус: обидва вектори вже нормовані → просто скалярний добуток
            common = set(q_vec) & set(vec)
            score = sum(q_vec[t] * vec[t] for t in common)
            if score > 0:
                scored.append((score, d))
        return scored

    def extra_status(self) -> dict:
        return {"vocabulary": len(self.idf)}


def _make_backend(name: str) -> SearchBackend:
    """Фабрика бекендів. Імпорт voyage — лінивий, щоб офлайн-шлях
    не тягнув httpx-клієнт і не вимагав ключа."""
    if name == "voyage":
        from ad_embeddings import VoyageBackend
        return VoyageBackend()
    return TfidfBackend()


class RagIndex:
    """Фасад над обраним бекендом. Публічний контракт незмінний:
    build(query) / search(question, k) / status()."""

    def __init__(self) -> None:
        self.docs: list[_Doc] = []
        self.ready = False
        self.sources: dict[str, int] = {"db": 0, "file": 0}
        self.backend: SearchBackend = TfidfBackend()

    # ---- побудова ----
    async def build(self, query: Callable[..., Awaitable[list[dict]]]) -> dict:
        docs = await collect_corpus(query)

        backend = _make_backend(config.rag_backend)
        try:
            await backend.build(docs)
        except Exception as e:  # noqa: BLE001 — деградуємо, а не падаємо
            if backend.name == "tfidf":
                raise  # tfidf не має чого «деградувати» — це справжня помилка
            # Fail-open на читання: embeddings недоступні → TF-IDF,
            # сервер лишається живим (див. план §3.B.3).
            log.warning(
                "RAG backend '%s' build failed (%s) -> falling back to tfidf",
                backend.name, type(e).__name__,
            )
            backend = TfidfBackend()
            await backend.build(docs)

        self.backend = backend
        self.docs = docs
        self.sources = {
            "db": sum(1 for d in docs if d.source == "db"),
            "file": sum(1 for d in docs if d.source == "file"),
        }
        self.ready = True
        return self.status()

    # ---- пошук ----
    async def search(self, question: str, k: int = 5) -> list[dict]:
        if not self.ready:
            raise RuntimeError("RAG-індекс не побудовано. Виклич rebuild_rag_index().")
        scored = await self.backend.scores(question)
        scored.sort(key=lambda x: x[0], reverse=True)
        results = []
        for score, d in scored[:k]:
            snippet = d.text if len(d.text) <= 600 else d.text[:600] + "…"
            results.append(
                {
                    "doc_id": d.doc_id,
                    "source": d.source,
                    "title": d.title,
                    "score": round(score, 4),
                    "snippet": snippet,
                }
            )
        return results

    def status(self) -> dict:
        return {
            "ready": self.ready,
            "backend": self.backend.name,
            "documents": len(self.docs),
            "sources": self.sources,
            **self.backend.extra_status(),
            "knowledge_dir": str(KNOWLEDGE_DIR),
        }


# Єдиний екземпляр індексу на процес.
INDEX = RagIndex()

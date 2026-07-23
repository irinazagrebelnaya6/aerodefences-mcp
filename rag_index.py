"""
Локальний RAG-шар над каталогом `aerodefences`.

Ідея: MCP-сервер не «генерує» відповідь сам — він грає роль RETRIEVER'а.
Він збирає корпус із ДВОХ джерел:
  1) База даних MySQL (products + specs + faqs + use_cases) — динамічні дані;
  2) Локальні файли `knowledge/*.md` — статичні політики/глосарій/regламент.
далі індексує їх у пам'яті (TF-IDF) і на запит повертає top-k релевантних
фрагментів. Генерацію (звʼязний текст відповіді) робить сама LLM-хост,
спираючись ВИКЛЮЧНО на повернені фрагменти (grounding).

Навмисно БЕЗ важких залежностей (numpy / sentence-transformers): чистий
Python, детермінований, працює офлайн і однаково в CI. Для навчального
каталогу (~15 продуктів, 65 FAQ, кілька md-файлів) TF-IDF + косинус —
цілком достатньо і прозоро для захисту.
"""

from __future__ import annotations

import math
import pathlib
import re
from collections import Counter
from typing import Awaitable, Callable

KNOWLEDGE_DIR = pathlib.Path(__file__).parent / "knowledge"

# Файл синонімів — це КОНФІГ, а не knowledge-документ для цитування.
# Його не індексуємо як звичайний файл; замість цього вписуємо його правила
# в документи товарів (див. _load_synonyms / _collect_db).
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

    __slots__ = ("doc_id", "source", "title", "text", "vec")

    def __init__(self, doc_id: str, source: str, title: str, text: str):
        self.doc_id = doc_id      # напр. "db:product:skymodule-x1"
        self.source = source      # "db" | "file"
        self.title = title
        self.text = text
        self.vec: dict[str, float] = {}


class RagIndex:
    """In-memory TF-IDF індекс. Будується з БД + локальних файлів."""

    def __init__(self) -> None:
        self.docs: list[_Doc] = []
        self.idf: dict[str, float] = {}
        self.ready = False
        self.sources: dict[str, int] = {"db": 0, "file": 0}

    # ---- побудова ----
    async def build(self, query: Callable[..., Awaitable[list[dict]]]) -> dict:
        """Зібрати корпус і порахувати TF-IDF.

        `query` — та сама async-функція read-доступу з сервера, тож RAG
        не дублює конфіг БД і ходить у неї єдиним шляхом.
        """
        docs: list[_Doc] = []
        docs.extend(await self._collect_db(query))
        docs.extend(self._collect_files())

        # IDF по всьому корпусу
        n = len(docs) or 1
        df: Counter[str] = Counter()
        tokenized: list[list[str]] = []
        for d in docs:
            toks = _tokenize(f"{d.title} {d.text}")
            tokenized.append(toks)
            df.update(set(toks))
        self.idf = {t: math.log((1 + n) / (1 + c)) + 1.0 for t, c in df.items()}

        # TF-IDF вектор кожного документа (нормований)
        for d, toks in zip(docs, tokenized):
            d.vec = self._vectorize(toks)

        self.docs = docs
        self.sources = {
            "db": sum(1 for d in docs if d.source == "db"),
            "file": sum(1 for d in docs if d.source == "file"),
        }
        self.ready = True
        return self.status()

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

    async def _collect_db(
        self, query: Callable[..., Awaitable[list[dict]]]
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

    def _collect_files(self) -> list[_Doc]:
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

    # ---- пошук ----
    def search(self, question: str, k: int = 5) -> list[dict]:
        if not self.ready:
            raise RuntimeError("RAG-індекс не побудовано. Виклич rebuild_rag_index().")
        q_vec = self._vectorize(_tokenize(question))
        if not q_vec:
            return []
        scored: list[tuple[float, _Doc]] = []
        for d in self.docs:
            # косинус: обидва вектори вже нормовані → просто скалярний добуток
            common = set(q_vec) & set(d.vec)
            score = sum(q_vec[t] * d.vec[t] for t in common)
            if score > 0:
                scored.append((score, d))
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
            "documents": len(self.docs),
            "sources": self.sources,
            "vocabulary": len(self.idf),
            "knowledge_dir": str(KNOWLEDGE_DIR),
        }


# Єдиний екземпляр індексу на процес.
INDEX = RagIndex()

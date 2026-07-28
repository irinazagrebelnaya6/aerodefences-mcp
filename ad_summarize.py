"""
In-memory summarization шар: стискає top-k RAG-фрагменти перед віддачею LLM
(економія контексту/токенів).

Екстрактивний, БЕЗ виклику LLM — сервер лишається retriever'ом: у кожному
сніпеті лишаємо найрелевантніші до запиту речення в межах бюджету символів.
Результати кешуються в памʼяті процесу (LRU) — повторний однаковий запит не
пересумаризовує. Вмикається `ADD_SUMMARIZE=on`.

(Якщо згодом захочеться абстрактивної суммаризації — цей самий інтерфейс
`summarize_results` можна перевести на виклик LLM, не чіпаючи `ask_catalog`.)
"""

from __future__ import annotations

import hashlib
import re
from collections import OrderedDict

from ad_config import config
from rag_index import _tokenize

# Речення: послідовність символів до термінатора (крапка/!/?/новий рядок).
_SENT_RE = re.compile(r"[^.!?\n]+[.!?]?")

# LRU-кеш стиснень у памʼяті процесу.
_CACHE: "OrderedDict[str, list[dict]]" = OrderedDict()
_CACHE_MAX = 256


def _summarize_snippet(question: str, text: str, max_chars: int) -> str:
    """Екстрактивно стиснути один сніпет до найрелевантніших речень."""
    if len(text) <= max_chars:
        return text
    qtok = set(_tokenize(question))
    sents = [s.strip() for s in _SENT_RE.findall(text) if s.strip()]
    if not sents:
        return text[:max_chars].rstrip() + "…"

    # оцінка речення = перетин токенів із запитом; вибираємо найкращі в межах
    # бюджету, потім відновлюємо початковий порядок для читабельності
    ranked = sorted(
        ((len(qtok & set(_tokenize(s))), i, s) for i, s in enumerate(sents)),
        key=lambda t: (-t[0], t[1]),
    )
    chosen: list[tuple[int, str]] = []
    total = 0
    for _overlap, i, s in ranked:
        if chosen and total + len(s) > max_chars:
            break
        chosen.append((i, s))
        total += len(s) + 1
    chosen.sort()
    out = " ".join(s for _, s in chosen)
    if len(out) > max_chars:
        out = out[:max_chars].rstrip() + "…"
    return out or (text[:max_chars].rstrip() + "…")


def summarize_results(question: str, results: list[dict]) -> list[dict]:
    """Стиснути `snippet` кожного результату (no-op, якщо вимкнено).

    Не змінює doc_id/source/title/score — лише скорочує `snippet`; додає
    `snippet_full_len` для прозорості (скільки символів було).
    """
    if not config.summarize or not results:
        return results

    key = hashlib.sha256(
        (question + "|" + "|".join(
            f"{r['doc_id']}:{len(r.get('snippet', ''))}" for r in results
        )).encode("utf-8")
    ).hexdigest()
    if key in _CACHE:
        _CACHE.move_to_end(key)
        return _CACHE[key]

    maxc = config.summarize_max_chars
    out = []
    for r in results:
        snippet = r.get("snippet", "")
        compressed = _summarize_snippet(question, snippet, maxc)
        item = {**r, "snippet": compressed}
        if len(compressed) < len(snippet):
            item["snippet_full_len"] = len(snippet)
        out.append(item)

    _CACHE[key] = out
    if len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)
    return out

"""Тести in-memory summarization (екстрактивна, без мережі/LLM)."""

from types import SimpleNamespace

import ad_summarize
from ad_summarize import summarize_results


def _cfg(monkeypatch, on=True, maxc=80):
    monkeypatch.setattr(
        "ad_summarize.config",
        SimpleNamespace(summarize=on, summarize_max_chars=maxc),
    )
    ad_summarize._CACHE.clear()


LONG = (
    "Цей модуль керує польотом дрона. "
    "Він має вбудований магнітометр і барометр. "
    "Живлення подається окремим роз'ємом. "
    "Гарантія становить два роки."
)
RESULTS = [{"doc_id": "db:x", "source": "db", "title": "X", "score": 1.0, "snippet": LONG}]


def test_noop_when_disabled(monkeypatch):
    _cfg(monkeypatch, on=False)
    out = summarize_results("керування польотом", RESULTS)
    assert out is RESULTS  # без змін


def test_compresses_and_keeps_relevant(monkeypatch):
    _cfg(monkeypatch, on=True, maxc=80)
    out = summarize_results("керування польотом", RESULTS)
    snip = out[0]["snippet"]
    assert len(snip) <= len(LONG)
    assert "польот" in snip.lower()          # релевантне речення збережене
    assert out[0]["snippet_full_len"] == len(LONG)  # прозорість: скільки було


def test_respects_char_budget(monkeypatch):
    _cfg(monkeypatch, on=True, maxc=50)
    out = summarize_results("живлення", RESULTS)
    # бюджет мʼякий (ціле речення), але не має бути близько повного тексту
    assert len(out[0]["snippet"]) < len(LONG)


def test_short_snippet_untouched(monkeypatch):
    _cfg(monkeypatch, on=True, maxc=300)
    short = [{"doc_id": "db:y", "source": "db", "title": "Y", "score": 1.0,
              "snippet": "Короткий опис."}]
    out = summarize_results("опис", short)
    assert out[0]["snippet"] == "Короткий опис."
    assert "snippet_full_len" not in out[0]  # не стискали


def test_cache_reuse(monkeypatch):
    _cfg(monkeypatch, on=True, maxc=80)
    out1 = summarize_results("керування польотом", RESULTS)
    out2 = summarize_results("керування польотом", RESULTS)
    assert out2 is out1  # той самий обʼєкт із LRU-кешу

#!/usr/bin/env python3
"""
Оцінка якості RAG на golden set: hit@1 / hit@5 для tfidf vs voyage.

Числовий доказ поліпшення для звіту (Фаза D плану). Будує індекс кожним
бекендом на РЕАЛЬНІЙ БД і рахує, скільки запитів golden-set знаходять
очікуваний doc_id у top-1 і top-5.

Запуск:
    # лише tfidf (без ключа):
    .venv/bin/python docs/verification/eval_rag.py

    # порівняти з voyage (потрібен VOYAGE_API_KEY):
    VOYAGE_API_KEY=... .venv/bin/python docs/verification/eval_rag.py --with-voyage

Вихід — таблиця hit@1/hit@5 по бекендах + перелік промахів.
"""

import argparse
import asyncio
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

GOLDEN = ROOT / "tests" / "golden_queries.json"


async def _eval_backend(backend_name: str, cases: list[dict]) -> dict:
    """Будує індекс заданим бекендом і рахує hit@1 / hit@5."""
    os.environ["ADD_RAG_BACKEND"] = backend_name
    # ad_config читає env один раз на імпорті → перечитуємо для кожного бекенда
    import importlib

    import ad_config
    importlib.reload(ad_config)
    import rag_index
    importlib.reload(rag_index)
    import ad_embeddings
    importlib.reload(ad_embeddings)
    from ad_db import query

    idx = rag_index.RagIndex()
    st = await idx.build(query)
    actual_backend = st["backend"]  # може деградувати на tfidf без ключа

    hit1 = hit5 = 0
    misses = []
    for c in cases:
        results = await idx.search(c["q"], k=5)
        ids = [r["doc_id"] for r in results]
        top5 = c["expect"] in ids
        top1 = bool(ids) and ids[0] == c["expect"]
        hit5 += top5
        hit1 += top1
        if not top5:
            misses.append((c["q"], c["expect"], ids[:3]))

    n = len(cases)
    return {
        "requested": backend_name,
        "actual": actual_backend,
        "hit1": hit1, "hit5": hit5, "n": n,
        "misses": misses,
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-voyage", action="store_true",
                    help="also evaluate the voyage backend (needs VOYAGE_API_KEY)")
    args = ap.parse_args()

    cases = json.loads(GOLDEN.read_text(encoding="utf-8"))["queries"]
    backends = ["tfidf"]
    if args.with_voyage:
        if not os.getenv("VOYAGE_API_KEY"):
            print("!! --with-voyage requires VOYAGE_API_KEY; skipping voyage")
        else:
            backends.append("voyage")

    reports = [await _eval_backend(b, cases) for b in backends]

    print(f"\nGolden set: {len(cases)} queries\n")
    print(f"{'backend':<20} {'hit@1':>8} {'hit@5':>8}")
    print("-" * 38)
    for r in reports:
        label = r["requested"]
        if r["actual"] != r["requested"]:
            label += f"->{r['actual']}"  # деградація без ключа
        print(f"{label:<20} {r['hit1']}/{r['n']:<6} {r['hit5']}/{r['n']:<6}")

    for r in reports:
        if r["misses"]:
            print(f"\nMisses ({r['requested']}):")
            for q, expect, got in r["misses"]:
                print(f"  '{q}' → expected {expect}, got {got}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

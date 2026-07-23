"""
RAG-інструменти: сервер як RETRIEVER над БД + локальними файлами knowledge/.
Генерацію робить LLM-хост на основі повернених фрагментів (grounding).
"""

from fastmcp.dependencies import CurrentContext
from fastmcp.server.context import Context

import rag_index
from ad_config import mcp
from ad_db import query
from ad_security import _require_role


@mcp.tool
async def rebuild_rag_index(ctx: Context = CurrentContext()) -> dict:
    """Перебудувати RAG-індекс із поточних даних БД + файлів knowledge/*.md.
    Викликати після масових змін у каталозі, щоб пошук був актуальним.

    ⚠️ Multi-replica: індекс тримається в памʼяті процесу, тож цей виклик
    перебудовує лише ту репліку, що обробила запит. Кожна репліка будує свій
    індекс ліниво при першому ask_catalog. Для узгодженості між репліками —
    винести індекс у спільне сховище або тригерити rebuild на всіх (див.
    PRODUCTION.md, п. 5.3)."""
    _require_role(ctx, "editor")  # перебудова індексу — write-подібна дія
    await ctx.info("rebuild_rag_index started")
    status = await rag_index.INDEX.build(query)
    await ctx.info("rebuild_rag_index done", extra=status)
    return status


@mcp.tool
async def ask_catalog(
    question: str,
    k: int = 5,
    ctx: Context = CurrentContext(),
) -> dict:
    """RAG-пошук: повертає top-k релевантних фрагментів каталогу (з БД і
    локальних політик) для питання природною мовою. LLM має будувати
    відповідь ВИКЛЮЧНО на цих фрагментах і посилатися на їхні doc_id.
    Індекс будується лениво при першому виклику."""
    if not rag_index.INDEX.ready:
        await ctx.info("RAG index cold -> building")
        await rag_index.INDEX.build(query)

    results = rag_index.INDEX.search(question, k=k)
    await ctx.info("ask_catalog", extra={"question": question, "hits": len(results)})
    return {
        "question": question,
        "hits": len(results),
        "results": results,
        "grounding_note": (
            "Відповідай тільки за наведеними фрагментами. Якщо їх бракує — "
            "скажи про це й запропонуй уточнити запит."
        ),
    }

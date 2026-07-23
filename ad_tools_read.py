"""
READ-інструменти каталогу + контекст/транспорт/моніторинг (усі read-only).

Нічого не змінюють у БД. Доступ до даних — через Repository-шар
(`product_repo` / `category_repo` / `faq_repo`), а не «сирі» запити. Стеля
`MAX_LIMIT` захищає від «витягни весь каталог».
"""

import asyncio
import time

from fastmcp.dependencies import CurrentContext
from fastmcp.server.context import Context

import rag_index
from ad_config import ALLOWED_STATUSES, MAX_LIMIT, log, mcp
from ad_metrics import METRICS
from ad_repositories import category_repo, faq_repo, product_repo
from ad_security import _current_role


@mcp.tool
async def list_products(
    limit: int = 20,
    ctx: Context = CurrentContext(),
) -> list[dict]:
    """Повертає опубліковані продукти каталогу (id, name, sku, сумісність)."""
    limit = max(1, min(limit, MAX_LIMIT))
    await ctx.info(f"list_products(limit={limit})")
    rows = await product_repo.list_published(limit)
    await ctx.info("list_products done", extra={"rows": len(rows)})
    return rows


@mcp.tool
async def find_products(
    status: str | None = None,
    ndaa_compliant: bool | None = None,
    made_in_usa: bool | None = None,
    category: str | None = None,
    search: str | None = None,
    limit: int = 50,
    ctx: Context = CurrentContext(),
) -> list[dict]:
    """Пошук продуктів за фільтрами (усі — необов'язкові):
    - status: draft/published/archived
    - ndaa_compliant / made_in_usa: True/False
    - category: slug категорії (напр. 'sensors')
    - search: підрядок у назві або SKU
    Приклад сценарію: ndaa_compliant=False, status='published'
    -> опубліковані НЕ NDAA-сумісні продукти."""
    limit = max(1, min(limit, MAX_LIMIT))
    await ctx.info(
        "find_products",
        extra={"status": status, "ndaa": ndaa_compliant,
               "usa": made_in_usa, "category": category, "search": search},
    )
    rows = await product_repo.search(
        status, ndaa_compliant, made_in_usa, category, search, limit, ALLOWED_STATUSES
    )
    await ctx.info("find_products done", extra={"rows": len(rows)})
    return rows


@mcp.tool
async def get_product(
    slug: str,
    ctx: Context = CurrentContext(),
) -> dict:
    """Повна картка продукту за slug: основні поля + specs, features,
    use_cases, faqs, images (дані зібрані з кількох таблиць)."""
    await ctx.info(f"get_product(slug={slug!r})")

    products = await product_repo.get_full_published(slug)
    if not products:
        await ctx.warning(f"product not found: {slug!r}")
        raise ValueError(f"Продукт зі slug '{slug}' не знайдено")

    product = products[0]
    pid = product["id"]

    # Пов'язані таблиці — незалежні запити за product_id, виконуємо ПАРАЛЕЛЬНО
    # (asyncio.gather): результат ідентичний, але картка збирається швидше.
    specs, features, use_cases, faqs, images = await asyncio.gather(
        product_repo.specs(pid),
        product_repo.features(pid),
        product_repo.use_cases(pid),
        faq_repo.by_product(pid),
        product_repo.images(pid),
    )
    product["specs"] = specs
    product["features"] = features
    product["use_cases"] = use_cases
    product["faqs"] = faqs
    product["images"] = images

    await ctx.info(
        "get_product done",
        extra={
            "specs": len(product["specs"]),
            "features": len(product["features"]),
            "faqs": len(product["faqs"]),
        },
    )
    return product


@mcp.tool
async def list_categories(ctx: Context = CurrentContext()) -> list[dict]:
    """Список категорій каталогу з кількістю товарів у кожній."""
    await ctx.info("list_categories")
    rows = await category_repo.list_with_counts()
    await ctx.info("list_categories done", extra={"rows": len(rows)})
    return rows


@mcp.tool
async def get_category(slug: str, ctx: Context = CurrentContext()) -> dict:
    """Категорія за slug + усі її товари (будь-якого статусу)."""
    await ctx.info(f"get_category(slug={slug!r})")
    cats = await category_repo.get(slug)
    if not cats:
        await ctx.warning(f"category not found: {slug!r}")
        raise ValueError(f"Категорію зі slug '{slug}' не знайдено")

    category = cats[0]
    category["products"] = await category_repo.products_of(category["id"])
    await ctx.info("get_category done", extra={"products": len(category["products"])})
    return category


@mcp.tool
async def search_specs(
    search: str,
    limit: int = 50,
    ctx: Context = CurrentContext(),
) -> list[dict]:
    """Пошук за технічними характеристиками (specs) по всіх продуктах.
    Шукає підрядок у назві АБО значенні характеристики.
    Приклад: search='CAN' -> усі продукти з CAN в specs."""
    limit = max(1, min(limit, MAX_LIMIT))
    await ctx.info(f"search_specs(search={search!r})")
    rows = await product_repo.search_specs(search, limit)
    await ctx.info("search_specs done", extra={"rows": len(rows)})
    return rows


@mcp.tool
async def get_faqs(slug: str, ctx: Context = CurrentContext()) -> list[dict]:
    """Тільки FAQ (питання/відповіді) конкретного продукту за slug."""
    await ctx.info(f"get_faqs(slug={slug!r})")
    prod = await product_repo.get_id_name(slug)
    if not prod:
        await ctx.warning(f"product not found: {slug!r}")
        raise ValueError(f"Продукт зі slug '{slug}' не знайдено")
    rows = await faq_repo.by_product(prod[0]["id"])
    await ctx.info("get_faqs done", extra={"rows": len(rows)})
    return rows


@mcp.tool
async def related_products(slug: str, ctx: Context = CurrentContext()) -> list[dict]:
    """Пов'язані продукти (compatible/accessory/related/replacement) за slug.
    Показує, з чим товар працює як єдина система."""
    await ctx.info(f"related_products(slug={slug!r})")
    prod = await product_repo.get_id_name(slug)
    if not prod:
        await ctx.warning(f"product not found: {slug!r}")
        raise ValueError(f"Продукт зі slug '{slug}' не знайдено")
    rows = await product_repo.related(prod[0]["id"])
    await ctx.info("related_products done", extra={"rows": len(rows)})
    return rows


@mcp.tool
async def catalog_stats(ctx: Context = CurrentContext()) -> dict:
    """Зведена статистика каталогу: усього товарів, розподіл за статусами
    та категоріями, кількість NDAA-сумісних і Made in USA."""
    await ctx.info("catalog_stats")

    # Чотири незалежні агрегати — паралельно (asyncio.gather).
    total, by_status, by_category, flags = await asyncio.gather(
        product_repo.count_total(),
        product_repo.count_by_status(),
        category_repo.count_by_category(),
        product_repo.compliance_flags(),
    )

    result = {
        "total_products": total,
        "by_status": {r["status"]: r["n"] for r in by_status},
        "by_category": {r["category"]: r["n"] for r in by_category},
        "ndaa_compliant": int(flags["ndaa_compliant"] or 0),
        "made_in_usa": int(flags["made_in_usa"] or 0),
    }
    await ctx.info("catalog_stats done", extra={"total": total})
    return result


@mcp.tool
async def low_stock(threshold: int = 10, ctx: Context = CurrentContext()) -> list[dict]:
    """Товари із залишком на складі <= threshold.
    Продукти з невідомим залишком (NULL) до вибірки не потрапляють."""
    await ctx.info(f"low_stock(threshold={threshold})")
    rows = await product_repo.low_stock(threshold)
    await ctx.info("low_stock done", extra={"rows": len(rows)})
    return rows


@mcp.tool
async def find_products_by_price(
    min_price: float | None = None,
    max_price: float | None = None,
    ctx: Context = CurrentContext(),
) -> list[dict]:
    """Товари у ціновому діапазоні (обидві межі — необов'язкові).
    Товари без ціни (NULL) до вибірки не потрапляють."""
    await ctx.info(f"find_products_by_price(min={min_price}, max={max_price})")
    rows = await product_repo.by_price(min_price, max_price)
    await ctx.info("find_products_by_price done", extra={"rows": len(rows)})
    return rows


@mcp.tool
async def export_specs(ctx: Context = CurrentContext()) -> dict:
    """Вивантажує технічні характеристики (specs) для ВСІХ опублікованих
    продуктів. Довга операція — повідомляє прогрес через ctx.report_progress."""
    products = await product_repo.list_published_basic()
    total = len(products)
    await ctx.info("export_specs started", extra={"products": total})

    items = []
    for i, p in enumerate(products):
        await ctx.report_progress(progress=i, total=total)  # грань progress
        specs = await product_repo.specs(p["id"])
        items.append({"sku": p["sku"], "name": p["name"], "specs": specs})

    await ctx.report_progress(progress=total, total=total)  # 100%
    total_specs = sum(len(it["specs"]) for it in items)
    await ctx.info("export_specs done",
                   extra={"products": total, "specs": total_specs})
    return {"products": total, "total_specs": total_specs, "items": items}


# ────────────────────────────────────────────────────────────────────────
# Грань REQUEST-CONTEXT: хто і з якими метаданими звертається.
# ────────────────────────────────────────────────────────────────────────
@mcp.tool
async def whoami(ctx: Context = CurrentContext()) -> dict:
    """Показати контекст поточного запиту: request_id, client_id, session_id
    та клієнтські метадані (напр. user_id/trace_id, передані через meta).
    Корисно для аудиту — 'хто саме робить запит'."""
    info = {
        "request_id": ctx.request_id,
        "client_id": ctx.client_id,
        "role": _current_role(ctx),  # яку роль бачить сервер для цього виклику
    }

    try:
        info["session_id"] = ctx.session_id
    except RuntimeError:
        info["session_id"] = "session not established yet"

    meta = getattr(getattr(ctx, "request_context", None), "meta", None)
    if meta:
        info["meta"] = {
            "user_id": getattr(meta, "user_id", None),
            "trace_id": getattr(meta, "trace_id", None),
        }
    else:
        info["meta"] = None

    await ctx.info("whoami", extra={"client_id": ctx.client_id})
    return info


# ────────────────────────────────────────────────────────────────────────
# Грань TRANSPORT: різна поведінка залежно від способу зв'язку.
# ────────────────────────────────────────────────────────────────────────
@mcp.tool
async def catalog_report(ctx: Context = CurrentContext()) -> str:
    """Звіт по каталогу з деталізацією, що залежить від транспорту:
    stdio (локальний CLI) — короткий підсумок; http/sse (мережа) — детально."""
    transport = ctx.transport
    server_name = ctx.fastmcp.name

    stats = await product_repo.count_published_summary()

    await ctx.info("catalog_report", extra={"transport": transport})

    if transport == "stdio":
        body = f"Підсумок: {stats['n']} продуктів, опубліковано {stats['pub']}."
    elif transport in ("sse", "streamable-http", "http"):
        by_cat = await category_repo.count_by_category_named()
        lines = "\n".join(f"  - {r['name']}: {r['n']}" for r in by_cat)
        body = (f"Детальний звіт: {stats['n']} продуктів "
                f"(опубліковано {stats['pub']}).\nЗа категоріями:\n{lines}")
    else:
        body = f"Невідомий транспорт '{transport}'; мінімальний вивід."

    return f"Server: {server_name}\nTransport: {transport}\n\n{body}"


# ────────────────────────────────────────────────────────────────────────
# МОНІТОРИНГ: healthcheck + метрики процесу.
# ────────────────────────────────────────────────────────────────────────
@mcp.tool
async def healthcheck(ctx: Context = CurrentContext()) -> dict:
    """Стан сервісу: доступність БД, готовність RAG-індексу, аптайм, метрики.
    Read-only, доступний будь-якій ролі — для liveness/readiness-проб."""
    db_ok, db_error, products = True, None, None
    try:
        products = await product_repo.count_total()
    except Exception as e:  # noqa: BLE001 - у healthcheck ловимо все свідомо
        # Назовні — нейтральне повідомлення; повні деталі (хост/SQL/стек) лише в лог.
        db_ok = False
        db_error = "database unavailable"
        log.error("healthcheck db error: %s", e)

    return {
        "status": "ok" if db_ok else "degraded",
        "database": {"reachable": db_ok, "products": products, "error": db_error},
        "rag": rag_index.INDEX.status(),
        "uptime_seconds": round(time.time() - METRICS["started_at"], 1),
        "metrics": {k: v for k, v in METRICS.items() if k != "started_at"},
        "role": _current_role(ctx),
    }


@mcp.tool
async def metrics(ctx: Context = CurrentContext()) -> dict:
    """Лічильники процесу: скільки записів закомічено, скільки відмов доступу,
    аптайм. Проста заміна Prometheus для навчального сценарію."""
    return {
        "uptime_seconds": round(time.time() - METRICS["started_at"], 1),
        "writes_committed": METRICS["writes_committed"],
        "writes_denied": METRICS["writes_denied"],
    }

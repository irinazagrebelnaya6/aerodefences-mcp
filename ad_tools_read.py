"""
READ-інструменти каталогу + контекст/транспорт/моніторинг (усі read-only).

Нічого не змінюють у БД. Кожен обирає найточніший запит під намір і логує
хід через ctx. Стеля `MAX_LIMIT` захищає від «витягни весь каталог».
"""

import time

from fastmcp.dependencies import CurrentContext
from fastmcp.server.context import Context

import rag_index
from ad_config import ALLOWED_STATUSES, MAX_LIMIT, log, mcp
from ad_db import query
from ad_metrics import METRICS
from ad_security import _current_role


@mcp.tool
async def list_products(
    limit: int = 20,
    ctx: Context = CurrentContext(),
) -> list[dict]:
    """Повертає опубліковані продукти каталогу (id, name, sku, сумісність)."""
    limit = max(1, min(limit, MAX_LIMIT))
    await ctx.info(f"list_products(limit={limit})")

    rows = await query(
        """
        SELECT id, name, sku, is_ndaa_compliant, is_made_in_usa, status
        FROM products
        WHERE status = 'published'
        ORDER BY sort_order, id
        LIMIT %s
        """,
        (limit,),
    )

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

    where = []
    params: list = []

    if status is not None:
        if status not in ALLOWED_STATUSES:
            raise ValueError(f"Недопустимий статус '{status}'")
        where.append("p.status = %s")
        params.append(status)

    if ndaa_compliant is not None:
        where.append("p.is_ndaa_compliant = %s")
        params.append(1 if ndaa_compliant else 0)

    if made_in_usa is not None:
        where.append("p.is_made_in_usa = %s")
        params.append(1 if made_in_usa else 0)

    if category is not None:
        where.append("c.slug = %s")
        params.append(category)

    if search is not None:
        where.append("(p.name LIKE %s OR p.sku LIKE %s)")
        params.extend([f"%{search}%", f"%{search}%"])

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)

    sql = f"""
        SELECT p.id, p.name, p.sku, p.status,
               p.is_ndaa_compliant, p.is_made_in_usa,
               c.name AS category
        FROM products p
        LEFT JOIN categories c ON c.id = p.category_id
        {where_sql}
        ORDER BY p.sort_order, p.id
        LIMIT %s
    """
    await ctx.info(
        "find_products",
        extra={"status": status, "ndaa": ndaa_compliant,
               "usa": made_in_usa, "category": category, "search": search},
    )
    rows = await query(sql, tuple(params))
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

    products = await query(
        "SELECT * FROM products WHERE slug = %s AND status = 'published'",
        (slug,),
    )
    if not products:
        await ctx.warning(f"product not found: {slug!r}")
        raise ValueError(f"Продукт зі slug '{slug}' не знайдено")

    product = products[0]
    pid = product["id"]

    # Пов'язані таблиці — по одному запиту на кожну (за product_id)
    product["specs"] = await query(
        "SELECT spec_group, spec_name, spec_value FROM product_specs "
        "WHERE product_id = %s ORDER BY sort_order, id",
        (pid,),
    )
    product["features"] = await query(
        "SELECT title, body FROM product_features "
        "WHERE product_id = %s ORDER BY position, id",
        (pid,),
    )
    product["use_cases"] = await query(
        "SELECT title, subtitle FROM product_use_cases "
        "WHERE product_id = %s ORDER BY sort_order, id",
        (pid,),
    )
    product["faqs"] = await query(
        "SELECT question, answer FROM product_faqs "
        "WHERE product_id = %s ORDER BY sort_order, id",
        (pid,),
    )
    product["images"] = await query(
        "SELECT url, alt, is_primary FROM product_images "
        "WHERE product_id = %s ORDER BY is_primary DESC, sort_order, id",
        (pid,),
    )

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
    rows = await query(
        """
        SELECT c.id, c.slug, c.name, c.sort_order, c.is_visible,
               COUNT(p.id) AS products
        FROM categories c
        LEFT JOIN products p ON p.category_id = c.id
        GROUP BY c.id
        ORDER BY c.sort_order, c.name
        """
    )
    await ctx.info("list_categories done", extra={"rows": len(rows)})
    return rows


@mcp.tool
async def get_category(slug: str, ctx: Context = CurrentContext()) -> dict:
    """Категорія за slug + усі її товари (будь-якого статусу)."""
    await ctx.info(f"get_category(slug={slug!r})")
    cats = await query(
        "SELECT id, slug, name, sort_order, is_visible FROM categories WHERE slug = %s",
        (slug,),
    )
    if not cats:
        await ctx.warning(f"category not found: {slug!r}")
        raise ValueError(f"Категорію зі slug '{slug}' не знайдено")

    category = cats[0]
    category["products"] = await query(
        """
        SELECT id, name, sku, slug, status, price, currency
        FROM products
        WHERE category_id = %s
        ORDER BY sort_order, id
        """,
        (category["id"],),
    )
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
    rows = await query(
        """
        SELECT p.name AS product, p.slug, p.status,
               s.spec_group, s.spec_name, s.spec_value
        FROM product_specs s
        JOIN products p ON p.id = s.product_id
        WHERE s.spec_name LIKE %s OR s.spec_value LIKE %s
        ORDER BY p.sort_order, p.id, s.sort_order
        LIMIT %s
        """,
        (f"%{search}%", f"%{search}%", limit),
    )
    await ctx.info("search_specs done", extra={"rows": len(rows)})
    return rows


@mcp.tool
async def get_faqs(slug: str, ctx: Context = CurrentContext()) -> list[dict]:
    """Тільки FAQ (питання/відповіді) конкретного продукту за slug."""
    await ctx.info(f"get_faqs(slug={slug!r})")
    prod = await query("SELECT id, name FROM products WHERE slug = %s", (slug,))
    if not prod:
        await ctx.warning(f"product not found: {slug!r}")
        raise ValueError(f"Продукт зі slug '{slug}' не знайдено")
    rows = await query(
        "SELECT question, answer FROM product_faqs "
        "WHERE product_id = %s ORDER BY sort_order, id",
        (prod[0]["id"],),
    )
    await ctx.info("get_faqs done", extra={"rows": len(rows)})
    return rows


@mcp.tool
async def related_products(slug: str, ctx: Context = CurrentContext()) -> list[dict]:
    """Пов'язані продукти (compatible/accessory/related/replacement) за slug.
    Показує, з чим товар працює як єдина система."""
    await ctx.info(f"related_products(slug={slug!r})")
    prod = await query("SELECT id, name FROM products WHERE slug = %s", (slug,))
    if not prod:
        await ctx.warning(f"product not found: {slug!r}")
        raise ValueError(f"Продукт зі slug '{slug}' не знайдено")
    rows = await query(
        """
        SELECT r.relation_type, r.group_label, r.label,
               rp.name AS related_name, rp.slug AS related_slug, rp.status
        FROM product_relations r
        LEFT JOIN products rp ON rp.id = r.related_product_id
        WHERE r.product_id = %s
        ORDER BY r.relation_type, r.id
        """,
        (prod[0]["id"],),
    )
    await ctx.info("related_products done", extra={"rows": len(rows)})
    return rows


@mcp.tool
async def catalog_stats(ctx: Context = CurrentContext()) -> dict:
    """Зведена статистика каталогу: усього товарів, розподіл за статусами
    та категоріями, кількість NDAA-сумісних і Made in USA."""
    await ctx.info("catalog_stats")

    total = (await query("SELECT COUNT(*) AS n FROM products"))[0]["n"]
    by_status = await query(
        "SELECT status, COUNT(*) AS n FROM products GROUP BY status ORDER BY status"
    )
    by_category = await query(
        """
        SELECT c.name AS category, COUNT(p.id) AS n
        FROM categories c
        LEFT JOIN products p ON p.category_id = c.id
        GROUP BY c.id
        ORDER BY n DESC, c.name
        """
    )
    flags = (
        await query(
            """
            SELECT
              SUM(is_ndaa_compliant) AS ndaa_compliant,
              SUM(is_made_in_usa)    AS made_in_usa
            FROM products
            """
        )
    )[0]

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
    rows = await query(
        """
        SELECT id, name, sku, slug, status, stock_quantity
        FROM products
        WHERE stock_quantity IS NOT NULL AND stock_quantity <= %s
        ORDER BY stock_quantity, id
        """,
        (threshold,),
    )
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
    where = ["price IS NOT NULL"]
    params: list = []
    if min_price is not None:
        where.append("price >= %s")
        params.append(min_price)
    if max_price is not None:
        where.append("price <= %s")
        params.append(max_price)
    sql = f"""
        SELECT id, name, sku, slug, status, price, currency
        FROM products
        WHERE {' AND '.join(where)}
        ORDER BY price, id
    """
    rows = await query(sql, tuple(params))
    await ctx.info("find_products_by_price done", extra={"rows": len(rows)})
    return rows


@mcp.tool
async def export_specs(ctx: Context = CurrentContext()) -> dict:
    """Вивантажує технічні характеристики (specs) для ВСІХ опублікованих
    продуктів. Довга операція — повідомляє прогрес через ctx.report_progress."""
    products = await query(
        "SELECT id, name, sku FROM products "
        "WHERE status = 'published' ORDER BY sort_order, id"
    )
    total = len(products)
    await ctx.info("export_specs started", extra={"products": total})

    items = []
    for i, p in enumerate(products):
        await ctx.report_progress(progress=i, total=total)  # грань progress
        specs = await query(
            "SELECT spec_group, spec_name, spec_value FROM product_specs "
            "WHERE product_id = %s ORDER BY sort_order, id",
            (p["id"],),
        )
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

    stats = (await query(
        "SELECT COUNT(*) AS n, SUM(status='published') AS pub FROM products"
    ))[0]

    await ctx.info("catalog_report", extra={"transport": transport})

    if transport == "stdio":
        body = f"Підсумок: {stats['n']} продуктів, опубліковано {stats['pub']}."
    elif transport in ("sse", "streamable-http", "http"):
        by_cat = await query(
            "SELECT c.name, COUNT(*) AS n FROM products p "
            "LEFT JOIN categories c ON c.id = p.category_id "
            "GROUP BY c.name ORDER BY n DESC"
        )
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
        row = (await query("SELECT COUNT(*) AS n FROM products"))[0]
        products = row["n"]
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

"""
MCP-сервер над базою `aerodefences` (MySQL у Docker).

Поки що — мінімальний крок: одне read-only знаряддя `list_products`,
підключення до БД і логування запиту через контекст.
Далі нарощуватимемо інструменти (get_product, find_products, тощо).
"""

import asyncio
import json
import os

import pymysql
import pymysql.err
from dbutils.pooled_db import PooledDB
from dotenv import load_dotenv
from prometheus_client import Counter as PromCounter
from prometheus_client import start_http_server as prom_start_http_server
from pymysql.cursors import DictCursor

# Читаємо .env (якщо є) ще до формування DB_CONFIG
load_dotenv()

import logging
import time

from fastmcp import FastMCP
from fastmcp.dependencies import CurrentContext
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.context import Context
from fastmcp.server.dependencies import get_access_token, get_context
from fastmcp.server.elicitation import (
    AcceptedElicitation,
    CancelledElicitation,
    DeclinedElicitation,
)
from mcp.types import ResourceListChangedNotification

import rag_index

ALLOWED_STATUSES = ("draft", "published", "archived")

# ── Безпека: рівні ролей (RBAC) ─────────────────────────────────────────
# viewer  — лише читання (read-інструменти);
# editor  — читання + звичайні write-операції (ціна, склад, тексти, FAQ);
# admin   — усе, включно з compliance-прапорцями та масовими діями.
ROLES = {"viewer": 0, "editor": 1, "admin": 2}

# ── Моніторинг: лічильники на процес (чесні, з єдиних чокпоінтів) ────────
# In-memory METRICS лишаємо (використовується локально/у тестах), і ПАРАЛЕЛЬНО
# дзеркалимо у Prometheus-лічильники — щоб за кількох реплік метрики не
# фрагментувались, а збирались скрейпером із /metrics (див. __main__).
METRICS = {
    "started_at": time.time(),
    "writes_committed": 0,   # рахується в _run_write (кожен commit)
    "writes_denied": 0,      # рахується в _require_role (RBAC відмова)
}
PROM_WRITES_COMMITTED = PromCounter(
    "aerodefences_writes_committed_total", "Успішно закомічені write-операції"
)
PROM_WRITES_DENIED = PromCounter(
    "aerodefences_writes_denied_total", "Відмови RBAC на write-операціях"
)


class _JsonLogFormatter(logging.Formatter):
    """Структурований JSON-рядок на лог-запис (для прод-збирача: Loki/ELK)."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


# Логи йдуть у stderr (не заважає stdio-протоколу MCP у stdout).
# ADD_LOG_LEVEL=DEBUG|INFO|WARNING керує детальністю;
# ADD_LOG_FORMAT=json|text — формат (json для проду, text для локальної розробки).
_log_handler = logging.StreamHandler()
if os.getenv("ADD_LOG_FORMAT", "text").lower() == "json":
    _log_handler.setFormatter(_JsonLogFormatter())
else:
    _log_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s aerodefences %(message)s")
    )
logging.basicConfig(level=os.getenv("ADD_LOG_LEVEL", "INFO"), handlers=[_log_handler])
log = logging.getLogger("aerodefences")

# Транспорт визначаємо на рівні модуля: від нього залежить authn і джерело ролі.
TRANSPORT = os.getenv("ADD_TRANSPORT", "stdio")


def _build_auth():
    """JWT-автентифікація для мережевого (HTTP) транспорту.

    stdio (локальний довірений хост) authn не потребує → None.
    Для http вимагаємо джерело ключів (JWKS-URL або публічний ключ): без нього
    сервер НЕ стартує, щоб не підняти незахищений мережевий ендпоінт (fail-safe).
    """
    if TRANSPORT != "http":
        return None
    jwks_uri = os.getenv("ADD_JWT_JWKS_URI")
    public_key = os.getenv("ADD_JWT_PUBLIC_KEY")
    if not (jwks_uri or public_key):
        raise RuntimeError(
            "HTTP-транспорт вимагає JWT-authn: задай ADD_JWT_JWKS_URI або ADD_JWT_PUBLIC_KEY."
        )
    return JWTVerifier(
        jwks_uri=jwks_uri,
        public_key=public_key,
        issuer=os.getenv("ADD_JWT_ISSUER"),
        audience=os.getenv("ADD_JWT_AUDIENCE"),
    )


mcp = FastMCP(name="AeroDefences Catalog Server", auth=_build_auth())

# --- Налаштування підключення ---
# Креденшели (user/password) читаються ВИКЛЮЧНО з .env і НЕ зберігаються в коді.
# Локально: скопіювати .env.example -> .env і підставити власні значення.
# Стеля на розмір вибірки read-інструментів (захист від «витягни весь каталог»).
MAX_LIMIT = int(os.getenv("ADD_MAX_LIMIT", "200"))

DB_CONFIG = dict(
    host=os.getenv("ADD_DB_HOST", "127.0.0.1"),
    port=int(os.getenv("ADD_DB_PORT", "3306")),
    user=os.getenv("ADD_DB_USER", ""),
    password=os.getenv("ADD_DB_PASSWORD", ""),
    database=os.getenv("ADD_DB_NAME", "aerodefences"),
    # Таймаути: щоб зависла БД не тримала healthcheck/readiness й воркер-потік.
    connect_timeout=int(os.getenv("ADD_DB_CONNECT_TIMEOUT", "5")),
    read_timeout=int(os.getenv("ADD_DB_READ_TIMEOUT", "10")),
    write_timeout=int(os.getenv("ADD_DB_WRITE_TIMEOUT", "10")),
)
# TLS до MySQL: вмикається, коли задано CA-сертифікат (ADD_DB_SSL_CA).
_db_ssl_ca = os.getenv("ADD_DB_SSL_CA")
if _db_ssl_ca:
    DB_CONFIG["ssl"] = {"ca": _db_ssl_ca}

# Транзієнтні помилки БД, на яких має сенс повторити read.
_RETRYABLE_DB = (pymysql.err.OperationalError, pymysql.err.InterfaceError)

# Пул зʼєднань (один на процес). Ліниво: НЕ конектимось на імпорті модуля.
_POOL: PooledDB | None = None


def _get_pool() -> PooledDB:
    global _POOL
    if _POOL is None:
        _POOL = PooledDB(
            creator=pymysql,
            maxconnections=int(os.getenv("ADD_DB_POOL_SIZE", "5")),
            mincached=1,
            blocking=True,      # чекати вільне зʼєднання, а не падати
            ping=1,             # пінгувати зʼєднання перед видачею (реконект стейл)
            cursorclass=DictCursor,
            **DB_CONFIG,
        )
    return _POOL


def _retry_read(fn, attempts: int = 3, base_delay: float = 0.1):
    """Повтор із експоненційним бекофом на транзієнтних збоях БД.
    Застосовується ЛИШЕ до читань (write не повторюємо наосліп — щоб не
    задублювати INSERT; для write покладаємось на ping=1 у пулі)."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except _RETRYABLE_DB as e:
            last = e
            log.warning("db transient (read %d/%d): %s", i + 1, attempts, e.__class__.__name__)
            time.sleep(base_delay * (2 ** i))
    assert last is not None
    raise last


def _run_query(sql: str, params: tuple = ()) -> list[dict]:
    """Синхронний запит до MySQL через пул. Повертає список рядків як dict."""
    def _do():
        conn = _get_pool().connection()  # зі спільного пулу; close() повертає у пул
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()
        finally:
            conn.close()

    return _retry_read(_do)


async def query(sql: str, params: tuple = ()) -> list[dict]:
    """Async-обгортка: виконує блокуючий запит у окремому потоці."""
    return await asyncio.to_thread(_run_query, sql, params)


# ── Безпека: визначення ролі та бар'єр контролю доступу ──────────────────
def _role_from_token() -> str | None:
    """Роль із ПЕРЕВІРЕНОГО JWT: спершу claim `role`, потім scopes.
    None — якщо токена немає або роль не розпізнано."""
    try:
        token = get_access_token()
    except Exception:
        return None
    if token is None:
        return None
    claims = getattr(token, "claims", None) or {}
    role = claims.get("role")
    if role in ROLES:
        return role
    scopes = set(getattr(token, "scopes", None) or [])
    for r in ("admin", "editor", "viewer"):
        if r in scopes or f"role:{r}" in scopes:
            return r
    return None


def _current_role(ctx: Context) -> str:
    """Роль поточного виклику. Джерело залежить від транспорту:

    • http  — ЛИШЕ перевірений JWT (claim `role` / scopes). Клієнтські `meta`
      більше НЕ впливають на роль (усунення обходу RBAC). Автентифікований,
      але без валідної ролі → мінімальна `viewer`.
    • stdio — локальний довірений хост: роль з env. Дефолт `viewer` (fail-safe);
      dev може підняти через `ADD_DEV_ROLE`.
    """
    if TRANSPORT == "http":
        return _role_from_token() or "viewer"
    role = os.getenv("ADD_DEV_ROLE") or os.getenv("ADD_ROLE", "viewer")
    return role if role in ROLES else "viewer"


def _require_role(ctx: Context, minimum: str) -> None:
    """Кидає PermissionError, якщо роль виклику нижча за потрібну."""
    role = _current_role(ctx)
    if ROLES[role] < ROLES[minimum]:
        METRICS["writes_denied"] += 1
        PROM_WRITES_DENIED.inc()
        log.warning("access denied: role=%s needs=%s", role, minimum)
        raise PermissionError(
            f"Недостатньо прав: потрібна роль '{minimum}', поточна '{role}'."
        )


@mcp.prompt
def compliance_report(product_names: str, standard: str = "NDAA") -> str:
    """Готовий шаблон-підказка для звіту про відповідність продуктів стандарту.
    Грань PROMPTS: сервер віддає заготовку, яку LLM наповнює даними каталогу."""
    return f"""
Ти готуєш звіт про відповідність продукції стандарту {standard}.

Продукти для перевірки: {product_names}

Для кожного продукту вкажи:
1. Назву та SKU.
2. Чи відповідає {standard} (is_ndaa_compliant) і чи Made in USA.
3. Короткий висновок: чи можна пропонувати державному замовнику.

Наприкінці — загальний підсумок: скільки продуктів відповідають, скільки ні.
""".strip()


@mcp.resource("resource://schema")
async def schema() -> str:
    """Схема БД aerodefences: таблиці та їхні колонки.
    Ресурс для LLM — щоб вона оперувала реальними полями, а не вигаданими."""
    rows = await query(
        """
        SELECT TABLE_NAME AS table_name,
               COLUMN_NAME AS column_name,
               COLUMN_TYPE AS column_type,
               IS_NULLABLE AS is_nullable,
               COLUMN_KEY AS column_key
        FROM information_schema.columns
        WHERE TABLE_SCHEMA = %s
        ORDER BY TABLE_NAME, ORDINAL_POSITION
        """,
        (DB_CONFIG["database"],),
    )
    tables: dict[str, list] = {}
    for r in rows:
        tables.setdefault(r["table_name"], []).append(
            {
                "column": r["column_name"],
                "type": r["column_type"],
                "nullable": r["is_nullable"] == "YES",
                "key": r["column_key"] or None,
            }
        )
    return json.dumps({"database": DB_CONFIG["database"], "tables": tables}, indent=2)


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


async def _run_write(sql: str, params: tuple = ()) -> int:
    """Синхронний INSERT/UPDATE/DELETE. Повертає кількість змінених рядків."""
    def _do():
        conn = _get_pool().connection()  # зі спільного пулу (ping=1 реконектить стейл)
        try:
            with conn.cursor() as cur:
                affected = cur.execute(sql, params)
            conn.commit()
            return affected
        finally:
            conn.close()

    affected = await asyncio.to_thread(_do)
    if affected:
        METRICS["writes_committed"] += 1
        PROM_WRITES_COMMITTED.inc()
        log.info("write committed: affected=%s", affected)

    # Грань NOTIFICATIONS: якщо каталог реально змінився — сповіщаємо клієнта,
    # щоб він розумів, що раніше прочитані дані застаріли. Робиться в одному
    # місці, тому спрацьовує для ВСІХ write-знарядь, що ходять через _run_write.
    if affected:
        try:
            ctx = get_context()
            await ctx.send_notification(ResourceListChangedNotification())
            await ctx.info("catalog changed -> notification sent",
                           extra={"affected": affected})
        except RuntimeError:
            pass  # немає активного MCP-контексту (виклик поза сесією)

    return affected


async def _confirm(ctx: Context, message: str, min_role: str = "editor") -> tuple[bool, str]:
    """Спільний бар'єр для write-операцій: спершу RBAC, потім підтвердження людини.
    Повертає (ok, reason): ok=True якщо роль достатня І користувач відповів 'yes',
    інакше ok=False і текст причини скасування для відповіді клієнту."""
    _require_role(ctx, min_role)  # бар'єр контролю доступу ДО діалогу підтвердження
    confirm = await ctx.elicit(message, response_type=["yes", "no"])
    match confirm:
        case AcceptedElicitation(data="yes"):
            return True, ""
        case AcceptedElicitation(data="no"):
            await ctx.info("Користувач відповів 'no' — зміну скасовано")
            return False, "Скасовано користувачем (відповідь 'no')."
        case DeclinedElicitation():
            await ctx.info("Користувач відхилив підтвердження — зміну скасовано")
            return False, "Скасовано: підтвердження не надано."
        case CancelledElicitation():
            await ctx.info("Користувач перервав операцію")
            return False, "Операцію перервано."
    return False, "Скасовано."


@mcp.tool
async def set_product_status(
    slug: str,
    status: str,
    ctx: Context = CurrentContext(),
) -> str:
    """Змінює статус продукту (draft/published/archived).
    НЕБЕЗПЕЧНА дія: перед записом ЗАВЖДИ питає підтвердження в користувача."""

    # 0) БАР'ЄР ДОСТУПУ: змінювати статус може лише editor+
    _require_role(ctx, "editor")

    # 1) валідація вхідних даних
    if status not in ALLOWED_STATUSES:
        raise ValueError(
            f"Недопустимий статус '{status}'. Дозволені: {', '.join(ALLOWED_STATUSES)}"
        )

    # 2) перевіряємо, що продукт існує, і показуємо поточний статус
    rows = await query(
        "SELECT id, name, status FROM products WHERE slug = %s", (slug,)
    )
    if not rows:
        raise ValueError(f"Продукт зі slug '{slug}' не знайдено")

    product = rows[0]
    current = product["status"]
    if current == status:
        return f"Статус '{product['name']}' вже '{status}' — змін не потрібно."

    # 3) БАР'ЄР: явне підтвердження людини перед зміною
    await ctx.info(f"set_product_status: запит підтвердження {current} -> {status}")
    confirm = await ctx.elicit(
        f"Змінити статус '{product['name']}' з '{current}' на '{status}'? "
        f"(archived приховає товар від клієнтів)",
        response_type=["yes", "no"],
    )

    match confirm:
        case AcceptedElicitation(data="yes"):
            pass  # продовжуємо до запису
        case AcceptedElicitation(data="no"):
            await ctx.info("Користувач відповів 'no' — зміну скасовано")
            return "Скасовано користувачем (відповідь 'no')."
        case DeclinedElicitation():
            await ctx.info("Користувач відхилив підтвердження — зміну скасовано")
            return "Скасовано: підтвердження не надано."
        case CancelledElicitation():
            await ctx.info("Користувач перервав операцію")
            return "Операцію перервано."

    # 4) сам запис
    affected = await _run_write(
        "UPDATE products SET status = %s WHERE id = %s",
        (status, product["id"]),
    )
    await ctx.info(
        "set_product_status done",
        extra={"slug": slug, "from": current, "to": status, "affected": affected},
    )
    return f"OK: '{product['name']}' {current} -> {status} (змінено рядків: {affected})"


# Поля products, які дозволено редагувати через update_product_field.
# БІЛИЙ СПИСОК: лише текстові поля. slug/sku/status/price/stock мають окремі
# інструменти й сюди свідомо НЕ входять.
ALLOWED_UPDATE_FIELDS = (
    "subtitle", "short_description", "long_description", "key_advantage",
    "package_contents", "sku_descriptor", "documentation_url",
    "highlights_intro", "specs_intro", "built_for_intro", "compatible_intro",
)


async def _get_product_row(slug: str) -> dict:
    """Повертає базовий рядок продукту або кидає ValueError."""
    rows = await query("SELECT * FROM products WHERE slug = %s", (slug,))
    if not rows:
        raise ValueError(f"Продукт зі slug '{slug}' не знайдено")
    return rows[0]


@mcp.tool
async def update_price(
    slug: str,
    price: float,
    currency: str | None = None,
    ctx: Context = CurrentContext(),
) -> str:
    """Змінює ціну продукту (і, за бажанням, валюту).
    НЕБЕЗПЕЧНА дія: запитує підтвердження перед записом."""
    if price < 0:
        raise ValueError("Ціна не може бути від'ємною")
    p = await _get_product_row(slug)

    sets, params = ["price = %s"], [price]
    cur_desc = f"{p['price']} {p['currency']}"
    new_desc = f"{price} {p['currency']}"
    if currency is not None:
        if len(currency) != 3:
            raise ValueError("Код валюти має бути 3 символи (напр. 'USD')")
        sets.append("currency = %s")
        params.append(currency.upper())
        new_desc = f"{price} {currency.upper()}"

    ok, reason = await _confirm(
        ctx, f"Змінити ціну '{p['name']}' з {cur_desc} на {new_desc}?"
    )
    if not ok:
        return reason

    params.append(p["id"])
    n = await _run_write(f"UPDATE products SET {', '.join(sets)} WHERE id = %s", tuple(params))
    await ctx.info("update_price done", extra={"slug": slug, "to": new_desc, "affected": n})
    return f"OK: '{p['name']}' ціна -> {new_desc} (змінено рядків: {n})"


@mcp.tool
async def update_stock(
    slug: str,
    quantity: int,
    ctx: Context = CurrentContext(),
) -> str:
    """Змінює залишок на складі. НЕБЕЗПЕЧНА дія: підтвердження перед записом."""
    if quantity < 0:
        raise ValueError("Залишок не може бути від'ємним")
    p = await _get_product_row(slug)

    ok, reason = await _confirm(
        ctx, f"Змінити залишок '{p['name']}' з {p['stock_quantity']} на {quantity}?"
    )
    if not ok:
        return reason

    n = await _run_write(
        "UPDATE products SET stock_quantity = %s WHERE id = %s", (quantity, p["id"])
    )
    await ctx.info("update_stock done", extra={"slug": slug, "to": quantity, "affected": n})
    return f"OK: '{p['name']}' залишок {p['stock_quantity']} -> {quantity} (рядків: {n})"


@mcp.tool
async def set_compliance(
    slug: str,
    ndaa: bool | None = None,
    made_in_usa: bool | None = None,
    ctx: Context = CurrentContext(),
) -> str:
    """Змінює прапорці NDAA-сумісності та/або Made in USA.
    ⚠️ Це compliance-дані — юридично значимо. Підтвердження обов'язкове."""
    if ndaa is None and made_in_usa is None:
        raise ValueError("Вкажіть хоча б один прапорець: ndaa або made_in_usa")
    p = await _get_product_row(slug)

    sets, params, changes = [], [], []
    if ndaa is not None:
        sets.append("is_ndaa_compliant = %s")
        params.append(1 if ndaa else 0)
        changes.append(f"NDAA {bool(p['is_ndaa_compliant'])} -> {ndaa}")
    if made_in_usa is not None:
        sets.append("is_made_in_usa = %s")
        params.append(1 if made_in_usa else 0)
        changes.append(f"Made in USA {bool(p['is_made_in_usa'])} -> {made_in_usa}")

    ok, reason = await _confirm(
        ctx, f"Змінити для '{p['name']}': {'; '.join(changes)}?",
        min_role="admin",  # compliance — юридично значимо, лише admin
    )
    if not ok:
        return reason

    params.append(p["id"])
    n = await _run_write(f"UPDATE products SET {', '.join(sets)} WHERE id = %s", tuple(params))
    await ctx.info("set_compliance done", extra={"slug": slug, "changes": changes, "affected": n})
    return f"OK: '{p['name']}' — {'; '.join(changes)} (рядків: {n})"


@mcp.tool
async def update_product_field(
    slug: str,
    field: str,
    value: str,
    ctx: Context = CurrentContext(),
) -> str:
    """Оновлює одне ТЕКСТОВЕ поле продукту (subtitle, short_description тощо).
    Дозволені лише поля з білого списку ALLOWED_UPDATE_FIELDS — інші відхиляються.
    Підтвердження перед записом."""
    if field not in ALLOWED_UPDATE_FIELDS:
        raise ValueError(
            f"Поле '{field}' не дозволено. Дозволені: {', '.join(ALLOWED_UPDATE_FIELDS)}"
        )
    p = await _get_product_row(slug)
    old = p.get(field)

    ok, reason = await _confirm(
        ctx,
        f"Змінити '{field}' у '{p['name']}'?\n"
        f"Було: {str(old)[:120]!r}\nСтане: {value[:120]!r}",
    )
    if not ok:
        return reason

    # field вже перевірено по білому списку — безпечно підставити в SQL
    n = await _run_write(f"UPDATE products SET {field} = %s WHERE id = %s", (value, p["id"]))
    await ctx.info("update_product_field done", extra={"slug": slug, "field": field, "affected": n})
    return f"OK: '{p['name']}'.{field} оновлено (рядків: {n})"


@mcp.tool
async def add_spec(
    slug: str,
    spec_group: str,
    spec_name: str,
    spec_value: str,
    ctx: Context = CurrentContext(),
) -> str:
    """Додає нову технічну характеристику продукту. Підтвердження перед записом."""
    p = await _get_product_row(slug)
    nxt = (await query(
        "SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM product_specs WHERE product_id = %s",
        (p["id"],),
    ))[0]["n"]

    ok, reason = await _confirm(
        ctx, f"Додати spec до '{p['name']}': [{spec_group}] {spec_name} = {spec_value!r}?"
    )
    if not ok:
        return reason

    n = await _run_write(
        "INSERT INTO product_specs (product_id, spec_group, spec_name, spec_value, sort_order) "
        "VALUES (%s, %s, %s, %s, %s)",
        (p["id"], spec_group, spec_name, spec_value, nxt),
    )
    await ctx.info("add_spec done", extra={"slug": slug, "affected": n})
    return f"OK: до '{p['name']}' додано spec [{spec_group}] {spec_name} (рядків: {n})"


@mcp.tool
async def add_faq(
    slug: str,
    question: str,
    answer: str,
    ctx: Context = CurrentContext(),
) -> str:
    """Додає нове FAQ (питання/відповідь) продукту. Підтвердження перед записом."""
    p = await _get_product_row(slug)
    nxt = (await query(
        "SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM product_faqs WHERE product_id = %s",
        (p["id"],),
    ))[0]["n"]

    ok, reason = await _confirm(
        ctx, f"Додати FAQ до '{p['name']}'?\nQ: {question}\nA: {answer[:120]}"
    )
    if not ok:
        return reason

    n = await _run_write(
        "INSERT INTO product_faqs (product_id, question, answer, sort_order, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, NOW(), NOW())",
        (p["id"], question, answer, nxt),
    )
    await ctx.info("add_faq done", extra={"slug": slug, "affected": n})
    return f"OK: до '{p['name']}' додано FAQ (рядків: {n})"


@mcp.tool
async def reorder_product(
    slug: str,
    sort_order: int,
    ctx: Context = CurrentContext(),
) -> str:
    """Змінює позицію продукту у видачі (sort_order). Підтвердження перед записом."""
    if sort_order < 0:
        raise ValueError("sort_order не може бути від'ємним")
    p = await _get_product_row(slug)

    ok, reason = await _confirm(
        ctx, f"Змінити позицію '{p['name']}' з {p['sort_order']} на {sort_order}?"
    )
    if not ok:
        return reason

    n = await _run_write(
        "UPDATE products SET sort_order = %s WHERE id = %s", (sort_order, p["id"])
    )
    await ctx.info("reorder_product done", extra={"slug": slug, "to": sort_order, "affected": n})
    return f"OK: '{p['name']}' позиція {p['sort_order']} -> {sort_order} (рядків: {n})"


@mcp.tool
async def bulk_set_status(
    category: str,
    status: str,
    ctx: Context = CurrentContext(),
) -> str:
    """МАСОВО змінює статус УСІХ продуктів у категорії (за slug категорії).
    🔴 Зачіпає багато рядків одразу. Підтвердження показує, скільки саме."""
    if status not in ALLOWED_STATUSES:
        raise ValueError(
            f"Недопустимий статус '{status}'. Дозволені: {', '.join(ALLOWED_STATUSES)}"
        )
    cats = await query("SELECT id, name FROM categories WHERE slug = %s", (category,))
    if not cats:
        raise ValueError(f"Категорію зі slug '{category}' не знайдено")
    cat = cats[0]

    cnt = (await query(
        "SELECT COUNT(*) AS n FROM products WHERE category_id = %s AND status <> %s",
        (cat["id"], status),
    ))[0]["n"]
    if cnt == 0:
        return f"У категорії '{cat['name']}' немає товарів для зміни на '{status}' — no-op."

    ok, reason = await _confirm(
        ctx,
        f"Змінити статус на '{status}' для {cnt} товар(ів) у категорії "
        f"'{cat['name']}'? Це масова дія.",
        min_role="admin",  # масова зміна — лише admin
    )
    if not ok:
        return reason

    n = await _run_write(
        "UPDATE products SET status = %s WHERE category_id = %s AND status <> %s",
        (status, cat["id"], status),
    )
    await ctx.info("bulk_set_status done",
                   extra={"category": category, "status": status, "affected": n})
    return f"OK: у категорії '{cat['name']}' переведено {n} товар(ів) у '{status}'."


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
# Грань STATE: «кошик вибраних продуктів» у межах сесії.
# Дозволяє: вибрати продукти одним запитом, а наступним — діяти над ними,
# не перелічуючи їх знову. Стан живе в сесії (ctx.set_state/get_state).
# ────────────────────────────────────────────────────────────────────────
SELECTION_KEY = "selected_products"
SELECTION_TTL = int(os.getenv("ADD_SELECTION_TTL", "3600"))  # сек, лише для Redis

# Спільне сховище стану сесій. Якщо задано ADD_REDIS_URL — вибір живе в Redis
# (працює за кількох реплік/без sticky-sessions); інакше фолбек на памʼять сесії
# процесу (ctx.set_state) — зручно локально й у тестах.
_redis_client = None


def _redis():
    global _redis_client
    url = os.getenv("ADD_REDIS_URL")
    if not url:
        return None
    if _redis_client is None:
        import redis
        _redis_client = redis.Redis.from_url(url, decode_responses=True)
    return _redis_client


def _session_key(ctx: Context) -> str:
    try:
        return f"sel:{ctx.session_id}"
    except Exception:
        return "sel:local"


async def _get_selection(ctx: Context) -> list[dict]:
    """Поточний вибір (порожній список, якщо ще не вибирали)."""
    r = _redis()
    if r is not None:
        raw = await asyncio.to_thread(r.get, _session_key(ctx))
        return json.loads(raw) if raw else []
    return await ctx.get_state(SELECTION_KEY) or []


async def _set_selection(ctx: Context, value: list[dict]) -> None:
    r = _redis()
    if r is not None:
        await asyncio.to_thread(
            r.set, _session_key(ctx), json.dumps(value, default=str), SELECTION_TTL
        )
        return
    await ctx.set_state(SELECTION_KEY, value)


async def _del_selection(ctx: Context) -> None:
    r = _redis()
    if r is not None:
        await asyncio.to_thread(r.delete, _session_key(ctx))
        return
    await ctx.delete_state(SELECTION_KEY)


@mcp.tool
async def select_products(
    slugs: list[str],
    ctx: Context = CurrentContext(),
) -> dict:
    """Запам'ятати набір продуктів (за списком slug) у стані сесії.
    Замінює попередній вибір. Неіснуючі slug ігноруються й повертаються окремо."""
    found, missing = [], []
    for s in slugs:
        rows = await query("SELECT id, name, slug FROM products WHERE slug = %s", (s,))
        (found if rows else missing).append(rows[0] if rows else s)

    await _set_selection(ctx, found)
    await ctx.info("select_products", extra={"selected": len(found), "missing": len(missing)})
    return {
        "selected": [p["slug"] for p in found],
        "count": len(found),
        "not_found": missing,
    }


@mcp.tool
async def add_to_selection(slug: str, ctx: Context = CurrentContext()) -> dict:
    """Додати один продукт до поточного вибору (стан сесії)."""
    rows = await query("SELECT id, name, slug FROM products WHERE slug = %s", (slug,))
    if not rows:
        raise ValueError(f"Продукт зі slug '{slug}' не знайдено")

    selection = await _get_selection(ctx)
    if any(p["slug"] == slug for p in selection):
        return {"count": len(selection), "note": "вже у виборі"}

    selection.append(rows[0])
    await _set_selection(ctx, selection)
    await ctx.info("add_to_selection", extra={"slug": slug, "count": len(selection)})
    return {"selected": [p["slug"] for p in selection], "count": len(selection)}


@mcp.tool
async def get_selection(ctx: Context = CurrentContext()) -> list[dict]:
    """Показати поточний вибір продуктів (стан сесії)."""
    selection = await _get_selection(ctx)
    await ctx.info("get_selection", extra={"count": len(selection)})
    return selection


@mcp.tool
async def clear_selection(ctx: Context = CurrentContext()) -> str:
    """Очистити вибір продуктів (стан сесії)."""
    await _del_selection(ctx)
    await ctx.info("clear_selection")
    return "Вибір очищено."


@mcp.tool
async def apply_status_to_selection(
    status: str,
    ctx: Context = CurrentContext(),
) -> str:
    """Застосувати статус (draft/published/archived) до ВСІХ вибраних продуктів.
    Демонструє зв'язку state+elicitation: вибір беремо зі стану сесії, а перед
    масовим записом питаємо підтвердження. Нотифікація спрацює через _run_write."""
    if status not in ALLOWED_STATUSES:
        raise ValueError(f"Недопустимий статус '{status}'")

    selection = await _get_selection(ctx)
    if not selection:
        return "Вибір порожній. Спершу викликати select_products."

    names = ", ".join(p["name"] for p in selection)
    ok, reason = await _confirm(
        ctx, f"Перевести {len(selection)} вибраних продуктів у '{status}'? ({names})",
        min_role="admin",  # масова дія над вибором — лише admin
    )
    if not ok:
        return reason

    ids = [p["id"] for p in selection]
    placeholders = ", ".join(["%s"] * len(ids))
    n = await _run_write(
        f"UPDATE products SET status = %s WHERE id IN ({placeholders})",
        tuple([status, *ids]),
    )
    await ctx.info("apply_status_to_selection done",
                   extra={"status": status, "affected": n})
    return f"OK: {n} вибраних продуктів переведено у '{status}'."


# ────────────────────────────────────────────────────────────────────────
# RAG: сервер як RETRIEVER над БД + локальними файлами knowledge/.
# Генерацію робить LLM-хост на основі повернених фрагментів (grounding).
# ────────────────────────────────────────────────────────────────────────
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


if __name__ == "__main__":
    # ADD_TRANSPORT=stdio (дефолт, локальний хост) | http (мережевий деплой).
    if TRANSPORT == "http":
        host = os.getenv("ADD_HTTP_HOST", "0.0.0.0")
        port = int(os.getenv("ADD_HTTP_PORT", "8000"))
        # Prometheus-метрики на окремому порту (/metrics) для скрейпера.
        metrics_port = int(os.getenv("ADD_METRICS_PORT", "9100"))
        prom_start_http_server(metrics_port)
        log.info("starting HTTP transport on %s:%s (metrics on :%s)", host, port, metrics_port)
        mcp.run(transport="http", host=host, port=port)
    else:
        mcp.run()

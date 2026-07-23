"""
Ресурси MCP: схема БД `aerodefences`.
Грань RESOURCES — сервер віддає LLM опис реальних таблиць/колонок.

Схема БД змінюється лише міграціями, тож будуємо її ОДИН раз на процес і далі
віддаємо з кешу (наступні виклики не б'ють у БД). Оновлення схеми підхопиться
при рестарті процесу. Категорії НЕ кешуємо (там `COUNT(products)` змінюється
після write — див. REFACTOR_PLAN.md).
"""

import json

from ad_config import mcp
from ad_db import DB_CONFIG, query

# Кеш зібраного JSON схеми на час життя процесу (None = ще не будували).
_schema_cache: str | None = None


@mcp.resource("resource://schema")
async def schema() -> str:
    """Схема БД aerodefences: таблиці та їхні колонки.
    Ресурс для LLM — щоб вона оперувала реальними полями, а не вигаданими.
    Результат кешується на час життя процесу (схема статична між міграціями)."""
    global _schema_cache
    if _schema_cache is not None:
        return _schema_cache

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
    _schema_cache = json.dumps({"database": DB_CONFIG["database"], "tables": tables}, indent=2)
    return _schema_cache

"""
Ресурси MCP: схема БД `aerodefences`.
Грань RESOURCES — сервер віддає LLM опис реальних таблиць/колонок.
"""

import json

from ad_config import mcp
from ad_db import DB_CONFIG, query


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

"""Тести MCP-сервера aerodefences.

Покривають: read-інструменти, ресурс схеми, RAG, RBAC-бар'єр,
моніторинг (healthcheck/metrics) та net-zero write round-trip.

Тести НЕ прив'язані до конкретних назв продуктів: потрібний slug беремо
з БД у рантаймі. Тому вони проходять і на реальній локальній БД, і на
фіктивному seed (db/init.sql) у CI.
"""

import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import server_aerodefences as srv


def _text(result):
    return result.content[0].text


def _data(result):
    """Структурований результат; FastMCP загортає не-dict у {"result": ...}."""
    if result.structured_content is not None:
        sc = result.structured_content
        if isinstance(sc, dict) and set(sc.keys()) == {"result"}:
            return sc["result"]
        return sc
    return json.loads(_text(result))


async def _any_slug() -> str:
    """Будь-який наявний slug продукту (data-agnostic)."""
    rows = await srv.query("SELECT slug FROM products ORDER BY id LIMIT 1")
    return rows[0]["slug"]


# ── Базові read-інструменти ─────────────────────────────────────────────
async def test_list_tools_and_resource(client_accept):
    async with client_accept as c:
        tools = {t.name for t in await c.list_tools()}
        for name in ["list_products", "get_product", "ask_catalog",
                     "healthcheck", "set_product_status", "whoami"]:
            assert name in tools
        resources = {str(r.uri) for r in await c.list_resources()}
        assert "resource://schema" in resources


async def test_list_products_returns_rows(client_accept):
    async with client_accept as c:
        rows = _data(await c.call_tool("list_products", {"limit": 5}))
        assert isinstance(rows, list) and len(rows) > 0
        assert "name" in rows[0]


async def test_catalog_stats(client_accept):
    async with client_accept as c:
        assert _data(await c.call_tool("catalog_stats", {}))


# ── RAG ─────────────────────────────────────────────────────────────────
async def test_ask_catalog_returns_grounded_hits(client_accept):
    async with client_accept as c:
        data = _data(await c.call_tool(
            "ask_catalog", {"question": "що працює по CAN шині", "k": 3}))
        assert data["hits"] > 0
        assert "grounding_note" in data
        top = data["results"][0]
        assert {"doc_id", "source", "title", "score", "snippet"} <= set(top)


async def test_rag_index_covers_both_sources(client_accept):
    async with client_accept as c:
        st = _data(await c.call_tool("rebuild_rag_index", {}))
        assert st["ready"] is True
        assert st["sources"]["db"] > 0     # дані з БД
        assert st["sources"]["file"] > 0   # локальні файли knowledge/


# ── Моніторинг ──────────────────────────────────────────────────────────
async def test_healthcheck_ok(client_accept):
    async with client_accept as c:
        data = _data(await c.call_tool("healthcheck", {}))
        assert data["status"] == "ok"
        assert data["database"]["reachable"] is True


# ── RBAC ────────────────────────────────────────────────────────────────
async def test_viewer_cannot_write(set_role):
    """Роль viewer не має права на write — відмова ДО elicitation."""
    set_role("viewer")
    slug = await _any_slug()
    async with Client(srv.mcp) as c:
        with pytest.raises(ToolError):
            await c.call_tool("update_stock", {"slug": slug, "quantity": 5})


async def test_editor_cannot_change_compliance(set_role):
    """set_compliance вимагає admin; editor отримує відмову."""
    set_role("editor")
    slug = await _any_slug()
    async with Client(srv.mcp) as c:
        with pytest.raises(ToolError):
            await c.call_tool("set_compliance", {"slug": slug, "ndaa": True})


async def test_whoami_reports_role(set_role, client_accept):
    set_role("editor")
    async with client_accept as c:
        data = _data(await c.call_tool("whoami", {}))
        assert data["role"] == "editor"


# ── Write round-trip (net-zero) ──────────────────────────────────────────
async def test_update_stock_roundtrip(client_accept):
    slug = await _any_slug()
    async with client_accept as c:
        before = _data(await c.call_tool("get_product", {"slug": slug}))
        prod = before if isinstance(before, dict) else before[0]
        orig = prod.get("stock_quantity")
        new_val = (orig or 0) + 7

        writes0 = srv.METRICS["writes_committed"]
        await c.call_tool("update_stock", {"slug": slug, "quantity": new_val})
        assert srv.METRICS["writes_committed"] == writes0 + 1

        after = _data(await c.call_tool("get_product", {"slug": slug}))
        aprod = after if isinstance(after, dict) else after[0]
        assert aprod.get("stock_quantity") == new_val

        # повертаємо назад — net zero
        await c.call_tool("update_stock", {"slug": slug, "quantity": orig or 0})


async def test_decline_does_not_write(client_decline):
    """Якщо користувач відхилив підтвердження — у БД без змін."""
    slug = await _any_slug()
    async with client_decline as c:
        writes0 = srv.METRICS["writes_committed"]
        res = await c.call_tool("update_stock", {"slug": slug, "quantity": 999})
        assert "Скасовано" in _text(res) or "підтвердження" in _text(res)
        assert srv.METRICS["writes_committed"] == writes0

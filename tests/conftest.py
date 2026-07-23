"""Спільні фікстури для тестів MCP-сервера.

Використовуємо in-process клієнт FastMCP: `Client(mcp)` піднімає сервер у
тому ж процесі (без stdio-підпроцесу), тож тести швидкі й детерміновані.
Потрібна піднята MySQL (`add-mysql-1` на 127.0.0.1:3306) — ті самі креденшели,
що й у застосунку (див. .env / змінні оточення в CI).
"""

import pathlib
import sys

import pytest
from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult

# щоб імпортувати server_aerodefences.py з кореня репозиторію
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import server_aerodefences as srv  # noqa: E402


async def _auto_accept(message, response_type, params, context):
    """Автопідтвердження write-операцій у тестах ('yes')."""
    if response_type is None:
        return ElicitResult(action="accept")
    return response_type(value="yes")


async def _auto_decline(message, response_type, params, context):
    """Завжди відхиляє — для тесту, що без згоди запису не відбувається."""
    return ElicitResult(action="decline")


@pytest.fixture
def server():
    """Доступ до модуля сервера (mcp, METRICS, тощо)."""
    return srv


@pytest.fixture
def set_role(monkeypatch):
    """Фабрика: встановити роль поточного процесу через ADD_ROLE."""
    def _set(role: str):
        monkeypatch.setenv("ADD_ROLE", role)
    return _set


@pytest.fixture
def client_accept():
    """Клієнт, що автоматично підтверджує elicitation."""
    return Client(srv.mcp, elicitation_handler=_auto_accept)


@pytest.fixture
def client_decline():
    """Клієнт, що відхиляє elicitation."""
    return Client(srv.mcp, elicitation_handler=_auto_decline)


@pytest.fixture(autouse=True)
def _default_admin(monkeypatch):
    """За замовчуванням тести йдуть з роллю admin (окремі тести перевизначають)."""
    monkeypatch.setenv("ADD_ROLE", "admin")

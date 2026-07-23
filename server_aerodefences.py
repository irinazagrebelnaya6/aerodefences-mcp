"""
AeroDefences MCP — точка входу (агрегатор).

Історично весь сервер жив в одному файлі. Тепер логіку розкладено на модулі за
відповідальністю, а цей файл лише збирає їх докупи й запускає транспорт:

    ad_config.py      — env, константи, RBAC-рівні, логування, JWT-authn, `mcp`
    ad_metrics.py     — лічильники (in-memory + Prometheus)
    ad_db.py          — пул зʼєднань, read/write-обгортки, таймаути, retry
    ad_security.py    — визначення ролі, бар'єр доступу, підтвердження
    ad_resources.py   — ресурс `resource://schema`
    ad_prompts.py     — prompt `compliance_report`
    ad_tools_read.py  — read-інструменти + контекст/транспорт/моніторинг
    ad_tools_write.py — write-інструменти + «кошик» (стан сесії)
    ad_tools_rag.py   — RAG (`ask_catalog`, `rebuild_rag_index`)

Імпорт модулів `ad_*` нижче має побічний ефект — реєстрацію інструментів,
ресурсів і prompt-ів на спільному екземплярі `mcp`. Точка входу лишилась
`server_aerodefences.py`, тож `.mcp.json`, Docker, CI і тести не змінюються.
"""

import os

from prometheus_client import start_http_server as prom_start_http_server

# Реєстрація інструментів/ресурсів/prompt-ів на `mcp` (імпорт заради side-effect).
import ad_prompts  # noqa: F401,E402
import ad_resources  # noqa: F401,E402
import ad_tools_rag  # noqa: F401,E402
import ad_tools_read  # noqa: F401,E402
import ad_tools_write  # noqa: F401,E402
from ad_config import TRANSPORT, log, mcp
from ad_db import query
from ad_metrics import METRICS

# Публічний API модуля: те, чим користуються тести/харнеси
# (`server_aerodefences.mcp / .query / .METRICS`).
__all__ = ["mcp", "query", "METRICS"]


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

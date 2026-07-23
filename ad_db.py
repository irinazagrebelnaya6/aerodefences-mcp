"""
Доступ до БД, згрупований у клас `Database`: конфіг підключення, пул зʼєднань,
read/write-обгортки, таймаути, retry (лише read) і нотифікація про зміну каталогу.

Read і write ходять у MySQL ЄДИНИМ шляхом (через пул одного екземпляра
`Database`). Module-рівневі `query` / `_run_write` / `DB_CONFIG` — тонкі аліаси
над синглтоном `db` для сумісності з інструментами й тестами.
"""

import asyncio
import os
import time

import pymysql
import pymysql.err
from dbutils.pooled_db import PooledDB
from fastmcp.server.dependencies import get_context
from mcp.types import ResourceListChangedNotification
from pymysql.cursors import DictCursor

from ad_config import log
from ad_metrics import metrics

# --- Налаштування підключення ---
# Креденшели (user/password) читаються ВИКЛЮЧНО з .env і НЕ зберігаються в коді.
# Локально: скопіювати .env.example -> .env і підставити власні значення.
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


class Database:
    """Пул зʼєднань до MySQL + read/write-обгортки. Один екземпляр на процес.
    Пул створюється ліниво — на імпорті модуля до БД НЕ конектимось."""

    def __init__(self, config: dict, pool_size: int | None = None) -> None:
        self._config = config
        self._pool_size = pool_size or int(os.getenv("ADD_DB_POOL_SIZE", "5"))
        self._pool: PooledDB | None = None

    def _get_pool(self) -> PooledDB:
        if self._pool is None:
            self._pool = PooledDB(
                creator=pymysql,
                maxconnections=self._pool_size,
                mincached=1,
                blocking=True,      # чекати вільне зʼєднання, а не падати
                ping=1,             # пінгувати зʼєднання перед видачею (реконект стейл)
                cursorclass=DictCursor,
                **self._config,
            )
        return self._pool

    def _retry_read(self, fn, attempts: int = 3, base_delay: float = 0.1):
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

    def _run_query(self, sql: str, params: tuple = ()) -> list[dict]:
        """Синхронний запит до MySQL через пул. Повертає список рядків як dict."""
        def _do():
            conn = self._get_pool().connection()  # зі спільного пулу; close() повертає у пул
            try:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    return cur.fetchall()
            finally:
                conn.close()

        return self._retry_read(_do)

    async def query(self, sql: str, params: tuple = ()) -> list[dict]:
        """Async-обгортка: виконує блокуючий запит у окремому потоці."""
        return await asyncio.to_thread(self._run_query, sql, params)

    async def run_write(self, sql: str, params: tuple = ()) -> int:
        """Синхронний INSERT/UPDATE/DELETE. Повертає кількість змінених рядків."""
        def _do():
            conn = self._get_pool().connection()  # зі спільного пулу (ping=1 реконектить стейл)
            try:
                with conn.cursor() as cur:
                    affected = cur.execute(sql, params)
                conn.commit()
                return affected
            finally:
                conn.close()

        affected = await asyncio.to_thread(_do)
        if affected:
            metrics.record_commit()
            log.info("write committed: affected=%s", affected)

        # Грань NOTIFICATIONS: якщо каталог реально змінився — сповіщаємо клієнта,
        # щоб він розумів, що раніше прочитані дані застаріли. Робиться в одному
        # місці, тому спрацьовує для ВСІХ write-знарядь, що ходять через run_write.
        if affected:
            try:
                ctx = get_context()
                await ctx.send_notification(ResourceListChangedNotification())
                await ctx.info("catalog changed -> notification sent",
                               extra={"affected": affected})
            except RuntimeError:
                pass  # немає активного MCP-контексту (виклик поза сесією)

        return affected


# Синглтон на процес + module-рівневі аліаси для сумісності з інструментами/тестами.
db = Database(DB_CONFIG)
query = db.query
_run_write = db.run_write

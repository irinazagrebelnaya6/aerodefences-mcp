# Production Readiness — AeroDefences MCP Server

Чекліст переведення `server_aerodefences.py` (FastMCP над MySQL) із навчального
зразка в стан, придатний до реального прод-деплою (HTTP-транспорт, кілька клієнтів,
кілька реплік).

Легенда статусів: ✅ зроблено · 🚧 у роботі · 📄 документовано (впровадження — окремий крок) · ⬜ заплановано.

> **Принцип сумісності.** Локальний `stdio`-режим (Claude Code / REPL / тести) лишається
> робочим. Прод-механізми (JWT-authn, пул, TLS, Redis, метрики) вмикаються лише коли
> `ADD_TRANSPORT=http` та задані відповідні env-змінні; без них поведінка деградує до
> безпечного локального дефолту.

---

## 🔴 Critical — без цього в прод не можна

| # | Проблема | Рішення | Статус |
|---|----------|---------|--------|
| 1.1 | Дефолт ролі `admin` (fail-open) | Дефолт `viewer`; dev-зручність лише для stdio через `ADD_DEV_ROLE` | ✅ |
| 1.2 | Роль береться з клієнтського `meta.role` (обхід RBAC) | Прибрати; роль лише з перевіреного джерела на сервері | ✅ |
| 1.3 | HTTP без автентифікації | JWT-authn через FastMCP `JWTVerifier`; роль із claims/scopes токена | ✅ |
| 1.4 | Секрети в `docker-compose.yml` відкритим текстом | Винести в secrets / `.env` (не в git); прибрати дефолтні паролі | ✅ |

## 🟠 Should — надійність під навантаженням

| # | Проблема | Рішення | Статус |
|---|----------|---------|--------|
| 2.1 | Нове зʼєднання MySQL на кожен запит | Пул `DBUtils.PooledDB` на процес (`_get_pool`) | ✅ |
| 2.2 | Немає таймаутів/TLS до БД | `connect/read/write_timeout` + `ssl` у `DB_CONFIG` | ✅ |
| 2.3 | `limit` без стелі | `min(limit, MAX_LIMIT)` у read-інструментах | ✅ |
| 2.4 | Сирі винятки на транзієнтних збоях БД | Retry/backoff на read (`_retry_read`); write — через `ping=1` пулу | ✅ |
| 3.1 | `healthcheck` віддає сирий `str(e)` (витік деталей) | Нейтральне `database unavailable`, деталі — в лог | ✅ |

## 🔵 Should — збірка та деплой

| # | Проблема | Рішення | Статус |
|---|----------|---------|--------|
| 4.1 | Контейнер працює від root | `USER app` (uid 10001) у Dockerfile | ✅ |
| 4.2 | Нерепродуковані білди (плаваючі версії) | Install з `requirements.lock` (`--require-hashes`) | ✅ |
| 4.3 | У compose немає healthcheck/restart/лімітів для `mcp` | Додано healthcheck (/metrics), `restart`, ліміти CPU/RAM | ✅ |
| 4.4 | Seed = схема (тестові дані в прод) | Попередження в compose + міграції окремим кроком | 📄 |

## 🟢 Nice — спостережуваність та масштабування

| # | Проблема | Рішення | Статус |
|---|----------|---------|--------|
| 5.1 | Метрики in-memory (фрагментуються між репліками) | `prometheus-client` дзеркалить METRICS, `/metrics` на `ADD_METRICS_PORT` | ✅ |
| 5.2 | Сесійний стан у памʼяті процесу | Redis (`ADD_REDIS_URL`), фолбек — памʼять сесії | ✅ |
| 5.3 | RAG-індекс перебудовується лише в 1 процесі | Лінива побудова на репліку + документована стратегія (нижче) | 📄 |
| 5.4 | Плоскі логи в stderr | JSON-формат (`ADD_LOG_FORMAT=json`) | ✅ |

---

## Нові змінні оточення

| Змінна | Призначення | Дефолт |
|--------|-------------|--------|
| `ADD_ROLE` | Роль для stdio/локального режиму | `viewer` |
| `ADD_DEV_ROLE` | Підвищена роль лише для stdio-dev | — |
| `ADD_JWT_JWKS_URI` / `ADD_JWT_PUBLIC_KEY` | Джерело ключів для перевірки JWT | — |
| `ADD_JWT_ISSUER` / `ADD_JWT_AUDIENCE` | Валідація issuer/audience | — |
| `ADD_DB_POOL_SIZE` | Розмір пулу зʼєднань | `5` |
| `ADD_DB_CONNECT_TIMEOUT` / `ADD_DB_READ_TIMEOUT` | Таймаути до БД (сек) | `5` / `10` |
| `ADD_DB_SSL_CA` | Шлях до CA-сертифіката для TLS до БД | — |
| `ADD_REDIS_URL` | Спільне сховище сесійного стану | — |
| `ADD_MAX_LIMIT` | Стеля на `limit` у read-інструментах | `200` |

---

## Верифікація

1. `.venv/bin/python -m pytest -q` — тести проходять; stdio-REPL/`client_aerodefences.py` працюють.
2. Без `ADD_ROLE` сервер стартує як `viewer`; write → `PermissionError`.
3. HTTP без валідного JWT → відмова; роль береться з claims; `meta.role` не впливає.
4. Паралельне навантаження без вичерпання конектів; healthcheck при недоступній БД → `degraded` швидко, без витоку.
5. `docker compose up --build` — `mcp` non-root, healthcheck зелений, секрети не в compose.
6. `list_products(limit=100000)` фактично обмежено `ADD_MAX_LIMIT`.

# AeroDefences MCP — курсовий проєкт

MCP-сервіс (FastMCP) над каталогом компонентів для БПЛА: політні контролери,
сенсори, навігація, живлення, оптичне/теплове payload. Сервер публікує для LLM
інструменти читання/зміни каталогу, RAG-пошук по даних і локальних політиках,
контроль доступу (RBAC), підтвердження небезпечних дій та моніторинг.

RAG-пошук має **три взаємозамінні бекенди** (`ADD_RAG_BACKEND`): TF-IDF
(офлайн-дефолт), семантичні **embeddings через Voyage AI**, та **vector-DB
Qdrant** (ANN-пошук в окремому контейнері). Побудовано за
мікросервісною архітектурою для LLM: **Sidecar** (окремий контейнер
[`sidecar/`](sidecar/) з клієнтом Voyage + кешем, mcp ходить по HTTP),
**Semantic Cache** (Redis) і **Event-Driven** інвалідація на write. Деталі —
[`RAG_EMBEDDINGS_PLAN.md`](RAG_EMBEDDINGS_PLAN.md) §4.

<!-- Після створення репо додати справжній URL, і бейдж почне показувати статус CI:
![CI](https://github.com/<user>/<repo>/actions/workflows/ci.yml/badge.svg) -->

> **Бізнес-сценарій.** Корпоративна Q&A + керування каталогом: менеджер природною
> мовою питає про товари, наявність, сумісність і відповідність (NDAA / Made in
> USA), а також безпечно вносить зміни (ціна, склад, статус, тексти, FAQ) — усе
> через LLM-агента поверх MCP.

---

## 📦 Артефакти курсового

| Артефакт | Де |
|---|---|
| **Код MCP-сервера** | [`server_aerodefences.py`](server_aerodefences.py), [`rag_index.py`](rag_index.py), [`ad_embeddings.py`](ad_embeddings.py) |
| **Технічна документація** | цей `README.md` + [`ARCHITECTURE.md`](ARCHITECTURE.md) |
| **Архітектурна схема** | **актуальна** — Mermaid у [ARCHITECTURE.md §6](ARCHITECTURE.md) (рендериться на GitHub) · редагована [`architecture.drawio`](architecture.drawio) |
| **Prompt Book** | [`PROMPT_BOOK.md`](PROMPT_BOOK.md) — системні промпти + guardrails |
| **Демонстрація** | [`DEMO.md`](DEMO.md) — сценарій + `🎥 <ВСТАВ_ПОСИЛАННЯ_НА_ВІДЕО>` |
| **Тести / CI** | [`tests/`](tests/), [`.github/workflows/ci.yml`](.github/workflows/ci.yml) |

---

## 🏗 Архітектура (огляд)

> **Актуальна схема — Mermaid у [ARCHITECTURE.md §6](ARCHITECTURE.md)** (рендериться
> прямо на GitHub і містить оновлений стек: embeddings-бекенд, Semantic Cache,
> Event-Driven). Редагована версія — [`architecture.drawio`](architecture.drawio).

Агент (LLM-хост) звертається до MCP-сервера, який маршрутизує запити до
READ/WRITE-інструментів, RAG-retriever'а (TF-IDF / Voyage embeddings) та памʼяті
сесії; write-и проходять барʼєр RBAC + підтвердження і скидають Semantic Cache.
Джерела — MySQL і локальні файли `knowledge/*.md`. Розгортання — Docker/compose,
перевірка — GitHub Actions CI.

---

## 🗺 Відповідність вимогам курсового

| Вимога (етап) | Реалізація |
|---|---|
| Логіка LLM + prompting | `PROMPT_BOOK.md`: системний промпт, стратегії маршрутизації, few-shot |
| Інтеграція з зовнішніми даними | MySQL + **RAG** (`ask_catalog`) над БД і локальними файлами; embeddings через Voyage AI |
| Мікросервісна архітектура LLM | **Sidecar** (контейнер `embeddings/` — Voyage-клієнт + кеш), **Semantic Cache** (Redis), **Event-Driven** інвалідація — `RAG_EMBEDDINGS_PLAN.md` §4 |
| Context / Memory / Routing | `ctx` (логи/elicit/progress), стан сесії (selection-cart), маршрутизація за описами |
| Підключення джерел | БД MySQL · локальні файли `knowledge/*.md` · клієнтські `meta` |
| Бізнес-сценарій | Q&A + керування каталогом БПЛА |
| Безпека | RBAC (viewer/editor/admin), elicitation, білий список полів, секрети в env |
| Інфраструктура | Docker + compose, GitHub Actions CI, логування, `healthcheck`/`metrics` |

---

## 🧱 Можливості сервера (32 інструменти + 1 ресурс + 1 prompt)

**READ:** `list_products`, `find_products`, `get_product`, `list_categories`,
`get_category`, `search_specs`, `get_faqs`, `related_products`, `catalog_stats`,
`low_stock`, `find_products_by_price`, `export_specs` (progress).

**WRITE (з підтвердженням + RBAC):** `set_product_status`, `update_price`,
`update_stock`, `set_compliance` (admin), `update_product_field` (білий список),
`add_spec`, `add_faq`, `reorder_product`, `bulk_set_status` (admin).

**RAG:** `ask_catalog`, `rebuild_rag_index` (бекенд TF-IDF / Voyage embeddings + semantic cache).
**Стан/пам'ять:** `select_products`, `add_to_selection`, `get_selection`,
`clear_selection`, `apply_status_to_selection` (admin).
**Контекст/моніторинг:** `whoami`, `catalog_report`, `healthcheck`, `metrics`.
**Ресурс:** `resource://schema`. **Prompt:** `compliance_report`.

---

## 🔐 Безпека (RBAC)

Роль визначається сервером залежно від транспорту: у HTTP — з перевіреного JWT
(claim `role` / scopes), у stdio — зі змінної `ADD_ROLE`. Клієнтські `meta` на
роль НЕ впливають.

| Роль | Права |
|---|---|
| `viewer` | лише читання |
| `editor` | + звичайні write (ціна, склад, статус, тексти, FAQ) |
| `admin` | + compliance-прапорці та масові дії |

Дефолт — `viewer` (deny-by-default); для локального stdio-dev роль піднімається
через `ADD_DEV_ROLE`. Деталі guardrails — у [`PROMPT_BOOK.md`](PROMPT_BOOK.md).

---

## 🚀 Запуск

### Варіант 1 — усе в Docker (найпростіше, відтворювано)

```bash
docker compose up --build
# MySQL із seed db/init.sql -> 127.0.0.1:3307
# MCP HTTP-сервер           -> 127.0.0.1:8000
```

### Варіант 2 — локально (venv + наявна MySQL)

```bash
cp .env.example .env        # за потреби відредагувати креденшели
uv venv && uv pip install -e ".[dev]"   # або pip install -e ".[dev]"

# сценарний harness (повний автопрогін без LLM)
.venv/bin/python client_aerodefences.py
# інтерактивний REPL (живий elicitation)
.venv/bin/python repl_aerodefences.py
```

### Тести та лінт

```bash
.venv/bin/ruff check .
.venv/bin/python -m pytest -q      # потрібна піднята MySQL із даними
```

### Підключення до справжнього Claude Code

Через [`.mcp.json`](.mcp.json) (project-scope). Тоді роль клієнта грає жива LLM:

```bash
# із кореня репозиторію:
claude mcp add aerodefences -s project -- \
  ./.venv/bin/python \
  ./server_aerodefences.py
```

---

## 🗂 Структура репозиторію

```
server_aerodefences.py   # точка входу-агрегатор (реєструє модулі, запускає транспорт)
ad_config.py ad_db.py ad_security.py ad_metrics.py      # інфраструктура (config / БД / RBAC / метрики)
ad_resources.py ad_prompts.py                           # ресурс schema / prompt compliance_report
ad_tools_read.py ad_tools_write.py ad_tools_rag.py      # інструменти (read+моніторинг / write+кошик / RAG)
rag_index.py             # RAG-retriever + фасад бекендів (TF-IDF/Voyage) над БД + knowledge/
ad_embeddings.py voyage_client.py  # семантичний бекенд + легкий клієнт Voyage AI
ad_semcache.py           # Semantic Cache (Redis) для ask_catalog + інвалідація
ad_qdrant.py             # vector-DB бекенд (Qdrant, ANN-пошук по REST)
ad_summarize.py          # in-memory summarization (стискання фрагментів перед LLM)
sidecar/                 # Sidecar-контейнер embeddings-proxy (свій Dockerfile)
knowledge/*.md           # локальне джерело знань для RAG (політики, глосарій)
client_aerodefences.py   # harness-клієнт (тест без LLM)
repl_aerodefences.py     # інтерактивний REPL
tests/                   # pytest: read/RAG/embeddings/semcache/RBAC/monitoring/round-trip
db/init.sql              # знеособлений seed БД (для compose + CI)
Dockerfile, docker-compose.yml, .github/workflows/ci.yml
RAG_EMBEDDINGS_PLAN.md   # план + мікросервісні патерни LLM (Sidecar/Cache/Event-Driven)
PROMPT_BOOK.md, ARCHITECTURE.md, DEMO.md, architecture.drawio
```

---

## ⚙️ Змінні оточення

| Змінна | Призначення | Дефолт |
|---|---|---|
| `ADD_DB_HOST/PORT/USER/PASSWORD/NAME` | підключення до MySQL | 127.0.0.1:3307 |
| `ADD_ROLE` | роль доступу для stdio (`viewer/editor/admin`) | `viewer` |
| `ADD_TRANSPORT` | `stdio` або `http` | `stdio` |
| `ADD_HTTP_HOST/PORT` | адреса для HTTP-транспорту | `0.0.0.0:8000` |
| `ADD_LOG_LEVEL` | рівень логів | `INFO` |

> ℹ️ У документах і seed-даних назви продуктів **фіктивні** (для публічної здачі).
> Локальний код працює проти реальної БД як є.

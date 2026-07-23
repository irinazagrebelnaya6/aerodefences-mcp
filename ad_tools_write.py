"""
WRITE-інструменти каталогу + «кошик вибраних продуктів» (стан сесії).

Кожен запис проходить бар'єр `_confirm` (RBAC + підтвердження людини) і йде
через Repository-шар (`product_repo` / `category_repo` / `faq_repo`), який
інкапсулює SQL та commit. Стан вибору живе в Redis (`ADD_REDIS_URL`) або, як
фолбек, у памʼяті сесії процесу.
"""

import asyncio
import json
import os

from fastmcp.dependencies import CurrentContext
from fastmcp.server.context import Context
from fastmcp.server.elicitation import (
    AcceptedElicitation,
    CancelledElicitation,
    DeclinedElicitation,
)

from ad_config import (
    ALLOWED_STATUSES,
    ALLOWED_UPDATE_FIELDS,
    SELECTION_KEY,
    SELECTION_TTL,
    mcp,
)
from ad_repositories import category_repo, faq_repo, product_repo
from ad_security import _confirm, _require_role


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
    rows = await product_repo.get_id_name_status(slug)
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
    affected = await product_repo.set_status(product["id"], status)
    await ctx.info(
        "set_product_status done",
        extra={"slug": slug, "from": current, "to": status, "affected": affected},
    )
    return f"OK: '{product['name']}' {current} -> {status} (змінено рядків: {affected})"


async def _get_product_row(slug: str) -> dict:
    """Повертає базовий рядок продукту або кидає ValueError."""
    rows = await product_repo.get_row(slug)
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

    cur_desc = f"{p['price']} {p['currency']}"
    new_desc = f"{price} {p['currency']}"
    if currency is not None:
        if len(currency) != 3:
            raise ValueError("Код валюти має бути 3 символи (напр. 'USD')")
        new_desc = f"{price} {currency.upper()}"

    ok, reason = await _confirm(
        ctx, f"Змінити ціну '{p['name']}' з {cur_desc} на {new_desc}?"
    )
    if not ok:
        return reason

    n = await product_repo.set_price(p["id"], price, currency)
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

    n = await product_repo.set_stock(p["id"], quantity)
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

    changes = []
    if ndaa is not None:
        changes.append(f"NDAA {bool(p['is_ndaa_compliant'])} -> {ndaa}")
    if made_in_usa is not None:
        changes.append(f"Made in USA {bool(p['is_made_in_usa'])} -> {made_in_usa}")

    ok, reason = await _confirm(
        ctx, f"Змінити для '{p['name']}': {'; '.join(changes)}?",
        min_role="admin",  # compliance — юридично значимо, лише admin
    )
    if not ok:
        return reason

    n = await product_repo.set_compliance(p["id"], ndaa, made_in_usa)
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

    # field вже перевірено по білому списку — безпечно передати в репозиторій
    n = await product_repo.set_field(p["id"], field, value)
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

    ok, reason = await _confirm(
        ctx, f"Додати spec до '{p['name']}': [{spec_group}] {spec_name} = {spec_value!r}?"
    )
    if not ok:
        return reason

    n = await product_repo.add_spec(p["id"], spec_group, spec_name, spec_value)
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

    ok, reason = await _confirm(
        ctx, f"Додати FAQ до '{p['name']}'?\nQ: {question}\nA: {answer[:120]}"
    )
    if not ok:
        return reason

    n = await faq_repo.add(p["id"], question, answer)
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

    n = await product_repo.set_sort_order(p["id"], sort_order)
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
    cats = await category_repo.get_id_name(category)
    if not cats:
        raise ValueError(f"Категорію зі slug '{category}' не знайдено")
    cat = cats[0]

    cnt = await product_repo.count_in_category_not_status(cat["id"], status)
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

    n = await product_repo.bulk_set_status_by_category(cat["id"], status)
    await ctx.info("bulk_set_status done",
                   extra={"category": category, "status": status, "affected": n})
    return f"OK: у категорії '{cat['name']}' переведено {n} товар(ів) у '{status}'."


# ────────────────────────────────────────────────────────────────────────
# Грань STATE: «кошик вибраних продуктів» у межах сесії.
# Дозволяє: вибрати продукти одним запитом, а наступним — діяти над ними,
# не перелічуючи їх знову.
#
# Спільне сховище стану сесій. Якщо задано ADD_REDIS_URL — вибір живе в Redis
# (працює за кількох реплік/без sticky-sessions); інакше фолбек на памʼять сесії
# процесу (ctx.set_state) — зручно локально й у тестах.
# ────────────────────────────────────────────────────────────────────────
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
        rows = await product_repo.get_id_name_slug(s)
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
    rows = await product_repo.get_id_name_slug(slug)
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
    масовим записом питаємо підтвердження. Нотифікація спрацює через run_write."""
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
    n = await product_repo.bulk_set_status_by_ids(ids, status)
    await ctx.info("apply_status_to_selection done",
                   extra={"status": status, "affected": n})
    return f"OK: {n} вибраних продуктів переведено у '{status}'."

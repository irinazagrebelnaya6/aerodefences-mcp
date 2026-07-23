# План розвитку: нові інструменти MCP над БД `aerodefences`

Ідеї нових знарядь для сервера `server_aerodefences.py` — щоб LLM могла
**читати** й **змінювати** дані в базі. Усе будується на наявній обв'язці:
`_run_query` (read), `_run_write` + `ctx.elicit` (write з підтвердженням).

---

## 📖 READ-інструменти (без підтвердження) — ✅ РЕАЛІЗОВАНО

Усі 8 додано в `server_aerodefences.py` і перевірено на живій БД.

| Інструмент | Що робить | Таблиці | Статус |
|---|---|---|---|
| `list_categories()` | Список категорій + кількість товарів у кожній | `categories` + `COUNT products` | ✅ |
| `get_category(slug)` | Категорія та її товари | `categories`, `products` | ✅ |
| `search_specs(search, limit)` | Пошук за характеристиками («у кого CAN?», «26 TOPS») | `product_specs` | ✅ |
| `get_faqs(slug)` | Тільки FAQ товару | `product_faqs` | ✅ |
| `related_products(slug)` | З чим товар «працює як система» | `product_relations` | ✅ |
| `catalog_stats()` | Зведення: скільки товарів за статусами, категоріями, NDAA/USA | агрегати по `products` | ✅ |
| `low_stock(threshold)` | Товари із залишком нижче порогу (NULL відкинуто) | `products.stock_quantity` | ✅ |
| `find_products_by_price(min_price, max_price)` | Фільтр за ціною (NULL відкинуто) | `products.price` | ✅ |

---

## ✏️ WRITE-інструменти (з `ctx.elicit`, як `set_product_status`) — ✅ РЕАЛІЗОВАНО

Усі 8 додано в `server_aerodefences.py` через спільний хелпер `_confirm`
і перевірено round-trip тестом (зміна → перевірка в БД → відновлення).

| Інструмент | Що змінює | Ризик | Статус |
|---|---|---|---|
| `update_price(slug, price, currency?)` | Ціна товару | 💰 середній | ✅ |
| `update_stock(slug, quantity)` | Залишок на складі | низький | ✅ |
| `set_compliance(slug, ndaa?, made_in_usa?)` | Прапорці NDAA / Made in USA | ⚠️ високий (юридично значимо) | ✅ |
| `update_product_field(slug, field, value)` | Текстове поле — **лише за білим списком `ALLOWED_UPDATE_FIELDS`** | залежить | ✅ |
| `add_spec(slug, spec_group, spec_name, spec_value)` | Додати характеристику | низький | ✅ |
| `add_faq(slug, question, answer)` | Додати FAQ | низький | ✅ |
| `reorder_product(slug, sort_order)` | Позиція у видачі | низький | ✅ |
| `bulk_set_status(category, status)` | Масова зміна статусу за категорією | 🔴 високий (багато рядків за раз) | ✅ |

---

## 🧩 Рецепт додавання (однаковий для всіх)

### READ — мінімальна обв'язка

```python
@mcp.tool
async def list_categories(ctx: Context = CurrentContext()) -> list[dict]:
    await ctx.info("list_categories")
    return await query("""
        SELECT c.slug, c.name, COUNT(p.id) AS products
        FROM categories c
        LEFT JOIN products p ON p.category_id = c.id
        GROUP BY c.id ORDER BY c.name
    """)
```

### WRITE — завжди 4 кроки, як у `set_product_status`

```python
@mcp.tool
async def update_price(slug: str, price: float,
                       ctx: Context = CurrentContext()) -> str:
    # 1) валідація
    if price < 0:
        raise ValueError("Ціна не може бути від'ємною")
    # 2) читаємо поточний стан
    rows = await query("SELECT id, name, price FROM products WHERE slug=%s", (slug,))
    if not rows:
        raise ValueError(f"Продукт '{slug}' не знайдено")
    p = rows[0]
    # 3) БАР'ЄР — підтвердження людини
    confirm = await ctx.elicit(
        f"Змінити ціну '{p['name']}' з {p['price']} на {price}?",
        response_type=["yes", "no"])
    match confirm:
        case AcceptedElicitation(data="yes"): pass
        case _: return "Скасовано."
    # 4) запис
    n = await _run_write("UPDATE products SET price=%s WHERE id=%s", (price, p["id"]))
    return f"OK: {p['name']} price -> {price} (рядків: {n})"
```

---

## ⚠️ Важливі принципи для write-інструментів

- **Білий список полів.** Універсальний `update_product_field` НЕ повинен приймати
  будь-яке ім'я колонки в SQL — інакше це діра (SQL-ін'єкція через ім'я поля +
  випадкове псування). Дозволяй лише явний набір: `{"subtitle", "short_description", ...}`.
- **Завжди `ctx.elicit` перед записом** — це єдиний бар'єр між «LLM щось вирішила»
  і «дані змінилися».
- **Читати перед записом** — показуй у підтвердженні `було → стане`, щоб людина
  бачила, що саме змінюється.
- **Масові операції (`bulk_*`)** — показуй у запиті підтвердження, *скільки рядків*
  зачепить, і логуй `affected`.
- **Небезпечні прапорці (NDAA/Made in USA)** — це не просто дані, а compliance;
  такі писати особливо обережно.

---

## 🎯 Перші кандидати на реалізацію

1. **`list_categories`** + **`catalog_stats`** — читання, безпечно, швидко.
2. **`update_price`** — запис, показує повний патерн із підтвердженням.

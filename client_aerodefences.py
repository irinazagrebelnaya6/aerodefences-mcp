"""
Harness (випробувальний стенд) для server_aerodefences.py.

Піднімає сервер як підпроцес (stdio), викликає його знаряддя й друкує
результат. Використовується для тестування сервера БЕЗ реальної LLM.
За зразком client_logging.py: ловить логи сервера через log_handler.
"""

import asyncio

from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult
from fastmcp.client.logging import LogMessage


async def log_handler(message: LogMessage):
    """Друкує логи, які сервер шле через ctx.info / ctx.debug."""
    msg = message.data.get("msg")
    extra = message.data.get("extra")
    level = message.level.upper()
    if extra:
        print(f"  [SERVER {level}] {msg} | {extra}")
    else:
        print(f"  [SERVER {level}] {msg}")


async def progress_handler(progress: float, total: float | None, message: str | None):
    """Друкує прогрес довгих операцій сервера (ctx.report_progress)."""
    if total:
        print(f"  progress: {progress/total*100:.0f}%")
    else:
        print(f"  progress: {progress}")


async def message_handler(message):
    """Ловить MCP-нотифікації сервера (грань notifications)."""
    if hasattr(message, "root"):
        method = message.root.method
        if method == "notifications/resources/list_changed":
            print("  [NOTIFICATION] каталог змінився -> дані застаріли, треба перечитати")
        elif method == "notifications/tools/list_changed":
            print("  [NOTIFICATION] список знарядь змінився")
        elif method == "notifications/prompts/list_changed":
            print("  [NOTIFICATION] список промптів змінився")


async def elicitation_handler(message, response_type, params, context):
    """Відповідає на запити підтвердження від сервера.
    Інтерактивно читає ввід; якщо stdin недоступний (автотест) — 'yes'."""
    print(f"\n  [SERVER ПИТАЄ] {message}")
    try:
        user_input = input("  yes/no (Enter=decline, 'cancel'=abort) > ").strip()
    except EOFError:
        user_input = "yes"  # автотест без термінала
        print(f"  (немає stdin -> авто-відповідь '{user_input}')")

    if user_input.lower() == "cancel":
        return ElicitResult(action="cancel")
    if user_input == "":
        return ElicitResult(action="decline")
    if response_type is None:
        return ElicitResult(action="accept")
    return response_type(value=user_input)


async def main():
    client = Client(
        "./server_aerodefences.py",
        log_handler=log_handler,
        elicitation_handler=elicitation_handler,
        progress_handler=progress_handler,
        message_handler=message_handler,
    )

    async with client:
        # 1) що вміє сервер
        tools = await client.list_tools()
        print("== TOOLS ==")
        for t in tools:
            print(f"  - {t.name}: {t.description}")

        # 2) виклик знаряддя
        print("\n== call list_products(limit=5) ==")
        result = await client.call_tool("list_products", {"limit": 5})

        print("\n== RESULT ==")
        for row in result.data:
            flags = []
            if row["is_ndaa_compliant"]:
                flags.append("NDAA")
            if row["is_made_in_usa"]:
                flags.append("USA")
            print(f"  #{row['id']:>2} {row['name']:<22} {row['sku']:<16} [{', '.join(flags)}]")

        # 2.5) ресурс: схема БД (грань resources/access)
        print("\n== read resource://schema ==")
        res = await client.read_resource("resource://schema")
        import json as _json
        parsed = _json.loads(res[0].text)
        print(f"  база: {parsed['database']}, таблиць: {len(parsed['tables'])}")
        print(f"  products має колонок: {len(parsed['tables']['products'])}")

        # 2.7) find_products — сценарій "опубліковані НЕ NDAA-сумісні"
        print("\n== find_products(status='published', ndaa_compliant=False) ==")
        found = (await client.call_tool(
            "find_products",
            {"status": "published", "ndaa_compliant": False},
        )).data
        print(f"  знайдено: {len(found)}")
        for row in found:
            print(f"    #{row['id']} {row['name']} ({row['category']})")

        print("\n== find_products(category='sensors') ==")
        sensors = (await client.call_tool(
            "find_products", {"category": "sensors"}
        )).data
        for row in sensors:
            print(f"    #{row['id']} {row['name']} [{row['status']}]")

        # 2.9) export_specs — довга операція з прогресом (грань progress)
        print("\n== call export_specs() (дивись progress) ==")
        exp = (await client.call_tool("export_specs", {})).data
        print(f"  вивантажено: {exp['products']} продуктів, "
              f"{exp['total_specs']} specs усього")

        # 3) повна картка одного продукту
        print("\n== call get_product('battlecore-fc7') ==")
        product = (await client.call_tool("get_product", {"slug": "battlecore-fc7"})).data

        print("\n== RESULT ==")
        print(f"  {product['name']} ({product['sku']})")
        print(f"  {product.get('subtitle') or ''}")
        print(f"  specs={len(product['specs'])}  features={len(product['features'])} "
              f" use_cases={len(product['use_cases'])}  faqs={len(product['faqs'])} "
              f" images={len(product['images'])}")
        if product["specs"]:
            print("  --- перші 3 specs ---")
            for s in product["specs"][:3]:
                print(f"    [{s['spec_group']}] {s['spec_name']}: {s['spec_value']}")
        if product["faqs"]:
            print("  --- перше FAQ ---")
            print(f"    Q: {product['faqs'][0]['question']}")
            print(f"    A: {product['faqs'][0]['answer'][:80]}...")

        # 4) НЕБЕЗПЕЧНА write-операція з підтвердженням (round-trip, дані не псуємо)
        test_slug = "edgenode-ai"
        before = (await client.call_tool("get_product", {"slug": test_slug})).data["status"]
        print(f"\n== set_product_status: round-trip для '{test_slug}' (зараз '{before}') ==")

        print("  -> ставимо 'archived'")
        r1 = await client.call_tool(
            "set_product_status", {"slug": test_slug, "status": "archived"}
        )
        print(f"  RESULT: {r1.data}")

        print(f"  -> повертаємо назад '{before}'")
        r2 = await client.call_tool(
            "set_product_status", {"slug": test_slug, "status": before}
        )
        print(f"  RESULT: {r2.data}")

        # ────────────────────────────────────────────────────────────────
        # 5) НОВІ READ-ЗНАРЯДДЯ (безпечні, лише читання)
        # ────────────────────────────────────────────────────────────────
        async def call(name, args=None):
            return (await client.call_tool(name, args or {})).data

        print("\n== READ: list_categories() ==")
        for c in await call("list_categories"):
            print(f"    {c['name']:<22} товарів: {c['products']}")

        print("\n== READ: catalog_stats() ==")
        stats = await call("catalog_stats")
        print(f"    усього: {stats['total_products']}, за статусами: {stats['by_status']}")
        print(f"    NDAA: {stats['ndaa_compliant']}, Made in USA: {stats['made_in_usa']}")

        print("\n== READ: search_specs('CAN') ==")
        specs = await call("search_specs", {"search": "CAN", "limit": 5})
        for s in specs[:5]:
            print(f"    {s['product']:<18} [{s['spec_group']}] {s['spec_name']}")

        print("\n== READ: get_category('sensors') ==")
        cat = await call("get_category", {"slug": "sensors"})
        print(f"    {cat['name']}: {len(cat['products'])} товар(ів)")

        print("\n== READ: get_faqs('edgenode-ai') ==")
        faqs = await call("get_faqs", {"slug": "edgenode-ai"})
        print(f"    FAQ: {len(faqs)}; перше Q: {faqs[0]['question'] if faqs else '—'}")

        print("\n== READ: related_products('edgenode-ai') ==")
        for r in await call("related_products", {"slug": "edgenode-ai"}):
            print(f"    [{r['relation_type']}] {r['related_name']} ({r['related_slug']})")

        print("\n== READ: low_stock(threshold=10) ==")
        low = await call("low_stock", {"threshold": 10})
        print(f"    товарів із залишком <= 10: {len(low)} "
              f"(0 очікувано, поки stock_quantity = NULL)")

        print("\n== READ: find_products_by_price(min_price=0) ==")
        priced = await call("find_products_by_price", {"min_price": 0})
        print(f"    товарів з ціною: {len(priced)} (0 очікувано, поки price = NULL)")

        # ────────────────────────────────────────────────────────────────
        # 6) НОВІ WRITE-ЗНАРЯДДЯ — round-trip (міняємо → повертаємо назад).
        #    Кожен запис вимагає підтвердження (elicitation). Автотест без
        #    stdin відповідає 'yes'; в терміналі підтверджуєш вручну.
        # ────────────────────────────────────────────────────────────────
        p = await call("get_product", {"slug": test_slug})
        print(f"\n== WRITE round-trip для '{test_slug}' ==")

        # 6.1 update_product_field: subtitle -> demo -> назад
        orig_sub = p["subtitle"]
        print(f"  update_product_field: subtitle {orig_sub!r} -> '[demo]'")
        print("   ", (await call("update_product_field",
              {"slug": test_slug, "field": "subtitle", "value": "[demo]"})))
        print("   ", (await call("update_product_field",
              {"slug": test_slug, "field": "subtitle", "value": orig_sub})), "(відновлено)")

        # 6.2 set_compliance: інвертуємо NDAA -> назад
        ndaa = bool(p["is_ndaa_compliant"])
        print(f"  set_compliance: NDAA {ndaa} -> {not ndaa} -> {ndaa}")
        print("   ", (await call("set_compliance", {"slug": test_slug, "ndaa": not ndaa})))
        print("   ", (await call("set_compliance", {"slug": test_slug, "ndaa": ndaa})), "(відновлено)")

        # 6.3 reorder_product: sort_order -> 99 -> назад
        orig_sort = p["sort_order"]
        print(f"  reorder_product: sort_order {orig_sort} -> 99 -> {orig_sort}")
        print("   ", (await call("reorder_product", {"slug": test_slug, "sort_order": 99})))
        print("   ", (await call("reorder_product", {"slug": test_slug, "sort_order": orig_sort})), "(відновлено)")

        # 6.4 update_price / update_stock — round-trip лише якщо значення НЕ NULL
        #     (через тул не можна повернути NULL, тож не псуємо дані).
        if p["price"] is not None:
            print(f"  update_price: {p['price']} -> 999.99 -> {p['price']}")
            print("   ", (await call("update_price", {"slug": test_slug, "price": 999.99})))
            print("   ", (await call("update_price", {"slug": test_slug, "price": float(p["price"])})), "(відновлено)")
        else:
            print("  update_price: пропущено (price = NULL, нічим відновлювати). Приклад:")
            print('      call update_price {"slug": "edgenode-ai", "price": 1499.00, "currency": "USD"}')

        if p["stock_quantity"] is not None:
            print(f"  update_stock: {p['stock_quantity']} -> 100 -> {p['stock_quantity']}")
            print("   ", (await call("update_stock", {"slug": test_slug, "quantity": 100})))
            print("   ", (await call("update_stock", {"slug": test_slug, "quantity": p["stock_quantity"]})), "(відновлено)")
        else:
            print("  update_stock: пропущено (stock = NULL). Приклад:")
            print('      call update_stock {"slug": "edgenode-ai", "quantity": 50}')

        # 6.5 add_spec / add_faq / bulk_set_status — лише ілюстрація виклику
        #     (немає тулів для видалення/точного відкату, тож не виконуємо,
        #      щоб не лишати «сміття» у БД).
        print("\n== WRITE (лише приклади виклику, без запису) ==")
        print('  call add_spec        {"slug":"edgenode-ai","spec_group":"specification","spec_name":"Weight","spec_value":"45 g"}')
        print('  call add_faq         {"slug":"edgenode-ai","question":"Гарантія?","answer":"12 місяців."}')
        print('  call bulk_set_status {"category":"edge-compute","status":"draft"}   # 🔴 масова дія')


if __name__ == "__main__":
    asyncio.run(main())

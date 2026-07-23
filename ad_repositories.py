"""
Repository-шар: інкапсулює SQL каталогу, прибираючи повтор `query(...)` у
кожному інструменті (п.2) і дублювання SQL (п.6). Інструменти працюють з
`products` (`product_repo`), `categories` (`category_repo`) і `product_faqs`
(`faq_repo`) через методи, а не «сирі» запити.

SQL перенесено дослівно з інструментів — поведінка ідентична. Читання йдуть
через `query`, записи — через `_run_write` (обидва — з `ad_db`).
"""

from ad_db import _run_write, query


class ProductRepository:
    """Доступ до таблиці `products` та повʼязаних (`product_specs`,
    `product_features`, `product_use_cases`, `product_images`,
    `product_relations`)."""

    # ── одиничні lookups за slug (різні набори колонок під різні потреби) ──
    async def get_full_published(self, slug: str) -> list[dict]:
        return await query(
            "SELECT * FROM products WHERE slug = %s AND status = 'published'",
            (slug,),
        )

    async def get_row(self, slug: str) -> list[dict]:
        return await query("SELECT * FROM products WHERE slug = %s", (slug,))

    async def get_id_name(self, slug: str) -> list[dict]:
        return await query("SELECT id, name FROM products WHERE slug = %s", (slug,))

    async def get_id_name_slug(self, slug: str) -> list[dict]:
        return await query("SELECT id, name, slug FROM products WHERE slug = %s", (slug,))

    async def get_id_name_status(self, slug: str) -> list[dict]:
        return await query("SELECT id, name, status FROM products WHERE slug = %s", (slug,))

    # ── списки / пошук ──
    async def list_published(self, limit: int) -> list[dict]:
        return await query(
            """
            SELECT id, name, sku, is_ndaa_compliant, is_made_in_usa, status
            FROM products
            WHERE status = 'published'
            ORDER BY sort_order, id
            LIMIT %s
            """,
            (limit,),
        )

    async def search(
        self,
        status: str | None,
        ndaa_compliant: bool | None,
        made_in_usa: bool | None,
        category: str | None,
        search: str | None,
        limit: int,
        allowed_statuses: tuple,
    ) -> list[dict]:
        where = []
        params: list = []

        if status is not None:
            if status not in allowed_statuses:
                raise ValueError(f"Недопустимий статус '{status}'")
            where.append("p.status = %s")
            params.append(status)

        if ndaa_compliant is not None:
            where.append("p.is_ndaa_compliant = %s")
            params.append(1 if ndaa_compliant else 0)

        if made_in_usa is not None:
            where.append("p.is_made_in_usa = %s")
            params.append(1 if made_in_usa else 0)

        if category is not None:
            where.append("c.slug = %s")
            params.append(category)

        if search is not None:
            where.append("(p.name LIKE %s OR p.sku LIKE %s)")
            params.extend([f"%{search}%", f"%{search}%"])

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        params.append(limit)

        sql = f"""
            SELECT p.id, p.name, p.sku, p.status,
                   p.is_ndaa_compliant, p.is_made_in_usa,
                   c.name AS category
            FROM products p
            LEFT JOIN categories c ON c.id = p.category_id
            {where_sql}
            ORDER BY p.sort_order, p.id
            LIMIT %s
        """
        return await query(sql, tuple(params))

    async def search_specs(self, search: str, limit: int) -> list[dict]:
        return await query(
            """
            SELECT p.name AS product, p.slug, p.status,
                   s.spec_group, s.spec_name, s.spec_value
            FROM product_specs s
            JOIN products p ON p.id = s.product_id
            WHERE s.spec_name LIKE %s OR s.spec_value LIKE %s
            ORDER BY p.sort_order, p.id, s.sort_order
            LIMIT %s
            """,
            (f"%{search}%", f"%{search}%", limit),
        )

    async def low_stock(self, threshold: int) -> list[dict]:
        return await query(
            """
            SELECT id, name, sku, slug, status, stock_quantity
            FROM products
            WHERE stock_quantity IS NOT NULL AND stock_quantity <= %s
            ORDER BY stock_quantity, id
            """,
            (threshold,),
        )

    async def by_price(self, min_price: float | None, max_price: float | None) -> list[dict]:
        where = ["price IS NOT NULL"]
        params: list = []
        if min_price is not None:
            where.append("price >= %s")
            params.append(min_price)
        if max_price is not None:
            where.append("price <= %s")
            params.append(max_price)
        sql = f"""
            SELECT id, name, sku, slug, status, price, currency
            FROM products
            WHERE {' AND '.join(where)}
            ORDER BY price, id
        """
        return await query(sql, tuple(params))

    async def list_published_basic(self) -> list[dict]:
        return await query(
            "SELECT id, name, sku FROM products "
            "WHERE status = 'published' ORDER BY sort_order, id"
        )

    async def related(self, pid: int) -> list[dict]:
        return await query(
            """
            SELECT r.relation_type, r.group_label, r.label,
                   rp.name AS related_name, rp.slug AS related_slug, rp.status
            FROM product_relations r
            LEFT JOIN products rp ON rp.id = r.related_product_id
            WHERE r.product_id = %s
            ORDER BY r.relation_type, r.id
            """,
            (pid,),
        )

    # ── повʼязані таблиці картки товару (за product_id) ──
    async def specs(self, pid: int) -> list[dict]:
        return await query(
            "SELECT spec_group, spec_name, spec_value FROM product_specs "
            "WHERE product_id = %s ORDER BY sort_order, id",
            (pid,),
        )

    async def features(self, pid: int) -> list[dict]:
        return await query(
            "SELECT title, body FROM product_features "
            "WHERE product_id = %s ORDER BY position, id",
            (pid,),
        )

    async def use_cases(self, pid: int) -> list[dict]:
        return await query(
            "SELECT title, subtitle FROM product_use_cases "
            "WHERE product_id = %s ORDER BY sort_order, id",
            (pid,),
        )

    async def images(self, pid: int) -> list[dict]:
        return await query(
            "SELECT url, alt, is_primary FROM product_images "
            "WHERE product_id = %s ORDER BY is_primary DESC, sort_order, id",
            (pid,),
        )

    # ── статистика ──
    async def count_total(self) -> int:
        return (await query("SELECT COUNT(*) AS n FROM products"))[0]["n"]

    async def count_by_status(self) -> list[dict]:
        return await query(
            "SELECT status, COUNT(*) AS n FROM products GROUP BY status ORDER BY status"
        )

    async def compliance_flags(self) -> dict:
        return (
            await query(
                """
                SELECT
                  SUM(is_ndaa_compliant) AS ndaa_compliant,
                  SUM(is_made_in_usa)    AS made_in_usa
                FROM products
                """
            )
        )[0]

    async def count_published_summary(self) -> dict:
        return (await query(
            "SELECT COUNT(*) AS n, SUM(status='published') AS pub FROM products"
        ))[0]

    # ── записи (SQL інкапсульовано; виклик іде після підтвердження в інструменті) ──
    async def set_status(self, pid: int, status: str) -> int:
        return await _run_write(
            "UPDATE products SET status = %s WHERE id = %s", (status, pid)
        )

    async def set_price(self, pid: int, price: float, currency: str | None = None) -> int:
        sets, params = ["price = %s"], [price]
        if currency is not None:
            sets.append("currency = %s")
            params.append(currency.upper())
        params.append(pid)
        return await _run_write(
            f"UPDATE products SET {', '.join(sets)} WHERE id = %s", tuple(params)
        )

    async def set_stock(self, pid: int, quantity: int) -> int:
        return await _run_write(
            "UPDATE products SET stock_quantity = %s WHERE id = %s", (quantity, pid)
        )

    async def set_compliance(self, pid: int, ndaa: bool | None, made_in_usa: bool | None) -> int:
        sets, params = [], []
        if ndaa is not None:
            sets.append("is_ndaa_compliant = %s")
            params.append(1 if ndaa else 0)
        if made_in_usa is not None:
            sets.append("is_made_in_usa = %s")
            params.append(1 if made_in_usa else 0)
        params.append(pid)
        return await _run_write(
            f"UPDATE products SET {', '.join(sets)} WHERE id = %s", tuple(params)
        )

    async def set_field(self, pid: int, field: str, value: str) -> int:
        # `field` перевіряється по білому списку У ВИКЛИКАЧА (ALLOWED_UPDATE_FIELDS).
        return await _run_write(
            f"UPDATE products SET {field} = %s WHERE id = %s", (value, pid)
        )

    async def set_sort_order(self, pid: int, sort_order: int) -> int:
        return await _run_write(
            "UPDATE products SET sort_order = %s WHERE id = %s", (sort_order, pid)
        )

    async def add_spec(self, pid: int, spec_group: str, spec_name: str, spec_value: str) -> int:
        nxt = (await query(
            "SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM product_specs WHERE product_id = %s",
            (pid,),
        ))[0]["n"]
        return await _run_write(
            "INSERT INTO product_specs (product_id, spec_group, spec_name, spec_value, sort_order) "
            "VALUES (%s, %s, %s, %s, %s)",
            (pid, spec_group, spec_name, spec_value, nxt),
        )

    # ── масові дії ──
    async def count_in_category_not_status(self, category_id: int, status: str) -> int:
        return (await query(
            "SELECT COUNT(*) AS n FROM products WHERE category_id = %s AND status <> %s",
            (category_id, status),
        ))[0]["n"]

    async def bulk_set_status_by_category(self, category_id: int, status: str) -> int:
        return await _run_write(
            "UPDATE products SET status = %s WHERE category_id = %s AND status <> %s",
            (status, category_id, status),
        )

    async def bulk_set_status_by_ids(self, ids: list[int], status: str) -> int:
        placeholders = ", ".join(["%s"] * len(ids))
        return await _run_write(
            f"UPDATE products SET status = %s WHERE id IN ({placeholders})",
            tuple([status, *ids]),
        )


class CategoryRepository:
    """Доступ до таблиці `categories`."""

    async def list_with_counts(self) -> list[dict]:
        return await query(
            """
            SELECT c.id, c.slug, c.name, c.sort_order, c.is_visible,
                   COUNT(p.id) AS products
            FROM categories c
            LEFT JOIN products p ON p.category_id = c.id
            GROUP BY c.id
            ORDER BY c.sort_order, c.name
            """
        )

    async def get(self, slug: str) -> list[dict]:
        return await query(
            "SELECT id, slug, name, sort_order, is_visible FROM categories WHERE slug = %s",
            (slug,),
        )

    async def get_id_name(self, slug: str) -> list[dict]:
        return await query("SELECT id, name FROM categories WHERE slug = %s", (slug,))

    async def products_of(self, category_id: int) -> list[dict]:
        return await query(
            """
            SELECT id, name, sku, slug, status, price, currency
            FROM products
            WHERE category_id = %s
            ORDER BY sort_order, id
            """,
            (category_id,),
        )

    async def count_by_category(self) -> list[dict]:
        return await query(
            """
            SELECT c.name AS category, COUNT(p.id) AS n
            FROM categories c
            LEFT JOIN products p ON p.category_id = c.id
            GROUP BY c.id
            ORDER BY n DESC, c.name
            """
        )

    async def count_by_category_named(self) -> list[dict]:
        # Варіант для catalog_report: групування за назвою категорії.
        return await query(
            "SELECT c.name, COUNT(*) AS n FROM products p "
            "LEFT JOIN categories c ON c.id = p.category_id "
            "GROUP BY c.name ORDER BY n DESC"
        )


class FaqRepository:
    """Доступ до таблиці `product_faqs`."""

    async def by_product(self, pid: int) -> list[dict]:
        return await query(
            "SELECT question, answer FROM product_faqs "
            "WHERE product_id = %s ORDER BY sort_order, id",
            (pid,),
        )

    async def add(self, pid: int, question: str, answer: str) -> int:
        nxt = (await query(
            "SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM product_faqs WHERE product_id = %s",
            (pid,),
        ))[0]["n"]
        return await _run_write(
            "INSERT INTO product_faqs (product_id, question, answer, sort_order, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, NOW(), NOW())",
            (pid, question, answer, nxt),
        )


# Синглтони на процес.
product_repo = ProductRepository()
category_repo = CategoryRepository()
faq_repo = FaqRepository()

"""Миграция 003 (S02): демо-данные — data-migration.

Синтетические данные v1 (er-data-model: «при реальных данных — персональные данные»),
объёмы и ловушки зафиксированы в solution/sessions/S02_session.md (Task 4, решение
canon-check FIX-007):
- orders: каждая 12-я строка с campaign_id NULL — ловушка INNER JOIN (ADR-006);
- bonuses только у 7 из 12 сотрудников — ловушка LEFT JOIN … IS NULL («не был премирован»);
- employees.salary — PII-колонка, вне прав, кроме hr_viewer;
- реестр: 5 active-рёбер с семантикой + 1 deprecated с replaced_by (жизненный цикл ADR-006 §4.2);
- settings_version наполняется автоматически триггерами bump_settings_version.

Тот же generate_series-набор на каждом прогоне (детерминированный).
"""

from alembic import op

revision = "s02_0003_demo_data"
down_revision = "s02_0002_roles_hardening"
branch_labels = None
depends_on = None

DEMO_SQL = r"""
-- === Бизнес-данные (синтетика v1) ===
INSERT INTO customers (name, country, created_at)
SELECT 'Customer ' || g,
       (ARRAY['RU', 'KZ', 'DE', 'BY'])[1 + (g % 4)],
       now() - (g || ' days')::interval
FROM generate_series(1, 20) g;

INSERT INTO campaigns (name, channel, started_at) VALUES
    ('Spring Sale', 'ads', DATE '2026-07-01'),
    ('Back to School', 'email', DATE '2026-08-15'),
    ('Autumn Promo', 'ads', DATE '2026-09-10'),
    ('Partner Week', 'partner', DATE '2026-09-20'),
    ('Black November', 'ads', DATE '2026-11-01'),
    ('Winter Warmup', 'email', DATE '2026-11-10');

INSERT INTO products (name, category, price)
SELECT 'Product ' || g,
       (ARRAY['electronics', 'home', 'sports', 'books'])[1 + (g % 4)],
       (100 + (g * 7 % 900))::numeric(12, 2)
FROM generate_series(1, 30) g;

-- 400 заказов; каждая 12-я — вне кампаний (campaign_id NULL ≥ 30 строк: ловушка);
-- total заполняется агрегатом по позициям ниже (ревью Minor-9: «выручка по заказам»
-- и «выручка по позициям» не должны расходиться для бизнес-сцен агента)
INSERT INTO orders (customer_id, campaign_id, order_date, status, total)
SELECT 1 + (g % 20),
       CASE WHEN g % 12 = 0 THEN NULL ELSE 1 + (g % 6) END,
       DATE '2026-09-01' + (g % 75),
       (ARRAY['paid', 'shipped', 'cancelled'])[1 + (g % 3)],
       0
FROM generate_series(1, 400) g;

-- 1–4 позиции на заказ (детерминированно; сумма по заказам = 1000)
INSERT INTO order_items (order_id, product_id, quantity, unit_price)
SELECT o.order_id,
       1 + ((o.order_id * 2 + k) % 30),
       1 + ((o.order_id + k) % 3),
       (SELECT price FROM products WHERE product_id = 1 + ((o.order_id * 2 + k) % 30))
FROM orders o
CROSS JOIN generate_series(0, 3) k
WHERE k < 1 + (o.order_id % 4);

UPDATE orders o
SET total = agg.item_total
FROM (
    SELECT order_id, SUM(quantity * unit_price) AS item_total
    FROM order_items
    GROUP BY order_id
) agg
WHERE o.order_id = agg.order_id;

-- HR: 12 сотрудников, salary — PII; премии только у 7 (ловушка LEFT JOIN … IS NULL)
INSERT INTO employees (name, department, salary)
SELECT 'Employee ' || g,
       CASE WHEN g <= 5 THEN 'sales' WHEN g <= 9 THEN 'logistics' ELSE 'hr' END,
       (60000 + g * 1500)::numeric(12, 2)
FROM generate_series(1, 12) g;

INSERT INTO bonuses (employee_id, period, amount)
SELECT g, '2026-Q4', (5000 + g * 500)::numeric(12, 2)
FROM generate_series(1, 7) g;

-- === Реестр рёбер (владелец — куратор; демо-бутстрап для сцен S05/S07/S09) ===
INSERT INTO agent_relationships (
    edge_id, version, status, replaced_by, left_table, left_columns, right_table,
    right_columns, join_type, cardinality, fanout_guard, meaning, intent_tags,
    usage_notes, weight, domain, owner, reviewed_at
) VALUES
    ('e_campaigns_orders', 1, 'active', NULL, 'campaigns', '{campaign_id}', 'orders',
     '{campaign_id}', 'inner', 'n:1', false,
     'заказ атрибутирован кампании', '{attribution,ads}',
     'campaign_id NULLABLE: заказы вне кампаний молча теряются в INNER JOIN —'
     ' join_type содержательное решение (ADR-006)', 1.2,
     'marketing', 'curator', now()),
    ('e_customers_orders', 1, 'active', NULL, 'customers', '{customer_id}', 'orders',
     '{customer_id}', 'inner', '1:n', false,
     'клиент разместил заказ', '{buyer,client}',
     'customers.created_at — критерий «новый клиент» (по created_at)', 1.0,
     'sales', 'curator', now()),
    ('e_orders_items', 1, 'active', NULL, 'orders', '{order_id}', 'order_items',
     '{order_id}', 'inner', '1:n', true,
     'заказ раскрывается в позиции', '{items,basket}',
     'агрегация поверх ребра только с предагрегацией в CTE (fanout_guard, ADR-004)', 0.9,
     'sales', 'curator', now()),
    ('e_products_items', 1, 'active', NULL, 'products', '{product_id}', 'order_items',
     '{product_id}', 'inner', '1:n', true,
     'товар продаётся в позициях заказов', '{sku,product}',
     'агрегация поверх ребра — предагрегация (fanout_guard)', 0.8,
     'catalog', 'curator', now()),
    ('e_employees_bonuses', 1, 'active', NULL, 'employees', '{employee_id}', 'bonuses',
     '{employee_id}', 'left', '1:n', false,
     'сотрудник получил премию (или нет)', '{bonus,hr}',
     'LEFT JOIN … IS NULL — «не был премирован»; bonuses не у всех сотрудников', 1.0,
     'hr', 'curator', now());

-- Устаревание вместо удаления: replaced_by → активное ребро (FK), история сохраняется
INSERT INTO agent_relationships (
    edge_id, version, status, replaced_by, left_table, left_columns, right_table,
    right_columns, join_type, cardinality, fanout_guard, meaning, intent_tags,
    usage_notes, weight, domain, owner, reviewed_at
) VALUES
    ('e_orders_items_v0', 1, 'deprecated', 'e_orders_items', 'orders', '{order_id}', 'order_items',
     '{order_id}', 'inner', '1:n', true,
     'УСТАРЕВШЕ: черновая семантика ребра до ревизии (демо жизненного цикла)', '{}',
     'не попадает в вьюху active — агент его не видит (ADR-006 §4.2)', 0.1,
     'sales', 'curator', now());

-- === Демо-права (ADR-005: права — данные; salary вне прав, кроме hr_viewer) ===
INSERT INTO agent_permissions (role, table_name, columns, updated_by) VALUES
    ('logistics_viewer', 'customers', '*', 'curator'),
    ('logistics_viewer', 'orders', '*', 'curator'),
    ('logistics_viewer', 'order_items', '*', 'curator'),
    ('logistics_viewer', 'campaigns', '*', 'curator'),
    ('logistics_viewer', 'products', '*', 'curator'),
    ('hr_viewer', 'employees', '*', 'curator'),
    ('hr_viewer', 'bonuses', '*', 'curator'),
    ('analyst', 'orders', '*', 'curator'),
    ('analyst', 'order_items', '*', 'curator'),
    ('analyst', 'products', '*', 'curator');

INSERT INTO agent_user_roles (user_id, role) VALUES
    ('u_logist', 'logistics_viewer'),
    ('u_hr', 'hr_viewer'),
    ('u_analyst', 'analyst');

-- Каталог источников (ADR-011): v1 — все таблицы из pg, протокол sql
INSERT INTO agent_data_sources (table_name, ds_id, protocol)
SELECT t, 'pg', 'sql'
FROM (VALUES ('customers'), ('campaigns'), ('orders'), ('order_items'),
             ('products'), ('employees'), ('bonuses')) AS v (t);
-- settings_version наполняется триггерами bump_settings_version на INSERT выше
"""


def upgrade() -> None:
    op.execute(DEMO_SQL)


def downgrade() -> None:
    op.execute(
        r"""
        DELETE FROM agent_data_sources;
        DELETE FROM agent_user_roles;
        DELETE FROM agent_permissions;
        DELETE FROM agent_relationships;
        DELETE FROM bonuses;
        DELETE FROM employees;
        DELETE FROM order_items;
        DELETE FROM orders;
        DELETE FROM products;
        DELETE FROM campaigns;
        DELETE FROM customers;
        """
    )

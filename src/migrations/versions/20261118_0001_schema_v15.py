"""Миграция 001 (S02): схема v1.5 — бизнес-таблицы, настройки, реестр рёбер, трейс, аудит.

Источники DDL (канон):
- состав и поля таблиц — docs/diagrams/er-data-model.md v1.5;
- agent_permissions / agent_user_roles / settings_version + bump_settings_version() —
  ADR-005 §4 (права как данные, триггерный счётчик версий);
- agent_relationships — ADR-006 §4.1 (единственный DDL-источник контракта ребра R1);
- agent_edge_proposals — ADR-006 §4.4; вьюха active — ADR-006 §4.2;
- страховочный триггер append-only на query_trace — security.md §2.2.

Роли и гранты — отдельная миграция (0002); демо-данные — data-migration (0003).
"""

from alembic import op

revision = "s02_0001_schema_v15"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA_SQL = r"""
-- Расширение pgvector (er-data-model v1.5; векторная память — S06)
CREATE EXTENSION IF NOT EXISTS vector;

-- === Бизнес-схема: витрина заказов + HR-домен (er-data-model v1.5) ===
CREATE TABLE customers (
    customer_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        text NOT NULL,                 -- синтетика v1 (персональные данные — PII)
    country     text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE campaigns (
    campaign_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        text NOT NULL,
    channel     text NOT NULL CHECK (channel IN ('ads', 'email', 'partner')),
    started_at  date NOT NULL
);

CREATE TABLE products (
    product_id  integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        text NOT NULL,
    category    text NOT NULL,
    price       numeric(12, 2) NOT NULL
);

CREATE TABLE orders (
    order_id    integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id integer NOT NULL REFERENCES customers (customer_id),
    campaign_id integer REFERENCES campaigns (campaign_id),
    -- NULL = вне кампаний (ловушка INNER JOIN, ADR-006)
    order_date  date NOT NULL,
    status      text NOT NULL CHECK (status IN ('paid', 'shipped', 'cancelled')),
    total       numeric(12, 2) NOT NULL
);

CREATE TABLE order_items (
    order_item_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id      integer NOT NULL REFERENCES orders (order_id),
    product_id    integer NOT NULL REFERENCES products (product_id),
    quantity      integer NOT NULL CHECK (quantity > 0),
    unit_price    numeric(12, 2) NOT NULL
);

CREATE TABLE employees (
    employee_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        text NOT NULL,                 -- синтетика v1 (PII)
    department  text NOT NULL CHECK (department IN ('sales', 'logistics', 'hr')),
    salary      numeric(12, 2) NOT NULL
    -- salary: PII-колонка, вне прав кроме hr_viewer (er-data-model)
);

CREATE TABLE bonuses (
    bonus_id    integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    employee_id integer NOT NULL REFERENCES employees (employee_id),
    period      text NOT NULL,                 -- '2026-Q4'
    amount      numeric(12, 2) NOT NULL
);

-- === Права как данные (ADR-005 §4) ===
CREATE TABLE agent_permissions (
    role        text NOT NULL,
    schema_name text NOT NULL DEFAULT 'public',
    table_name  text NOT NULL,
    columns     text NOT NULL DEFAULT '*',     -- '*' | 'employee_id,name,department'
    updated_by  text,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (role, schema_name, table_name)
);

CREATE TABLE agent_user_roles (
    user_id text NOT NULL,
    role    text NOT NULL,
    PRIMARY KEY (user_id, role)
);

-- === Реестр рёбер — единственный SoT связей (DDL-контракт ребра R1: ADR-006 §4.1) ===
CREATE TABLE agent_relationships (
    edge_id              text PRIMARY KEY,               -- адресация в плане, валидаторе и аудите
    version              integer NOT NULL DEFAULT 1,     -- ревизия ребра (правка = +1, не DELETE)
    status               text NOT NULL DEFAULT 'draft',  -- draft | active | deprecated
    replaced_by          text REFERENCES agent_relationships (edge_id),
    -- replaced_by: устаревание вместо удаления
    left_table           text NOT NULL,
    left_columns         text[] NOT NULL,                -- составные ключи
    right_table          text NOT NULL,
    right_columns        text[] NOT NULL,
    join_operator        text NOT NULL DEFAULT '=',
    join_type            text NOT NULL DEFAULT 'inner',
    -- join_type: inner | left — бизнес-дефолт, модель не решает
    cardinality          text NOT NULL,                  -- 1:1 | 1:n | n:1 | n:m
    fanout_guard         boolean NOT NULL DEFAULT false,
    -- fanout_guard: агрегация поверх ребра требует предагрегации (ADR-004)
    mandatory_predicates jsonb NOT NULL DEFAULT '{}',
    -- mandatory_predicates: MANDT, языковой ключ, deleted=false, tenant
    temporal             jsonb,                          -- «на дату заказа» vs «на сегодня»
    meaning              text NOT NULL,                  -- что связь значит бизнесу
    intent_tags          text[] NOT NULL DEFAULT '{}',   -- выбор между параллельными рёбрами
    usage_notes          text,                           -- ловушки; входят в ответ как допущения
    weight               numeric NOT NULL DEFAULT 1.0,   -- ранжирование путей
    domain               text NOT NULL,                  -- доменная нарезка
    owner                text NOT NULL,                  -- ответственность
    reviewed_at          timestamptz,                    -- дата последнего ревью владельцем
    ci_report            jsonb,                          -- протокол последнего CI-прогона ребра
    created_at           timestamptz NOT NULL DEFAULT now()
);

-- Заявки владельцу на рёбра (ADR-006 §4.4): «нет активного пути» — не только отказ
CREATE TABLE agent_edge_proposals (
    proposal_id     serial PRIMARY KEY,
    left_table      text NOT NULL,
    right_table     text NOT NULL,
    question_sample text NOT NULL,               -- пример вопроса без активного пути
    hits            integer NOT NULL DEFAULT 1,  -- частота (ранжирует очередь владельцу)
    status          text NOT NULL DEFAULT 'open',  -- open | accepted | rejected
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- Каталог источников (ADR-011): таблица → источник; v1 — только pg/sql
CREATE TABLE agent_data_sources (
    table_name text PRIMARY KEY,
    ds_id      text NOT NULL,    -- 'pg' | 'sap'
    protocol   text NOT NULL     -- 'sql' | 'spec' — форма запроса к источнику
);

-- === Версионирование настроек — триггерный счётчик (ADR-005 §4, ревизия 02) ===
CREATE TABLE settings_version (
    name    text PRIMARY KEY,   -- 'permissions' | 'user_roles' | 'relationships'
    version bigint NOT NULL DEFAULT 0
);

-- Счётчик должен инкрементироваться при изменении настроек ЛЮБОЙ ролью с грантом
-- (registry_writer тоже), поэтому SECURITY DEFINER — функция выполняется от владельца.
-- Hardening (ревью Important-1): SET search_path = pg_catalog + квалификация
-- public.settings_version — иначе temp-таблица-двойник в сессии пишущего перехватывает
-- bump (pg_temp просматривается раньше public даже при search_path без него),
-- и обходится инвариант NFR-5. Прямой вызов запрещён (REVOKE EXECUTE):
-- триггеры EXECUTE-привилегию не требуют.
CREATE FUNCTION bump_settings_version() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
BEGIN
  -- алиас sv: в ON CONFLICT DO UPDATE ссылка на строку-цель квалификацией
  -- схемы не разворачивается (plpgsql) — только по имени/алиасу
  INSERT INTO public.settings_version AS sv (name, version) VALUES (TG_ARGV[0], 1)
  ON CONFLICT (name) DO UPDATE SET version = sv.version + 1;
  RETURN NULL;
END $$;

REVOKE EXECUTE ON FUNCTION bump_settings_version() FROM PUBLIC;

CREATE TRIGGER bump_agent_permissions
AFTER INSERT OR UPDATE OR DELETE ON agent_permissions
FOR EACH STATEMENT EXECUTE FUNCTION bump_settings_version('permissions');

CREATE TRIGGER bump_agent_user_roles
AFTER INSERT OR UPDATE OR DELETE ON agent_user_roles
FOR EACH STATEMENT EXECUTE FUNCTION bump_settings_version('user_roles');

CREATE TRIGGER bump_agent_relationships
AFTER INSERT OR UPDATE OR DELETE ON agent_relationships
FOR EACH STATEMENT EXECUTE FUNCTION bump_settings_version('relationships');

-- Агент читает только active (ADR-006 §4.2): draft не существует, deprecated в вьюху не попадает
CREATE VIEW agent_relationships_active AS
SELECT * FROM agent_relationships WHERE status = 'active';

-- === Канал трейса (FR-11) и аудит (S8) ===
CREATE TABLE query_trace (
    trace_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      text NOT NULL,              -- проверенная identity (text без FK)
    question     text NOT NULL,
    assumptions  jsonb,                      -- допущения clarify
    join_plan    jsonb,                      -- edge_id рёбер плана
    graph_version text,                      -- пин снапшота реестра на прогон
    query_plan   jsonb,                      -- NULL без декомпозиции
    sql          text,                       -- SQL (шаги — массивом в sql_steps)
    retry_count  integer NOT NULL DEFAULT 0,
    latency_ms   integer,
    ttft_ms      integer,                    -- TTFT первого LLM-вызова (NFR-1, R-C2)
    cost_rub     numeric(10, 4),
    node_timings jsonb,                      -- события LLM-вызовов (FR-11)
    cache_hit    boolean NOT NULL DEFAULT false,
    truncated    boolean NOT NULL DEFAULT false,
    approximate  boolean NOT NULL DEFAULT false,
    incomplete   boolean NOT NULL DEFAULT false,
    status       text NOT NULL CHECK (status IN (
                     'ok', 'clarification', 'degrade', 'fail', 'overflow',
                     'short_circuit', 'rejected', 'budget_stop', 'degraded_out')),
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE agent_audit (
    -- S8: append-only, отдельно от трейсов
    audit_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id      text NOT NULL,
    question     text NOT NULL,
    sql          text,
    edge_ids     text,                       -- рёбра плана
    graph_version text,                      -- снапшот, по которому считали
    row_count    integer,
    refusals     jsonb,                      -- отказы по правам (что именно, без чужой схемы)
    cost_rub     numeric(10, 4),
    model_levels jsonb,                      -- уровни моделей по вызовам
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- Append-only query_trace физически: пишет отдельная INSERT-only роль (security §2.2),
-- триггер — страховка от выдачи прав позже
CREATE FUNCTION forbid_query_trace_mutation() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'query_trace append-only: % запрещён (security.md §2.2)', TG_OP;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER query_trace_append_only
BEFORE UPDATE OR DELETE ON query_trace
FOR EACH ROW EXECUTE FUNCTION forbid_query_trace_mutation();
"""


def upgrade() -> None:
    op.execute(SCHEMA_SQL)


def downgrade() -> None:
    op.execute(
        r"""
        DROP TRIGGER IF EXISTS query_trace_append_only ON query_trace;
        DROP FUNCTION IF EXISTS forbid_query_trace_mutation();
        DROP TABLE IF EXISTS agent_audit;
        DROP TABLE IF EXISTS query_trace;
        DROP VIEW  IF EXISTS agent_relationships_active;
        DROP TRIGGER IF EXISTS bump_agent_relationships ON agent_relationships;
        DROP TRIGGER IF EXISTS bump_agent_user_roles ON agent_user_roles;
        DROP TRIGGER IF EXISTS bump_agent_permissions ON agent_permissions;
        DROP FUNCTION IF EXISTS bump_settings_version();
        DROP TABLE IF EXISTS settings_version;
        DROP TABLE IF EXISTS agent_data_sources;
        DROP TABLE IF EXISTS agent_edge_proposals;
        DROP TABLE IF EXISTS agent_relationships;
        DROP TABLE IF EXISTS agent_user_roles;
        DROP TABLE IF EXISTS agent_permissions;
        DROP TABLE IF EXISTS bonuses;
        DROP TABLE IF EXISTS employees;
        DROP TABLE IF EXISTS order_items;
        DROP TABLE IF EXISTS orders;
        DROP TABLE IF EXISTS products;
        DROP TABLE IF EXISTS campaigns;
        DROP TABLE IF EXISTS customers;
        """
    )

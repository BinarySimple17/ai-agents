"""Юнит-тесты feat-011 (S11): validate_sql — главный рубеж безопасности (ADR-005).

Покрытие по Tasks плана S11:
- Task 1 — форма: одна инструкция, корень SELECT, whitelist узлов (fail-closed),
  allow-list функций, обход CTE/UNION/подзапросов/LATERAL, внешний LIMIT
  (добавление/кламп — ruling R1 ARCH-GATE: SQL от assemble_sql всегда без LIMIT);
- Task 2 — ACL: deny-list схем (deny-by-default: мир агента — public), таблицы
  ∈ permissions + каталог мира (классификация «несуществующее» vs «вне прав»),
  колонки ∈ permissions (fail-closed резолюция алиасов — Ловушка 5), `*`
  разворачивается в разрешённые колонки (ADR-005 §4 п.2, ruling R2);
- Task 3 — скелет: skeleton_hash (state ↔ VO), структура джойнов = плану
  (тип/таблица/пары ON, порядок), mandatory_predicates в WHERE внешнего запроса;
- Task 4 — wiring FSM: три исхода → route_flags (parity с routing.FLAG_*),
  state.validation, трейс-событие; adversarial-галерея ADR-005 §4 п.8;
  property-based сверка с парсером (ruling R3).

Сервис детерминирован (без LLM/IO) — моков не требует (S00 правило 7).
"""

from typing import Any

import pytest

from text_to_sql_agent.application.ports.sql_executor import ROW_LIMIT
from text_to_sql_agent.application.services.validate_sql_service import (
    ValidateSqlResult,
    ValidateSqlService,
)
from text_to_sql_agent.domain.join_plan import JoinPlan, JoinPlanEdge
from text_to_sql_agent.domain.skeleton import Skeleton, build_skeleton
from text_to_sql_agent.domain.sql_validation import ValidationOutcome

# --- Фикстуры мира агента (S00 контракт 3: {table: [columns]}, '*' — все) ---

ORDERS_COLUMNS = ("id", "customer_id", "total", "status", "created_at")
CUSTOMERS_COLUMNS = ("id", "name", "region")

PERMISSIONS: dict[str, list[str]] = {
    "orders": list(ORDERS_COLUMNS),
    "customers": list(CUSTOMERS_COLUMNS),
}
# Каталог мира агента (agent_data_sources — SoT, BL-009): таблицы, существующие
# НИ ДЛЯ КОГО скрыто — «вне прав» различим от «несуществующей» (ruling rule 4)
CATALOG = frozenset({"orders", "customers", "bonuses", "campaigns", "order_items"})


def _plan(**overrides: Any) -> JoinPlan:
    """Каноничный план: orders → customers по customer_id = id."""
    edges = overrides.get(
        "edges",
        [
            JoinPlanEdge(
                rel_id="rel-orders-customers",
                from_table="orders",
                to_table="customers",
                from_columns=("customer_id",),
                to_columns=("id",),
                join_type="inner",
            )
        ],
    )
    return JoinPlan(
        tables=overrides.get("tables", ["orders", "customers"]),
        edges=edges,
        graph_version=overrides.get("graph_version", 1),
        mandatory_predicates=overrides.get("mandatory_predicates", {}),
    )


def _skeleton(plan: JoinPlan | None = None) -> Skeleton:
    return build_skeleton(plan or _plan())


def _validate(
    sql: str,
    *,
    plan: JoinPlan | None = None,
    skeleton: Skeleton | None = None,
    skeleton_hash: str | None = None,
    permissions: dict[str, list[str]] | None = None,
    catalog: frozenset[str] = CATALOG,
    row_limit: int = ROW_LIMIT,
) -> ValidateSqlResult:
    """Прогон через сервис с каноничными фикстурами (все входы обязательны — fail-closed)."""
    plan = plan or _plan()
    skeleton = skeleton if skeleton is not None else _skeleton(plan)
    skeleton_hash = skeleton_hash if skeleton_hash is not None else skeleton.skeleton_hash
    return ValidateSqlService().validate(
        sql,
        skeleton=skeleton,
        join_plan=plan,
        skeleton_hash=skeleton_hash,
        permissions=(
            permissions if permissions is not None else {k: list(v) for k, v in PERMISSIONS.items()}
        ),
        data_catalog=catalog,
        row_limit=row_limit,
    )


# --- Task 1: форма AST — одна инструкция, SELECT, whitelist, allow-list, LIMIT ---


class TestOuterLimit:
    def test_no_limit_added_row_limit(self):
        result = _validate(
            "SELECT o.id FROM orders o INNER JOIN customers c "
            "ON o.customer_id = c.id WHERE c.region = 'RU'"
        )
        assert result.verdict.outcome is ValidationOutcome.OK
        assert result.verdict.enforced_limit == ROW_LIMIT
        assert "LIMIT 1000" in result.sql

    def test_small_limit_preserved(self):
        result = _validate(
            "SELECT o.id FROM orders o INNER JOIN customers c ON o.customer_id = c.id LIMIT 42"
        )
        assert result.verdict.outcome is ValidationOutcome.OK
        assert result.verdict.enforced_limit == 42
        assert "LIMIT 42" in result.sql
        assert "LIMIT 1000" not in result.sql

    def test_large_limit_clamped(self):
        result = _validate(
            "SELECT o.id FROM orders o INNER JOIN customers c ON o.customer_id = c.id LIMIT 9999"
        )
        assert result.verdict.outcome is ValidationOutcome.OK
        assert result.verdict.enforced_limit == ROW_LIMIT
        assert "LIMIT 1000" in result.sql

    def test_union_root_limit_also_enforced(self):
        # UNION-корень: скелет изменён (hallucination), но обход веток и кламп
        # LIMIT работают — содержимое проверяется до вердикта о структуре
        result = _validate(
            "SELECT o.id FROM orders o INNER JOIN customers c ON o.customer_id = c.id "
            "UNION SELECT c.id FROM customers c LIMIT 5000"
        )
        assert result.verdict.enforced_limit == ROW_LIMIT


class TestStatementForm:
    @pytest.mark.parametrize(
        "sql",
        [
            "INSERT INTO orders (id) VALUES (1)",
            "UPDATE orders SET total = 0",
            "DELETE FROM orders",
            "MERGE INTO orders USING customers ON 1=1 WHEN MATCHED THEN UPDATE SET total = 0",
            "CREATE TABLE evil (id int)",
            "DROP TABLE orders",
            "ALTER TABLE orders ADD COLUMN evil int",
            "TRUNCATE orders",
            "SET search_path = pg_catalog",
            "COPY orders TO STDOUT",
            "CALL some_proc()",
            "DO $$ BEGIN END $$",
        ],
    )
    def test_non_select_root_rejected(self, sql: str):
        result = _validate(sql)
        assert result.verdict.outcome is ValidationOutcome.HALLUCINATION
        assert result.sql == sql  # исходный текст не изменён — ничего не исполняется

    def test_two_statements_rejected(self):
        result = _validate("SELECT o.id FROM orders o LIMIT 1; SELECT c.id FROM customers c")
        assert result.verdict.outcome is ValidationOutcome.HALLUCINATION

    def test_parse_error_rejected(self):
        result = _validate("SELEC * FRM orders WHERE")
        assert result.verdict.outcome is ValidationOutcome.HALLUCINATION

    def test_for_update_rejected(self):
        result = _validate("SELECT o.id FROM orders o FOR UPDATE")
        assert result.verdict.outcome is ValidationOutcome.HALLUCINATION

    def test_unknown_construct_fail_closed(self):
        # Незнакомая синтаксика (TABLESAMPLE) — не парсится postgres-диалектом
        # валидатора: отказ, не «не проверили» (parse-путь и whitelist-путь —
        # один исход: hallucination)
        result = _validate("SELECT o.id FROM orders o TABLESAMPLE SYSTEM (10)")
        assert result.verdict.outcome is ValidationOutcome.HALLUCINATION

    def test_offset_allowed(self):
        result = _validate(
            "SELECT o.id FROM orders o INNER JOIN customers c "
            "ON o.customer_id = c.id LIMIT 10 OFFSET 5"
        )
        assert result.verdict.outcome is ValidationOutcome.OK


class TestFunctionAllowList:
    @pytest.mark.parametrize(
        "fn",
        [
            "dblink('host=...', 'select 1')",
            "pg_read_file('/etc/passwd')",
            "pg_sleep(10)",
            "pg_ls_dir('.')",
            "lo_import('/tmp/x')",
        ],
    )
    def test_dangerous_functions_rejected(self, fn: str):
        result = _validate(f"SELECT {fn} FROM orders")
        assert result.verdict.outcome is ValidationOutcome.HALLUCINATION

    def test_allowed_functions_ok(self):
        result = _validate(
            "SELECT count(o.id), sum(o.total), lower(c.name), coalesce(o.status, 'none'), "
            "date_trunc('day', o.created_at) FROM orders o "
            "INNER JOIN customers c ON o.customer_id = c.id GROUP BY c.name LIMIT 10"
        )
        assert result.verdict.outcome is ValidationOutcome.OK

    def test_cast_is_allowed(self):
        result = _validate("SELECT o.total::text FROM orders o LIMIT 10")
        assert result.verdict.outcome is ValidationOutcome.OK


class TestNestedTraversal:
    """Обход вложенности: CTE/UNION/подзапросы/LATERAL — те же правила (план Task 1)."""

    def test_cte_reference_is_not_a_table(self):
        result = _validate(
            "WITH top AS (SELECT o.id, o.customer_id FROM orders o) "
            "SELECT t.id FROM top t INNER JOIN customers c ON t.customer_id = c.id LIMIT 10"
        )
        assert result.verdict.outcome is ValidationOutcome.OK

    def test_exists_subquery_checked(self):
        result = _validate(
            "SELECT o.id FROM orders o WHERE EXISTS "
            "(SELECT 1 FROM customers c WHERE c.id = o.customer_id) LIMIT 10"
        )
        assert result.verdict.outcome is ValidationOutcome.OK

    def test_window_function_checked(self):
        result = _validate(
            "SELECT c.region, sum(o.total) OVER (PARTITION BY c.region) FROM orders o "
            "INNER JOIN customers c ON o.customer_id = c.id LIMIT 10"
        )
        assert result.verdict.outcome is ValidationOutcome.OK

    def test_lateral_subquery_checked(self):
        result = _validate(
            "SELECT o.id, l.x FROM orders o, LATERAL (SELECT o.customer_id AS x) l LIMIT 10"
        )
        assert result.verdict.outcome is ValidationOutcome.OK

    def test_distinct_on_checked(self):
        result = _validate(
            "SELECT DISTINCT ON (c.region) c.region, c.name FROM orders o "
            "INNER JOIN customers c ON o.customer_id = c.id LIMIT 10"
        )
        assert result.verdict.outcome is ValidationOutcome.OK

    def test_grouping_sets_checked(self):
        result = _validate(
            "SELECT c.region, count(*) FROM orders o "
            "INNER JOIN customers c ON o.customer_id = c.id "
            "GROUP BY GROUPING SETS ((c.region), ()) LIMIT 10"
        )
        assert result.verdict.outcome is ValidationOutcome.OK

    @pytest.mark.skip(
        reason="Task 3 (скелет) — следующая итерация: UNION-корень противоречит скелету плана"
    )
    def test_union_root_structure_change_detected(self):
        # UNION-корень легален парсеру, но противоречит скелету плана — hallucination
        result = _validate(
            "SELECT o.id FROM orders o UNION ALL SELECT c.id FROM customers c LIMIT 10"
        )
        assert result.verdict.outcome is ValidationOutcome.HALLUCINATION


# --- Task 2: ACL — схемы, таблицы, колонки, '*' (ruling R2, BL-014) ---
# Скоуп коммита — Task 1 (директива владельца «после task 1 остановиться»):
# тесты Task 2/3 коммитятся как ЗАФИКСИРОВАННЫЙ КОНТРАКТ поведения (skip) —
# следующий инкремент снимает марки и доводит их до зелёного.

_SKIP_TASK2 = pytest.mark.skip(
    reason="Task 2 (ACL) — следующий инкремент; контракт поведения зафиксирован"
)


@_SKIP_TASK2
class TestSchemaDenyList:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT usename FROM pg_catalog.pg_roles",
            "SELECT table_name FROM information_schema.tables",
            "SELECT role FROM agent_permissions",
            "SELECT trace_id FROM agent_query_trace",  # agent_*-префикс
        ],
    )
    def test_service_schemas_denied(self, sql: str):
        result = _validate(sql)
        assert result.verdict.outcome is ValidationOutcome.DENIED

    def test_non_public_schema_denied(self):
        result = _validate("SELECT id FROM hr.employees")
        assert result.verdict.outcome is ValidationOutcome.DENIED

    def test_comment_does_not_hide_schema(self):
        result = _validate("SELECT usename FROM /* добраться мимо */ pg_catalog.pg_roles")
        assert result.verdict.outcome is ValidationOutcome.DENIED


@_SKIP_TASK2
class TestTableAcl:
    def test_existing_table_outside_permissions_denied(self):
        # bonuses существует в мире (каталог), но не в правах — degrade без retry
        result = _validate("SELECT b.amount FROM bonuses b LIMIT 10")
        assert result.verdict.outcome is ValidationOutcome.DENIED

    def test_unknown_table_hallucination(self):
        # sales_2025 нет ни в правах, ни в каталоге — модель выдумала; retry оправдан
        result = _validate("SELECT s.x FROM sales_2025 s LIMIT 10")
        assert result.verdict.outcome is ValidationOutcome.HALLUCINATION

    def test_union_with_foreign_table_denied(self):
        # Adversarial-галерея §4 п.8: UNION с чужой таблицей — denied доминирует
        result = _validate(
            "SELECT o.id FROM orders o UNION ALL SELECT b.id FROM bonuses b LIMIT 10"
        )
        assert result.verdict.outcome is ValidationOutcome.DENIED


@_SKIP_TASK2
class TestColumnAcl:
    def test_column_outside_permissions_denied(self):
        result = _validate(
            "SELECT c.phone FROM orders o INNER JOIN customers c ON o.customer_id = c.id LIMIT 10"
        )
        assert result.verdict.outcome is ValidationOutcome.DENIED

    def test_permitted_columns_ok(self):
        result = _validate(
            "SELECT c.name, c.region FROM orders o "
            "INNER JOIN customers c ON o.customer_id = c.id LIMIT 10"
        )
        assert result.verdict.outcome is ValidationOutcome.OK

    def test_qualified_by_table_name_without_alias(self):
        result = _validate(
            "SELECT customers.phone FROM orders INNER JOIN customers "
            "ON orders.customer_id = customers.id LIMIT 10"
        )
        assert result.verdict.outcome is ValidationOutcome.DENIED

    def test_unknown_alias_rejected(self):
        result = _validate("SELECT x.id FROM orders o LIMIT 10")
        assert result.verdict.outcome is ValidationOutcome.HALLUCINATION

    def test_unqualified_column_resolved(self):
        # Ловушка 5: unqualified резолвится по таблицам запроса (единственный кандидат)
        result = _validate(
            "SELECT total FROM orders INNER JOIN customers "
            "ON orders.customer_id = customers.id LIMIT 10"
        )
        assert result.verdict.outcome is ValidationOutcome.OK

    def test_unqualified_any_candidate_unpermitted_denied(self):
        # «повезёт с дефолтом» запрещено: id есть у обеих, phone — только у customers
        # (unqualified candidates = обе таблицы; вне прав любой → отказ)
        result = _validate(
            "SELECT phone FROM orders INNER JOIN customers "
            "ON orders.customer_id = customers.id LIMIT 10"
        )
        assert result.verdict.outcome is ValidationOutcome.DENIED

    def test_unqualified_ambiguous_rejected(self):
        # id разрешён обеим таблицам — неоднозначно: квалифицируйте (retry оправдан)
        result = _validate(
            "SELECT id FROM orders INNER JOIN customers "
            "ON orders.customer_id = customers.id LIMIT 10"
        )
        assert result.verdict.outcome is ValidationOutcome.HALLUCINATION

    def test_column_of_unpermitted_table_denied(self):
        result = _validate("SELECT b.amount, b.paid_at FROM bonuses b LIMIT 10")
        assert result.verdict.outcome is ValidationOutcome.DENIED


@_SKIP_TASK2
class TestStarExpansion:
    """ADR-005 §4 п.2 (ruling R2): '*' разворачивается в разрешённые колонки."""

    def test_star_expands_to_permitted_columns(self):
        result = _validate("SELECT * FROM orders o LIMIT 10")
        assert result.verdict.outcome is ValidationOutcome.OK
        for column in ORDERS_COLUMNS:
            assert f"o.{column}" in result.sql
        assert "*" not in result.sql

    def test_star_all_permitted_stays_literal(self):
        perms = {"orders": ["*"], "customers": ["*"]}
        result = _validate("SELECT * FROM orders o LIMIT 10", permissions=perms)
        assert result.verdict.outcome is ValidationOutcome.OK
        assert "*" in result.sql  # все колонки разрешены — разворот не нужен

    def test_qualified_star_expands(self):
        result = _validate(
            "SELECT c.*, o.total FROM orders o "
            "INNER JOIN customers c ON o.customer_id = c.id LIMIT 10"
        )
        assert result.verdict.outcome is ValidationOutcome.OK
        for column in CUSTOMERS_COLUMNS:
            assert f"c.{column}" in result.sql
        assert "c.*" not in result.sql

    def test_star_over_table_without_permitted_columns_denied(self):
        perms = {"orders": []}
        result = _validate("SELECT * FROM orders o LIMIT 10", permissions=perms)
        assert result.verdict.outcome is ValidationOutcome.DENIED

    def test_star_in_count_is_not_a_column(self):
        # COUNT(*) — агрегат, не проекция: разворот не применяется, колонки не выгорают
        result = _validate(
            "SELECT count(*) FROM orders o INNER JOIN customers c ON o.customer_id = c.id LIMIT 10"
        )
        assert result.verdict.outcome is ValidationOutcome.OK
        assert "count(*)" in result.sql.lower()


@_SKIP_TASK2
class TestPermissionsContract:
    def test_str_columns_value_rejected_fail_closed(self):
        # BL-014: CSV-строка в правах — нарушение контракта стыка; нормализация —
        # граница адаптера (PgPermissionStore), валидатор строку не парсит
        with pytest.raises(ValueError):
            _validate(
                "SELECT o.id FROM orders o LIMIT 10",
                permissions={"orders": "id,total", "customers": ["*"]},
            )


# --- Task 3: скелет не изменён — skeleton_hash, джойны, mandatory_predicates ---


def _canonical_sql() -> str:
    return (
        "SELECT o.id, c.name FROM orders o INNER JOIN customers c "
        "ON o.customer_id = c.id WHERE o.status = 'active' LIMIT 10"
    )


@pytest.mark.skip(reason="Task 3 (скелет) — следующий инкремент; контракт поведения зафиксирован")
class TestSkeletonIntegrity:
    def test_canonical_sql_passes(self):
        plan = _plan(mandatory_predicates={"rel-orders-customers": {"orders.status": "active"}})
        result = _validate(_canonical_sql(), plan=plan)
        assert result.verdict.outcome is ValidationOutcome.OK

    def test_join_type_changed_rejected(self):
        result = _validate(
            "SELECT o.id, c.name FROM orders o LEFT JOIN customers c "
            "ON o.customer_id = c.id WHERE o.status = 'active' LIMIT 10"
        )
        assert result.verdict.outcome is ValidationOutcome.HALLUCINATION

    def test_extra_table_rejected(self):
        result = _validate(
            "SELECT o.id FROM orders o INNER JOIN customers c ON o.customer_id = c.id "
            "INNER JOIN campaigns m ON o.campaign_id = m.id LIMIT 10"
        )
        assert result.verdict.outcome is ValidationOutcome.HALLUCINATION

    def test_missing_mandatory_predicate_rejected(self):
        plan = _plan(mandatory_predicates={"rel-orders-customers": {"orders.status": "active"}})
        result = _validate(
            "SELECT o.id, c.name FROM orders o INNER JOIN customers c "
            "ON o.customer_id = c.id LIMIT 10",
            plan=plan,
        )
        assert result.verdict.outcome is ValidationOutcome.HALLUCINATION

    def test_extra_user_predicate_allowed(self):
        # модель может ДОБАВлять фильтры (пользовательский WHERE) — только не убирать
        plan = _plan(mandatory_predicates={"rel-orders-customers": {"orders.status": "active"}})
        result = _validate(
            _canonical_sql() + " AND o.total > 100",
            plan=plan,
        )
        assert result.verdict.outcome is ValidationOutcome.OK

    def test_state_hash_mismatch_rejected(self):
        # Defense-in-depth: state.skeleton_hash не соответствует VO (подмена чекпоинта)
        result = _validate(_canonical_sql(), skeleton_hash="deadbeef")
        assert result.verdict.outcome is ValidationOutcome.HALLUCINATION

    def test_from_table_changed_rejected(self):
        plan = _plan(tables=["customers", "orders"])
        result = _validate(
            "SELECT o.id FROM orders o INNER JOIN customers c ON o.customer_id = c.id LIMIT 10",
            plan=plan,
        )
        assert result.verdict.outcome is ValidationOutcome.HALLUCINATION

    def test_three_table_join_reorder_rejected(self):
        plan = _plan(
            tables=["orders", "customers", "order_items"],
            edges=[
                JoinPlanEdge(
                    rel_id="r1",
                    from_table="orders",
                    to_table="customers",
                    from_columns=("customer_id",),
                    to_columns=("id",),
                    join_type="inner",
                ),
                JoinPlanEdge(
                    rel_id="r2",
                    from_table="orders",
                    to_table="order_items",
                    from_columns=("id",),
                    to_columns=("order_id",),
                    join_type="inner",
                ),
            ],
        )
        result = _validate(
            "SELECT o.id FROM orders o INNER JOIN order_items i ON o.id = i.order_id "
            "INNER JOIN customers c ON o.customer_id = c.id LIMIT 10",
            plan=plan,
        )
        assert result.verdict.outcome is ValidationOutcome.HALLUCINATION

    def test_three_table_join_canonical_passes(self):
        plan = _plan(
            tables=["orders", "customers", "order_items"],
            edges=[
                JoinPlanEdge(
                    rel_id="r1",
                    from_table="orders",
                    to_table="customers",
                    from_columns=("customer_id",),
                    to_columns=("id",),
                    join_type="inner",
                ),
                JoinPlanEdge(
                    rel_id="r2",
                    from_table="orders",
                    to_table="order_items",
                    from_columns=("id",),
                    to_columns=("order_id",),
                    join_type="inner",
                ),
            ],
        )
        result = _validate(
            "SELECT o.id, i.order_id FROM orders o INNER JOIN customers c "
            "ON o.customer_id = c.id INNER JOIN order_items i ON o.id = i.order_id LIMIT 10",
            plan=plan,
        )
        assert result.verdict.outcome is ValidationOutcome.OK


# --- Task 4: wiring FSM — вердикты → route_flags/state.validation/трейс ---
# (галерея §4 п.8 + property-сверка + узел — ниже)

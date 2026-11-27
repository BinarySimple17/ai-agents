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

from collections.abc import Iterator
from typing import Any

import pytest
import sqlglot
from sqlglot import exp

from text_to_sql_agent.application.ports.sql_executor import ROW_LIMIT
from text_to_sql_agent.application.services.validate_sql_service import (
    ValidateSqlResult,
    ValidateSqlService,
    build_sql_audit,
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

    def test_table_function_in_from_form_rejected(self):
        # ревью S11 I2: табличная функция в FROM — форм-отказ (SqlAuditError),
        # не ValidationError сервиса (Table.name пуст → падение pydantic-VO;
        # аргумент — литерал, чтобы bite шёл именно по ветке _expr_source)
        result = _validate("SELECT * FROM pg_read_file('/etc/passwd') LIMIT 10")
        assert result.verdict.outcome is ValidationOutcome.HALLUCINATION

    def test_interval_literal_allowed(self):
        # ревью S11 I4: INTERVAL — безопасный литерал типового фильтра «за N дней»
        result = _validate(
            "SELECT o.id FROM orders o INNER JOIN customers c ON o.customer_id = c.id "
            "WHERE o.created_at > CURRENT_DATE - INTERVAL '7 days' LIMIT 10"
        )
        assert result.verdict.outcome is ValidationOutcome.OK

    def test_all_quantifier_allowed(self):
        # ревью M4: квантор ALL парсится sqlglot как Anonymous('all') — безопасный
        # квантор сравнения (зеркало ANY-узла); расширение — осознанное, с тестом
        result = _validate(
            "SELECT o.id FROM orders o INNER JOIN customers c ON o.customer_id = c.id "
            "WHERE o.total = ALL(ARRAY[1, 2]) LIMIT 10"
        )
        assert result.verdict.outcome is ValidationOutcome.OK

    def test_order_by_output_alias_allowed(self):
        # ревью S11 I3: ORDER BY по output-алиасу — не ссылка на колонку БД
        # (приоритет имени вывода в PostgreSQL); ложный denied = degrade без retry
        result = _validate(
            "SELECT o.status AS st, count(*) FROM orders o INNER JOIN customers c "
            "ON o.customer_id = c.id GROUP BY c.name ORDER BY st LIMIT 10"
        )
        assert result.verdict.outcome is ValidationOutcome.OK

    def test_order_by_expression_uses_input_column_denied(self):
        # ревью волна-2 (Critical — регрессия I3-фикса bf518b7): unqualified имя
        # ВНУТРИ выражения ORDER BY резолвится в input-колонку (проверено на PG:
        # «ORDER BY country + 1» берёт customers.country) — она обязана пройти
        # ACL: region не в правах customers, алиас region маскирует orders.status
        perms = {"orders": ["id", "customer_id", "total", "status"], "customers": ["id", "name"]}
        result = _validate(
            "SELECT o.status AS region FROM orders o INNER JOIN customers c "
            "ON o.customer_id = c.id ORDER BY length(region) LIMIT 10",
            permissions=perms,
        )
        assert result.verdict.outcome is ValidationOutcome.DENIED

    def test_order_by_quoted_alias_case_mismatch_denied(self):
        # ревью волна-3 (Critical — остаток I3-пропуска в 1ab30f7): PG сопоставляет
        # output-алиас С УЧЁТОМ РЕГИСТРА — quoted «Country» ≠ unquoted Country,
        # ORDER BY Country берёт input-колонку customers.country (вне прав);
        # регистронезависимое сравнение валидатора считало это output-алиасом
        perms = {"orders": ["id", "customer_id", "total", "status"], "customers": ["id", "name"]}
        result = _validate(
            'SELECT o.status AS "Country" FROM orders o INNER JOIN customers c '
            "ON o.customer_id = c.id ORDER BY Country LIMIT 10",
            permissions=perms,
        )
        assert result.verdict.outcome is ValidationOutcome.DENIED

    def test_order_by_quoted_exact_match_ok(self):
        # quoted-алиас и quoted-точное имя в ORDER BY — легитимный output-алиас
        perms = {"orders": ["id", "customer_id", "total", "status"], "customers": ["id", "name"]}
        result = _validate(
            'SELECT o.status AS "Country" FROM orders o INNER JOIN customers c '
            'ON o.customer_id = c.id ORDER BY "Country" LIMIT 10',
            permissions=perms,
        )
        assert result.verdict.outcome is ValidationOutcome.OK

    def test_order_by_unquoted_alias_any_case_ok(self):
        # unquoted алиас + любой регистр ORDER BY — PG складывает оба в нижний
        # регистр: output-алиас легитимен
        perms = {"orders": ["id", "customer_id", "total", "status"], "customers": ["id", "name"]}
        result = _validate(
            "SELECT o.status AS country FROM orders o INNER JOIN customers c "
            "ON o.customer_id = c.id ORDER BY Country LIMIT 10",
            permissions=perms,
        )
        assert result.verdict.outcome is ValidationOutcome.OK

    def test_correlated_whole_row_requires_star(self):
        # ревью волна-2 (Important): whole-row внешнего скоупа внутри подзапроса —
        # не «колонка» разрешённого локального источника: '*' обязателен и на
        # внешнюю таблицу (PG возвращает ВСЮ строку orders)
        perms = {"orders": list(ORDERS_COLUMNS), "customers": ["*"]}
        result = _validate(
            "SELECT (SELECT orders FROM customers LIMIT 1) FROM orders INNER JOIN customers "
            "ON orders.customer_id = customers.id LIMIT 10",
            permissions=perms,
        )
        assert result.verdict.outcome is ValidationOutcome.DENIED

    def test_correlated_whole_row_local_candidates_stay_checked(self):
        # ревью волна-3 (Important): fallback НЕ заменяет локальных кандидатов
        # внешним ref, а ДОПОЛНЯЕТ — домен требует '*' на КАЖДОЙ таблице-
        # кандидате (schema-blind: права — единственный прокси существования
        # колонки; PG при коллизии читает ЛОКАЛЬНУЮ customers.country)
        perms = {"orders": ["*"], "customers": ["id", "name"]}
        result = _validate(
            "SELECT (SELECT country FROM customers LIMIT 1) FROM orders country "
            "INNER JOIN customers c ON country.customer_id = c.id LIMIT 10",
            permissions=perms,
        )
        assert result.verdict.outcome is ValidationOutcome.DENIED

    def test_correlated_whole_row_with_star_everywhere_ok(self):
        # ревью волна-3 (Minor): позитивный кейс — '*' на обеих таблицах,
        # correlated whole-row легален (защита от овер-отказа)
        perms = {"orders": ["*"], "customers": ["*"]}
        result = _validate(
            "SELECT (SELECT country FROM customers LIMIT 1) FROM orders country "
            "INNER JOIN customers c ON country.customer_id = c.id LIMIT 10",
            permissions=perms,
        )
        assert result.verdict.outcome is ValidationOutcome.OK

    def test_nested_select_star_is_own_scope_concern(self):
        # ревью волна-2 (Minor): прямая звезда ВНУТРЕННЕГО скоупа — забота своего
        # скоупа (разворот по его правам); сканер выражений проекции не спускается
        # во вложенный SELECT — иначе поверхность парсера сужена сверх необходимого
        result = _validate(
            "SELECT (SELECT * FROM customers LIMIT 1) FROM orders INNER JOIN customers "
            "ON orders.customer_id = customers.id LIMIT 10"
        )
        assert result.verdict.outcome is ValidationOutcome.OK
        assert "*" not in result.sql  # внутренняя звезда развёрнута своим скоупом


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
        result = _validate(
            "SELECT o.total::text FROM orders o INNER JOIN customers c "
            "ON o.customer_id = c.id LIMIT 10"
        )
        assert result.verdict.outcome is ValidationOutcome.OK


class TestNestedTraversal:
    """Обход вложенности: CTE/UNION/подзапросы/LATERAL — те же правила (план Task 1)."""

    def test_cte_reference_is_not_a_table(self):
        # CTE-референс — не таблица мира (тело проверено на своём скоупе), но
        # КОРНЕВОЙ WITH — изменённый скелет (assemble_sql его не производит):
        # структура отклоняется, тело при этом проверено теми же правилами
        result = _validate(
            "WITH top AS (SELECT o.id, o.customer_id FROM orders o) "
            "SELECT t.id FROM top t INNER JOIN customers c ON t.customer_id = c.id LIMIT 10"
        )
        assert result.verdict.outcome is ValidationOutcome.HALLUCINATION

    def test_cte_body_checked_when_root_structure_intact(self):
        # CTE ВНУТРИ подзапроса не трогает корень: тело (orders) проверено ACL,
        # CTE-референс — не таблица мира, структура скелета сохранена
        result = _validate(
            "SELECT o.id FROM orders o INNER JOIN customers c ON o.customer_id = c.id "
            "WHERE EXISTS (WITH top AS (SELECT o2.id AS id FROM orders o2) "
            "SELECT 1 FROM top WHERE top.id = c.id) LIMIT 10"
        )
        assert result.verdict.outcome is ValidationOutcome.OK

    def test_exists_subquery_checked(self):
        # EXISTS-подзапрос не трогает корень скелета; колонки тела (c2/c) — те же
        # правила ACL (корреляция через скоуп-цепочку)
        result = _validate(
            "SELECT o.id FROM orders o INNER JOIN customers c ON o.customer_id = c.id "
            "WHERE EXISTS (SELECT 1 FROM customers c2 WHERE c2.region = c.region) LIMIT 10"
        )
        assert result.verdict.outcome is ValidationOutcome.OK

    def test_window_function_checked(self):
        result = _validate(
            "SELECT c.region, sum(o.total) OVER (PARTITION BY c.region) FROM orders o "
            "INNER JOIN customers c ON o.customer_id = c.id LIMIT 10"
        )
        assert result.verdict.outcome is ValidationOutcome.OK

    def test_lateral_subquery_checked(self):
        # LATERAL внутри EXISTS: тело (orders) проверено ACL, корень скелета цел
        # (флип B2/BL-051: свободный вход давал hallucination — смешанный скоуп
        # несверяем; FSM проверяет тело теми же правилами — pin residual,
        # усиление «таблицы ⊆ план» отдельным коммитом не трогает этот кейс:
        # orders в плане)
        result = _validate(
            "SELECT o.id FROM orders o INNER JOIN customers c ON o.customer_id = c.id "
            "WHERE EXISTS (SELECT l.x FROM orders o2, LATERAL (SELECT o2.customer_id AS x) l "
            "WHERE l.x = c.id) LIMIT 10"
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

    def test_union_root_structure_change_detected(self):
        # UNION-корень легален парсеру, но противоречит скелету плана — hallucination
        # (флип B2/BL-051: свободный вход давал ok — контур удалён BL-048)
        result = _validate(
            "SELECT o.id FROM orders o UNION ALL SELECT c.id FROM customers c LIMIT 10"
        )
        assert result.verdict.outcome is ValidationOutcome.HALLUCINATION


class TestNestedBodyResidualPins:
    """BL-051(a) п.3 — регрессионные тесты правила «таблицы ⊆ план» (S39).

    До правила — pin-тесты residual (коммит удаления 1533749: оба кейса
    ожидали OK с пометкой «known residual, BL-051»); правило «таблицы ⊆ план»
    в `_check_skeleton` переворачивает их осознанно по решению владельца (a)
    (BL-051, отдельный коммит после removal). Права включают bonuses/campaigns
    (в дефолтных фикстурах bonuses вне прав — DENIED замаскировал бы суть:
    отказ именно за таблицу вне плана, не за ACL). Residual после правила —
    условия связи между таблицами самого плана во вложенных телах (revisit —
    вариант (b) BL-051 при первом кейсе злоупотребления формой).
    """

    _RESIDUAL_PERMS = {
        "orders": ["id", "customer_id", "campaign_id", "total", "status"],
        "customers": ["id", "name", "region"],
        "bonuses": ["employee_id", "amount"],
        "campaigns": ["campaign_id", "name", "channel"],
    }

    def test_nested_body_table_outside_plan_hallucination(self):
        # EXISTS с таблицей вне плана (bonuses) и выдуманным условием связи —
        # таблица в правах, ACL-чист; отказ именно за «таблицы ⊆ план» (ретрай
        # оправдан: модель уберёт таблицу или план перестроится)
        result = _validate(
            "SELECT o.id FROM orders o INNER JOIN customers c ON o.customer_id = c.id "
            "WHERE EXISTS (SELECT 1 FROM bonuses b WHERE b.employee_id = o.customer_id) "
            "LIMIT 10",
            permissions=self._RESIDUAL_PERMS,
        )
        assert result.verdict.outcome is ValidationOutcome.HALLUCINATION
        assert any("вне плана" in issue for issue in result.verdict.issues)

    def test_nested_body_join_without_edge_hallucination(self):
        # вложенный JOIN без ребра (bonuses↔campaigns) — обе таблицы в правах,
        # корень скелета цел; отказ за таблицы вне плана (BL-051(a))
        result = _validate(
            "SELECT o.id FROM orders o INNER JOIN customers c ON o.customer_id = c.id "
            "WHERE o.id IN (SELECT b.employee_id FROM bonuses b "
            "JOIN campaigns ca ON b.employee_id = ca.campaign_id) LIMIT 10",
            permissions=self._RESIDUAL_PERMS,
        )
        assert result.verdict.outcome is ValidationOutcome.HALLUCINATION
        assert any("вне плана" in issue for issue in result.verdict.issues)


# --- Task 2: ACL — схемы, таблицы, колонки, '*' (ruling R2, BL-014) ---


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
        result = _validate(
            "SELECT x.id FROM orders o INNER JOIN customers c ON o.customer_id = c.id LIMIT 10"
        )
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


class TestStarExpansion:
    """ADR-005 §4 п.2 (ruling R2): '*' разворачивается в разрешённые колонки."""

    def test_star_expands_to_permitted_columns(self):
        result = _validate(
            "SELECT * FROM orders o INNER JOIN customers c ON o.customer_id = c.id LIMIT 10"
        )
        assert result.verdict.outcome is ValidationOutcome.OK
        for column in ORDERS_COLUMNS:
            assert f"o.{column}" in result.sql
        assert "*" not in result.sql

    def test_star_all_permitted_stays_literal(self):
        perms = {"orders": ["*"], "customers": ["*"]}
        result = _validate(
            "SELECT * FROM orders o INNER JOIN customers c ON o.customer_id = c.id LIMIT 10",
            permissions=perms,
        )
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
        # обе таблицы в правах (таблица-уровень чист) — отказ именно за звезду
        perms = {"orders": [], "customers": list(CUSTOMERS_COLUMNS)}
        result = _validate(
            "SELECT * FROM orders o INNER JOIN customers c ON o.customer_id = c.id LIMIT 10",
            permissions=perms,
        )
        assert result.verdict.outcome is ValidationOutcome.DENIED

    def test_star_in_count_is_not_a_column(self):
        # COUNT(*) — агрегат, не проекция: разворот не применяется, колонки не выгорают
        result = _validate(
            "SELECT count(*) FROM orders o INNER JOIN customers c ON o.customer_id = c.id LIMIT 10"
        )
        assert result.verdict.outcome is ValidationOutcome.OK
        assert "count(*)" in result.sql.lower()


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


class TestSkeletonIntegrity:
    # Все таблицы галереи в правах: отказ за СТРУКТУРУ, не за ACL (изоляция concern'а)
    _THREE_TABLE_PERMS = {
        **{k: list(v) for k, v in PERMISSIONS.items()},
        "order_items": ["id", "order_id", "quantity"],
    }

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
        # bonuses разрешён (таблица/колонки в правах) — отказ именно за ЛИШНИЙ
        # JOIN вне рёбер плана, не за ACL (ruling: denied доминирует, поэтому
        # структурную сверку изолируем разрешённой таблицей)
        perms = {**{k: list(v) for k, v in PERMISSIONS.items()}, "bonuses": ["id", "customer_id"]}
        result = _validate(
            "SELECT o.id FROM orders o INNER JOIN customers c ON o.customer_id = c.id "
            "INNER JOIN bonuses b ON o.customer_id = b.customer_id LIMIT 10",
            permissions=perms,
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
            "SELECT o.id, c.name FROM orders o INNER JOIN customers c "
            "ON o.customer_id = c.id WHERE o.status = 'active' AND o.total > 100 LIMIT 10",
            plan=plan,
        )
        assert result.verdict.outcome is ValidationOutcome.OK

    def test_state_hash_mismatch_rejected(self):
        # Defense-in-depth: state.skeleton_hash не соответствует VO (подмена чекпоинта)
        result = _validate(_canonical_sql(), skeleton_hash="deadbeef")
        assert result.verdict.outcome is ValidationOutcome.HALLUCINATION

    def test_from_table_changed_rejected(self):
        # Валидный реверс-план (FROM customers JOIN orders): SQL с FROM orders
        # не соответствует скелету — from_table фиксирован планом
        plan = _plan(
            tables=["customers", "orders"],
            edges=[
                JoinPlanEdge(
                    rel_id="rel-customers-orders",
                    from_table="customers",
                    to_table="orders",
                    from_columns=("id",),
                    to_columns=("customer_id",),
                    join_type="inner",
                )
            ],
        )
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
            permissions=self._THREE_TABLE_PERMS,
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
            permissions=self._THREE_TABLE_PERMS,
        )
        assert result.verdict.outcome is ValidationOutcome.OK


# --- Task 4: adversarial-галерея ADR-005 §4 п.8 + property-сверка (ruling R3) ---


class TestRowExpansionWholeRow:
    """Ревью S11 C1: row-expansion не обходит R2/ACL колонок.

    Звезда, спрятанная в выражении проекции (скобки/алиас/CAST), — форм-отказ
    (литеральная звезда не доходит до БД); whole-row-ссылка (SELECT orders)
    требует '*' в правах КАЖДОЙ таблицы-кандидата — явный список её не покрывает.
    На до-фиксовом коде каждый из первых пяти кейсов проходил как ok (байт
    подтверждён ревью на живом PostgreSQL).
    """

    def test_paren_star_form_rejected(self):
        result = _validate(
            "SELECT (orders.*) FROM orders INNER JOIN customers "
            "ON orders.customer_id = customers.id LIMIT 10"
        )
        assert result.verdict.outcome is ValidationOutcome.HALLUCINATION

    def test_aliased_star_form_rejected(self):
        result = _validate(
            "SELECT orders.* AS x FROM orders INNER JOIN customers "
            "ON orders.customer_id = customers.id LIMIT 10"
        )
        assert result.verdict.outcome is ValidationOutcome.HALLUCINATION

    def test_cast_star_form_rejected(self):
        result = _validate(
            "SELECT (orders.*)::text FROM orders INNER JOIN customers "
            "ON orders.customer_id = customers.id LIMIT 10"
        )
        assert result.verdict.outcome is ValidationOutcome.HALLUCINATION

    def test_whole_row_reference_denied_without_star_perms(self):
        # явный список на читаемую таблица + '*' на соседнюю — прежний код
        # резолвил whole-row как «колонку» через разрешённого соседа (утечка)
        perms = {"orders": list(ORDERS_COLUMNS), "customers": ["*"]}
        result = _validate(
            "SELECT orders FROM orders INNER JOIN customers "
            "ON orders.customer_id = customers.id LIMIT 10",
            permissions=perms,
        )
        assert result.verdict.outcome is ValidationOutcome.DENIED

    def test_whole_row_cast_denied_without_star_perms(self):
        perms = {"orders": list(ORDERS_COLUMNS), "customers": ["*"]}
        result = _validate(
            "SELECT orders::text FROM orders INNER JOIN customers "
            "ON orders.customer_id = customers.id LIMIT 10",
            permissions=perms,
        )
        assert result.verdict.outcome is ValidationOutcome.DENIED

    def test_whole_row_with_star_perms_ok(self):
        # '*' на всех кандидатах — whole-row безопасен (аналог суб-звезды R2)
        perms = {"orders": ["*"], "customers": ["*"]}
        result = _validate(
            "SELECT orders FROM orders INNER JOIN customers "
            "ON orders.customer_id = customers.id LIMIT 10",
            permissions=perms,
        )
        assert result.verdict.outcome is ValidationOutcome.OK


class TestLexicalCteVisibility:
    """Ревью S11 C2: видимость CTE — лексическая (семантика PostgreSQL).

    CTE-тень («WITH orders AS …» поверх реальной таблицы orders) не прячет
    таблицу от ACL: внутри тела первого CTE имя bonuses ещё НЕ видно как CTE —
    это реальная таблица и её права проверяются. На до-фиксовом коде сцена
    проходила как ok (байт: контроль без обманки — denied).
    """

    def test_decoy_cte_does_not_hide_real_table(self):
        result = _validate(
            "WITH orders AS (SELECT * FROM bonuses), bonuses AS (SELECT 1 AS customer_id) "
            "SELECT count(*) FROM orders INNER JOIN customers "
            "ON orders.customer_id = customers.id LIMIT 10"
        )
        assert result.verdict.outcome is ValidationOutcome.DENIED

    def test_control_direct_bonuses_still_denied(self):
        # контроль: из отказа нельзя «вылечиться» — прямое чтение bonuses вне прав
        result = _validate(
            "SELECT count(*) FROM bonuses b INNER JOIN customers c ON b.customer_id = c.id LIMIT 10"
        )
        assert result.verdict.outcome is ValidationOutcome.DENIED

    def test_from_resolving_to_cte_is_not_skeleton_from(self):
        # FROM, привязанный к CTE, — не «FROM не изменён»: структурный отказ
        result = _validate("WITH orders AS (SELECT 1 AS id) SELECT id FROM orders LIMIT 10")
        assert result.verdict.outcome is ValidationOutcome.HALLUCINATION


class TestAdversarialGallery:
    """Корпус §4 п.8 — покрытие ≥ поверхности парсера (условие приёмки PR).

    Каждая сцена не исполняется; исход классифицируется КОДОМ (denied — degrade
    без retry, hallucination — retry оправдан), не текстом ошибки. Инъекции
    2-го порядка (S6): строковые литералы остаются литералами (sqlglot не
    парсит их содержимое), комментарии не обходят проверки (AST их не несёт).
    """

    @pytest.mark.parametrize(
        ("sql", "expected"),
        [
            # UNION с чужой таблицей — denied доминирует над структурой
            (
                "SELECT o.id FROM orders o UNION ALL SELECT b.id FROM bonuses b LIMIT 10",
                ValidationOutcome.DENIED,
            ),
            # каталоги и служебные схемы (§4 п.8 «каталоги»)
            ("SELECT usename FROM pg_catalog.pg_roles LIMIT 10", ValidationOutcome.DENIED),
            ("SELECT table_name FROM information_schema.tables LIMIT 10", ValidationOutcome.DENIED),
            ("SELECT role FROM agent_permissions LIMIT 10", ValidationOutcome.DENIED),
            # window OVER(PARTITION BY ...) — тело проверено, корень цел
            (
                "SELECT c.region, sum(o.total) OVER (PARTITION BY c.region) FROM orders o "
                "INNER JOIN customers c ON o.customer_id = c.id LIMIT 10",
                ValidationOutcome.OK,
            ),
            # подзапросы (EXISTS с корреляцией через скоуп-цепочку)
            (
                "SELECT o.id FROM orders o INNER JOIN customers c ON o.customer_id = c.id "
                "WHERE EXISTS (SELECT 1 FROM customers c2 WHERE c2.region = c.region) LIMIT 10",
                ValidationOutcome.OK,
            ),
            # подзапрос с ЧУЖОЙ таблицей в теле — denied (ACL тела; B2 S31 —
            # контур-независимый кейс, мигрирован при удалении тул-контура BL-048)
            (
                "SELECT o.id FROM orders o WHERE o.id IN "
                "(SELECT employee_id FROM bonuses) LIMIT 10",
                ValidationOutcome.DENIED,
            ),
            # подзапрос в FROM с чужой таблицей: denied (тело) доминирует над
            # структурой — обе причины в issues (B2 S31 — мигрирован BL-048)
            (
                "SELECT o.id FROM orders o JOIN (SELECT employee_id FROM bonuses) b "
                "ON b.employee_id = o.id LIMIT 10",
                ValidationOutcome.DENIED,
            ),
            # LATERAL — вложенный (корневой LATERAL-джойн = изменённый скелет);
            # флип B2/BL-051: свободный вход тул-контура давал hallucination
            # (несверяемый смешанный скоуп) — FSM ok, тело проверено ACL
            (
                "SELECT o.id FROM orders o INNER JOIN customers c ON o.customer_id = c.id "
                "WHERE EXISTS (SELECT l.x FROM orders o2, LATERAL (SELECT o2.customer_id AS x) l "
                "WHERE l.x = c.id) LIMIT 10",
                ValidationOutcome.OK,
            ),
            # DISTINCT ON
            (
                "SELECT DISTINCT ON (c.region) c.region, c.name FROM orders o "
                "INNER JOIN customers c ON o.customer_id = c.id LIMIT 10",
                ValidationOutcome.OK,
            ),
            # GROUPING SETS
            (
                "SELECT c.region, count(*) FROM orders o "
                "INNER JOIN customers c ON o.customer_id = c.id "
                "GROUP BY GROUPING SETS ((c.region), ()) LIMIT 10",
                ValidationOutcome.OK,
            ),
            # t.* — разворот в разрешённые колонки (ruling R2), не отказ
            (
                "SELECT c.* FROM orders o INNER JOIN customers c ON o.customer_id = c.id LIMIT 10",
                ValidationOutcome.OK,
            ),
            # инъекция через ЗАКРЫТУЮ строку: вторая инструкция — форм-отказ
            # (одна инструкция; содержимое литерала — данные, закрытая строка +
            # «; DROP TABLE» снаружи — уже вторая инструкция)
            (
                "SELECT o.id FROM orders o INNER JOIN customers c ON o.customer_id = c.id "
                "WHERE o.status = 'x'; DROP TABLE orders; --'",
                ValidationOutcome.HALLUCINATION,
            ),
            # комментарий не прячет структуру/предикат (AST прозрачен)
            (
                "SELECT o.id FROM orders o /* добраться мимо */ "
                "INNER JOIN customers c ON o.customer_id = c.id",
                ValidationOutcome.OK,
            ),
            # UNION-корень легален парсеру, но это изменённый скелет; флип
            # B2/BL-051: свободный вход тул-контура давал ok (свои таблицы,
            # тела проверены) — FSM: скелет фиксирован планом
            (
                "SELECT o.id FROM orders o UNION ALL SELECT c.id FROM customers c LIMIT 10",
                ValidationOutcome.HALLUCINATION,
            ),
        ],
    )
    def test_gallery_scene_classified_by_code(self, sql: str, expected: ValidationOutcome):
        result = _validate(sql)
        assert result.verdict.outcome is expected

    def test_string_literal_is_not_parsed_as_sql(self):
        # S6: содержимое литерала не порождает узлов AST — «DROP TABLE» внутри
        # кавычек не виден валидатору как инструкция (проверка: запрос проходит
        # форму, литерал — данные, не инструкции)
        result = _validate(
            "SELECT o.id FROM orders o INNER JOIN customers c ON o.customer_id = c.id "
            "WHERE o.status = 'x; DROP TABLE orders; --'"
        )
        assert result.verdict.outcome is ValidationOutcome.OK
        assert "DROP" not in result.sql.replace("'x; DROP TABLE orders; --'", "")


class TestPropertyOracleColumns:
    """Ruling R3 ARCH-GATE (ADR-005 §4 п.9): для корпуса множество колонок,
    извлечённое валидатором, обязано равняться множеству из независимого обхода
    sqlglot; расхождение = дыра в whitelist'е узлов — тест красный до продакшена.

    Оракул — ВСТРОЕННЫЙ walk (не общий с валидатором код): для каждой exp.Column
    кандидаты = источники ближайшего объемлющего SELECT; квалификатор резолвится
    подъёмом по объемлющим скоупам. Корпус — галерея §4 п.8 (валидные парсеру)
    + канонические комбинации; корень без CTE (CTE-граница корреляции покрыта
    отдельными тестами Task 1/3).
    """

    @staticmethod
    def _oracle_columns(sql: str) -> set[tuple[str, tuple[str, ...]]]:
        tree = sqlglot.parse_one(sql, read="postgres")
        result: set[tuple[str, tuple[str, ...]]] = set()
        for column in tree.find_all(exp.Column):
            if isinstance(column.this, exp.Star):
                continue  # t.* — проекция звёздами, не колонка
            # ближайший объемлющий SELECT (колонка не в собственном подзапросе)
            node = column.parent
            scope = None
            while node is not None:
                if type(node) is exp.Select:
                    scope = node
                    break
                node = node.parent
            if scope is None:
                continue

            def sources_of(select: exp.Select) -> list[tuple[str, str]]:
                sources: list[tuple[str, str]] = []
                from_clause = select.args.get("from_")
                if from_clause is not None:
                    t = from_clause.this
                    sources.append(((t.alias or t.name).lower(), t.name.lower()))
                for join in select.args.get("joins") or []:
                    t = join.this
                    if isinstance(t, (exp.Subquery, exp.Lateral)):
                        sources.append(((t.alias or "").lower(), (t.alias or "").lower()))
                    else:
                        sources.append(((t.alias or t.name).lower(), t.name.lower()))
                return sources

            qualifier = (column.table or "").lower()
            if qualifier:
                # подъём по объемлющим скоупам до скоупа, содержащего квалификатор
                candidate = None
                for select in _enclosing_selects(column):
                    for ref, name in sources_of(select):
                        if ref == qualifier:
                            candidate = name
                            break
                    if candidate is not None:
                        break
                if candidate is None:
                    continue  # битый квалификатор — форм-отказ, оракул его не судит
                result.add((column.name.lower(), (candidate,)))
            else:
                names = tuple(name for _, name in sources_of(scope))
                if names:
                    result.add((column.name.lower(), names))
        return result

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT o.id FROM orders o INNER JOIN customers c ON o.customer_id = c.id",
            "SELECT o.id, c.name, c.region FROM orders o INNER JOIN customers c "
            "ON o.customer_id = c.id WHERE o.status = 'active' AND o.total > 100 LIMIT 10",
            "SELECT c.region, sum(o.total) OVER (PARTITION BY c.region) FROM orders o "
            "INNER JOIN customers c ON o.customer_id = c.id LIMIT 10",
            "SELECT DISTINCT ON (c.region) c.region, c.name FROM orders o "
            "INNER JOIN customers c ON o.customer_id = c.id LIMIT 10",
            "SELECT c.region, count(*) FROM orders o INNER JOIN customers c "
            "ON o.customer_id = c.id GROUP BY GROUPING SETS ((c.region), ()) LIMIT 10",
            "SELECT o.id FROM orders o INNER JOIN customers c ON o.customer_id = c.id "
            "WHERE EXISTS (SELECT 1 FROM customers c2 WHERE c2.region = c.region) LIMIT 10",
            "SELECT o.id FROM orders o INNER JOIN customers c ON o.customer_id = c.id "
            "WHERE EXISTS (SELECT l.x FROM orders o2, LATERAL (SELECT o2.customer_id AS x) l "
            "WHERE l.x = c.id) LIMIT 10",
            "SELECT o.* FROM orders o INNER JOIN customers c ON o.customer_id = c.id LIMIT 10",
            "SELECT o.total::text, extract(year FROM o.created_at) FROM orders o "
            "INNER JOIN customers c ON o.customer_id = c.id LIMIT 10",
            "SELECT o.id FROM orders o INNER JOIN customers c ON o.customer_id = c.id "
            "WHERE o.total BETWEEN 10 AND 100 AND o.status LIKE 'act%' LIMIT 10",
            "SELECT o.id FROM orders o INNER JOIN customers c ON o.customer_id = c.id "
            "WHERE o.customer_id IN (SELECT c2.id FROM customers c2 WHERE c2.region = 'RU') "
            "LIMIT 10",
            "SELECT CASE WHEN o.total > 100 THEN 'big' ELSE 'small' END FROM orders o "
            "INNER JOIN customers c ON o.customer_id = c.id LIMIT 10",
            "SELECT o.id FROM orders o INNER JOIN customers c ON o.customer_id = c.id "
            "WHERE coalesce(o.status, 'none') = lower(c.name) GROUP BY c.name "
            "ORDER BY c.name LIMIT 10",
            "SELECT o.id FROM orders o INNER JOIN (SELECT id, region FROM customers) cs "
            "ON o.customer_id = cs.id LIMIT 10",
        ],
    )
    def test_validator_columns_equal_oracle(self, sql: str):
        audit = build_sql_audit(sql)
        validator_set = {(c.name, tuple(s.name for s in c.candidates)) for c in audit.columns}
        assert validator_set == self._oracle_columns(sql), sql


def _enclosing_selects(node: exp.Expression) -> Iterator[exp.Select]:
    """Объемлющие SELECT'ы от ближайшего к корню (для подъёма квалификатора)."""
    current = node.parent
    while current is not None:
        if type(current) is exp.Select:
            yield current
        current = current.parent

"""Юнит-тесты S31 Task 1: validate_free_sql/validate_free — свободный вход
тул-контура за тем же рубежом (D-033 п.5 вариант A; R1/B2 ARCH-GATE).

Покрытие:
- общий ACL с FSM-входом: функции/таблицы/колонки/звёзды/LIMIT — один валидатор,
  две политики (правило 2 сессии S31, второй копии ACL нет);
- инвариант реестра (R1): пары операндов JOIN — рёбра активного снимка
  (выдуманная связь → hallucination; comma/cross — та же цепочка источников);
- fail-closed несверяемых форм: производный источник (CTE/подзапрос/LATERAL)
  в операнде JOIN ИЛИ в корреляции, соединение ≥3 источников без равенств
  колонок — обёртка связь не легализует (ruling «CTE+CTE разрешена» отозван);
- adversarial-корпус через validate_free (B2: denied в FSM без скелетной причины
  → denied и здесь; корневые UNION/CTE/LATERAL/подзапрос с чужой таблицей —
  security.md §3, ADR-005 §4 п.8 «adversarial-тесты обязательны»).

Сервис детерминирован (без LLM/IO) — моков не требует (S00 правило 7).
"""

from typing import Any

import pytest

from text_to_sql_agent.application.ports.sql_executor import ROW_LIMIT
from text_to_sql_agent.application.services.validate_sql_service import (
    ValidateSqlService,
    build_sql_audit,
)
from text_to_sql_agent.domain.sql_validation import (
    SqlAudit,
    ValidationOutcome,
    validate_free_sql,
)

# --- Фикстуры мира агента (S00 контракт 3: {table: [columns]}, '*' — все) ---

ORDERS_COLUMNS = ("id", "customer_id", "campaign_id", "total", "status", "created_at")
CUSTOMERS_COLUMNS = ("id", "name", "region")
CAMPAIGNS_COLUMNS = ("id", "name", "channel", "started_at")

PERMISSIONS: dict[str, list[str]] = {
    "orders": list(ORDERS_COLUMNS),
    "customers": list(CUSTOMERS_COLUMNS),
    "campaigns": list(CAMPAIGNS_COLUMNS),
}
# Каталог мира агента (agent_data_sources — SoT, BL-009): вне прав различим от
# несуществующей (ruling правило 4 S11)
CATALOG = frozenset({"orders", "customers", "bonuses", "campaigns", "order_items"})
# Активный снимок реестра (контракт SchemaGraphStore.join_edges — sorted lowercase
# пары): в мире одна связь — customers↔orders (демо-ребро e_customers_orders)
JOIN_EDGES = frozenset({("customers", "orders")})
# Цепочка customers→orders→campaigns — оба ребра на месте
CHAIN_EDGES = frozenset({("customers", "orders"), ("campaigns", "orders")})

_SERVICE = ValidateSqlService()


def _free(sql: str, **overrides: Any):
    """validate_free с миром фикстур; оверрайды — для изолированных сценариев."""
    return _SERVICE.validate_free(
        sql,
        permissions=overrides.get("permissions", PERMISSIONS),
        data_catalog=overrides.get("data_catalog", CATALOG),
        row_limit=overrides.get("row_limit", ROW_LIMIT),
        join_edges=overrides.get("join_edges", JOIN_EDGES),
    )


# --- Форма (общий парсинг с FSM-входом — fail-closed) ---


def test_dml_rejected_by_form() -> None:
    result = _free("UPDATE orders SET total = 0 WHERE id = 1")
    assert result.verdict.outcome is ValidationOutcome.HALLUCINATION
    assert "по форме" in result.verdict.issues[0]


def test_second_statement_rejected() -> None:
    result = _free("SELECT id FROM orders LIMIT 1; DROP TABLE orders")
    assert result.verdict.outcome is ValidationOutcome.HALLUCINATION


def test_set_command_rejected() -> None:
    result = _free("SET search_path = public")
    assert result.verdict.outcome is ValidationOutcome.HALLUCINATION


def test_lock_clause_rejected() -> None:
    # FOR UPDATE — узел Lock вне whitelist'а (deny-by-default)
    result = _free("SELECT id FROM orders WHERE id = 1 FOR UPDATE LIMIT 1")
    assert result.verdict.outcome is ValidationOutcome.HALLUCINATION


def test_function_outside_allowlist_rejected() -> None:
    result = _free("SELECT pg_sleep(1)")
    assert result.verdict.outcome is ValidationOutcome.HALLUCINATION
    assert any("pg_sleep" in issue for issue in result.verdict.issues)


# --- Общий ACL с FSM-входом (таблицы/схемы/колонки) ---


def test_agent_infrastructure_table_denied() -> None:
    result = _free("SELECT role FROM agent_permissions LIMIT 10")
    assert result.verdict.outcome is ValidationOutcome.DENIED


def test_foreign_schema_denied() -> None:
    result = _free("SELECT usename FROM pg_catalog.pg_roles LIMIT 10")
    assert result.verdict.outcome is ValidationOutcome.DENIED


def test_table_outside_catalog_is_hallucination() -> None:
    # несуществующая в мире — hallucination (ретрай оправдан), не denied
    result = _free("SELECT amount FROM payroll LIMIT 10")
    assert result.verdict.outcome is ValidationOutcome.HALLUCINATION


def test_catalog_table_without_rights_denied() -> None:
    # существует, но вне прав — degrade без retry (сигнал безопасности)
    result = _free("SELECT amount FROM bonuses LIMIT 10")
    assert result.verdict.outcome is ValidationOutcome.DENIED


def test_column_outside_rights_denied() -> None:
    # пара операндов на ребре — отказ именно от колонки, не от связи
    result = _free("SELECT c.nickname FROM orders o JOIN customers c ON o.customer_id = c.id")
    assert result.verdict.outcome is ValidationOutcome.DENIED
    assert any("nickname" in issue for issue in result.verdict.issues)


def test_cte_body_foreign_table_denied() -> None:
    result = _free("WITH x AS (SELECT amount FROM bonuses) SELECT * FROM x")
    assert result.verdict.outcome is ValidationOutcome.DENIED


# --- Звёзды и LIMIT (общая политика с FSM-входом, ruling R1/R2 S11) ---


def test_star_expanded_and_limit_added() -> None:
    result = _free("SELECT * FROM orders")
    assert result.verdict.outcome is ValidationOutcome.OK
    assert "*" not in result.sql  # литеральная звезда не доходит до БД
    for column in ORDERS_COLUMNS:
        assert column in result.sql
    assert f"LIMIT {ROW_LIMIT}" in result.sql


def test_explicit_limit_clamped() -> None:
    result = _free("SELECT id FROM orders LIMIT 5000")
    assert result.verdict.outcome is ValidationOutcome.OK
    assert f"LIMIT {ROW_LIMIT}" in result.sql
    assert "5000" not in result.sql


def test_smaller_limit_preserved() -> None:
    result = _free("SELECT id FROM orders LIMIT 10")
    assert result.verdict.outcome is ValidationOutcome.OK
    assert "LIMIT 10" in result.sql


# --- Инвариант реестра (R1): джойн только по рёбрам снапшота ---


def test_registry_edge_join_ok() -> None:
    result = _free("SELECT o.total FROM orders o JOIN customers c ON o.customer_id = c.id")
    assert result.verdict.outcome is ValidationOutcome.OK


def test_invented_join_hallucination() -> None:
    # обе таблицы разрешены, но связи нет — «джойн придуман» (ADR-006)
    result = _free("SELECT o.total FROM orders o JOIN campaigns c ON o.campaign_id = c.id")
    assert result.verdict.outcome is ValidationOutcome.HALLUCINATION
    assert any("не существует в реестре" in issue for issue in result.verdict.issues)


def test_comma_join_invented_hallucination() -> None:
    result = _free("SELECT o.id FROM orders o, campaigns c WHERE o.campaign_id = c.id")
    assert result.verdict.outcome is ValidationOutcome.HALLUCINATION


def test_cross_join_invented_hallucination() -> None:
    result = _free("SELECT o.id FROM orders o CROSS JOIN campaigns c LIMIT 10")
    assert result.verdict.outcome is ValidationOutcome.HALLUCINATION


def test_reversed_direction_same_edge() -> None:
    # направление равенства/джойна не семантично — пара отсортирована
    result = _free("SELECT c.id FROM customers c JOIN orders o ON c.id = o.customer_id")
    assert result.verdict.outcome is ValidationOutcome.OK


def test_chain_needs_every_edge() -> None:
    sql = (
        "SELECT c.id FROM customers c JOIN orders o ON c.id = o.customer_id "
        "JOIN campaigns m ON o.campaign_id = m.id"
    )
    ok = _free(sql, join_edges=CHAIN_EDGES)
    assert ok.verdict.outcome is ValidationOutcome.OK
    missing = _free(sql)  # ребра orders↔campaigns нет
    assert missing.verdict.outcome is ValidationOutcome.HALLUCINATION


def test_schema_qualified_tables_same_pair() -> None:
    # квалификация схемой — pair остаётся bare-именами
    audit = build_sql_audit(
        "SELECT o.id FROM public.orders o JOIN public.customers c ON o.customer_id = c.id"
    )
    assert audit.join_table_pairs == (("customers", "orders"),)


def test_no_join_no_pairs() -> None:
    audit = build_sql_audit("SELECT id FROM orders")
    assert audit.join_table_pairs == ()
    assert audit.unresolved_join_scopes is False


# --- Несверяемые формы (fail-closed): производный операнд, comma ≥3 ---


def test_table_cte_mix_unresolved() -> None:
    # таблица + CTE — пара реестром не выражается: fail-closed hallucination
    result = _free(
        "WITH x AS (SELECT id FROM customers) "
        "SELECT o.id FROM orders o JOIN x ON x.id = o.customer_id"
    )
    assert result.verdict.outcome is ValidationOutcome.HALLUCINATION
    assert any("не сверяется с реестром" in issue for issue in result.verdict.issues)


def test_table_subquery_mix_unresolved() -> None:
    result = _free(
        "SELECT o.id FROM orders o JOIN (SELECT id FROM customers) c ON c.id = o.customer_id"
    )
    assert result.verdict.outcome is ValidationOutcome.HALLUCINATION


def test_cte_cte_join_unresolved() -> None:
    # Ревью S31 C1 (регрессия, кусает до-фикс: было OK по ruling «CTE+CTE —
    # алгебра над телами», отозван): обёртка CTE не делает связь легальной —
    # CROSS JOIN двух производных источников = выдуманная связь своих таблиц
    result = _free(
        "WITH x AS (SELECT id, customer_id FROM orders), "
        "y AS (SELECT id, name FROM customers) "
        "SELECT x.id FROM x CROSS JOIN y"
    )
    assert result.verdict.outcome is ValidationOutcome.HALLUCINATION
    assert any("не сверяется с реестром" in issue for issue in result.verdict.issues)


def test_cte_invented_join_inside_body_hallucination() -> None:
    # выдуманная связь внутри тела CTE — та же проверка реестра
    result = _free(
        "WITH x AS (SELECT o.id FROM orders o JOIN campaigns c ON o.campaign_id = c.id) "
        "SELECT * FROM x"
    )
    assert result.verdict.outcome is ValidationOutcome.HALLUCINATION


def test_unresolved_join_scopes_flag_in_ir() -> None:
    audit = build_sql_audit(
        "SELECT o.id FROM orders o JOIN (SELECT id FROM customers) c ON c.id = o.customer_id"
    )
    assert audit.unresolved_join_scopes is True
    assert audit.join_table_pairs == ()


# --- Контракты входа (fail-closed wiring) ---


def test_row_limit_must_be_positive() -> None:
    audit = build_sql_audit("SELECT id FROM orders")
    with pytest.raises(ValueError, match="row_limit"):
        validate_free_sql(
            audit,
            permissions=PERMISSIONS,
            data_catalog=CATALOG,
            row_limit=0,
            join_edges=JOIN_EDGES,
        )


def test_permissions_contract_csv_rejected() -> None:
    # BL-014: CSV-строки валидатор не парсит — честный ValueError
    audit = build_sql_audit("SELECT id FROM orders")
    with pytest.raises(ValueError, match="BL-014"):
        validate_free_sql(
            audit,
            permissions={"orders": "id,total"},  # type: ignore[dict-item]
            data_catalog=CATALOG,
            row_limit=ROW_LIMIT,
            join_edges=JOIN_EDGES,
        )


# --- Adversarial-корпус через свободный вход (B2 ARCH-GATE) ---


class TestAdversarialGalleryFreeMode:
    """Корпус §4 п.8 (ADR-005) прогнан через validate_free (B2 S31): denied в FSM
    БЕЗ скелетной причины — denied и здесь. Осознанные отклонения от FSM-исходов
    (поправка ADR-002 §5): (а) корневой UNION своих таблиц — легален (скелета нет,
    тела проверяются теми же правилами); (б) LATERAL рядом с таблицей —
    hallucination (смешанный скоуп несверяем — fail-closed)."""

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
            # подзапрос с ЧУЖОЙ таблицей в теле — denied (ACL тела)
            (
                "SELECT o.id FROM orders o WHERE o.id IN "
                "(SELECT employee_id FROM bonuses) LIMIT 10",
                ValidationOutcome.DENIED,
            ),
            # подзапрос в FROM с чужой таблицей: denied (тело) доминирует над
            # несверяемой парой смешанного скоупа (обе причины в issues)
            (
                "SELECT o.id FROM orders o JOIN (SELECT employee_id FROM bonuses) b "
                "ON b.employee_id = o.id LIMIT 10",
                ValidationOutcome.DENIED,
            ),
            # window OVER — тело проверено, корень цел (ребро на месте)
            (
                "SELECT c.region, sum(o.total) OVER (PARTITION BY c.region) FROM orders o "
                "INNER JOIN customers c ON o.customer_id = c.id LIMIT 10",
                ValidationOutcome.OK,
            ),
            # EXISTS с корреляцией через скоуп-цепочку — оба скоупа однотабличные
            (
                "SELECT o.id FROM orders o INNER JOIN customers c ON o.customer_id = c.id "
                "WHERE EXISTS (SELECT 1 FROM customers c2 WHERE c2.region = c.region) LIMIT 10",
                ValidationOutcome.OK,
            ),
            # LATERAL рядом с таблицей — несверяемый смешанный скоуп (отклонение (б))
            (
                "SELECT o.id FROM orders o INNER JOIN customers c ON o.customer_id = c.id "
                "WHERE EXISTS (SELECT l.x FROM orders o2, LATERAL (SELECT o2.customer_id AS x) l "
                "WHERE l.x = c.id) LIMIT 10",
                ValidationOutcome.HALLUCINATION,
            ),
            # DISTINCT ON / GROUPING SETS — форма легальна, права на месте
            (
                "SELECT DISTINCT ON (c.region) c.region, c.name FROM orders o "
                "INNER JOIN customers c ON o.customer_id = c.id LIMIT 10",
                ValidationOutcome.OK,
            ),
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
            (
                "SELECT o.id FROM orders o INNER JOIN customers c ON o.customer_id = c.id "
                "WHERE o.status = 'x'; DROP TABLE orders; --'",
                ValidationOutcome.HALLUCINATION,
            ),
            # комментарий не прячет структуру (AST прозрачен)
            (
                "SELECT o.id FROM orders o /* добраться мимо */ "
                "INNER JOIN customers c ON o.customer_id = c.id",
                ValidationOutcome.OK,
            ),
            # корневой UNION СВОИХ таблиц — легален в свободном режиме (отклонение (а))
            (
                "SELECT o.id FROM orders o UNION ALL SELECT c.id FROM customers c LIMIT 10",
                ValidationOutcome.OK,
            ),
        ],
    )
    def test_gallery_scene_classified_by_code(self, sql: str, expected: ValidationOutcome):
        result = _free(sql)
        assert result.verdict.outcome is expected

    def test_string_literal_is_not_parsed_as_sql(self) -> None:
        # S6: содержимое литерала не порождает узлов AST
        result = _free(
            "SELECT o.id FROM orders o INNER JOIN customers c ON o.customer_id = c.id "
            "WHERE o.status = 'x; DROP TABLE orders; --'"
        )
        assert result.verdict.outcome is ValidationOutcome.OK
        assert "DROP" not in result.sql.replace("'x; DROP TABLE orders; --'", "")


# --- IR: пары операндов (сверка контракта SqlAudit) ---


def test_ir_pairs_sorted_deduplicated() -> None:
    audit: SqlAudit = build_sql_audit(
        "SELECT c.id FROM customers c JOIN orders o ON c.id = o.customer_id "
        "JOIN customers c2 ON c2.id = o.customer_id"
    )
    # customers↔orders (двойная цепочка — дубликат срезан)
    assert audit.join_table_pairs == (("customers", "orders"),)


# --- Fix-волна ревью S31: C1 (корреляции), I1 (comma >=3), self-join ---


def test_cte_cross_join_of_foreign_pair_unresolved() -> None:
    # Ревью C1, живое воспроизведение: два CTE над РАЗРЕШЁННЫМИ таблицами без
    # ребра — выдуманная связь через обёртку; регрессия кусает до-фикс (было ok)
    result = _free(
        "WITH x AS (SELECT id FROM orders), y AS (SELECT id FROM campaigns) "
        "SELECT x.id FROM x CROSS JOIN y"
    )
    assert result.verdict.outcome is ValidationOutcome.HALLUCINATION


def test_correlated_subquery_invented_pair_hallucination() -> None:
    # Ревью C1: корреляция EXISTS утверждает связь orders↔campaigns — сверяется
    # с реестром как пара JOIN; ребра нет → hallucination (кусает до-фикс: было ok)
    result = _free(
        "SELECT o.id FROM orders o WHERE EXISTS "
        "(SELECT 1 FROM campaigns c WHERE c.id = o.campaign_id)"
    )
    assert result.verdict.outcome is ValidationOutcome.HALLUCINATION
    assert any("не существует в реестре" in issue for issue in result.verdict.issues)


def test_correlated_subquery_on_real_edge_ok() -> None:
    # Легальная корреляция по настоящему ребру реестра проходит
    result = _free(
        "SELECT o.id FROM orders o WHERE EXISTS "
        "(SELECT 1 FROM customers c WHERE c.id = o.customer_id)",
        join_edges=CHAIN_EDGES,
    )
    assert result.verdict.outcome is ValidationOutcome.OK


def test_self_join_needs_no_edge() -> None:
    # Ревью S31 (set aside C1): self-join таблицы к себе связи между РАЗНЫМИ
    # таблицами не утверждает — симметрично с self-join через два CTE
    result = _free("SELECT o.id FROM orders o JOIN orders o2 ON o2.customer_id = o.customer_id")
    assert result.verdict.outcome is ValidationOutcome.OK


def test_comma_three_tables_unresolved() -> None:
    # Ревью S31 I1: comma-FROM на >=3 табличных источников — «соседние» пары
    # произвольны (дыра a-c и ложный отказ легального хаба) — форма fail-closed
    result = _free("SELECT o.id FROM orders o, customers c, campaigns m WHERE o.customer_id = c.id")
    assert result.verdict.outcome is ValidationOutcome.HALLUCINATION
    assert any("не сверяется с реестром" in issue for issue in result.verdict.issues)


def test_comma_two_tables_pair_checked() -> None:
    # Двухтабличный comma — единственная пара, та же проверка цепочки
    ok = _free("SELECT o.id FROM orders o, customers c WHERE o.customer_id = c.id")
    assert ok.verdict.outcome is ValidationOutcome.OK
    invented = _free("SELECT o.id FROM orders o, campaigns m WHERE o.campaign_id = m.id")
    assert invented.verdict.outcome is ValidationOutcome.HALLUCINATION


# --- Re-review S31 fix-волна 2: корреляция-обёртка (Critical), предикат-пары (I3),
# --- comma-регрессия по исходу (M9), USING/ON TRUE (M11)

# Полная цепочка треугольника customers↔orders↔campaigns (+ хорда) — все соседние
# пары comma-FROM на рёбрах: до-фикс (пары цепочки без детектора ≥3) давал ok.
# Контракт join_edges — sorted lowercase пары (как отдаёт SchemaGraphStore.join_edges)
ALL_TRIANGLE_EDGES = frozenset(
    {("customers", "orders"), ("campaigns", "orders"), ("campaigns", "customers")}
)


def test_correlation_on_outer_cte_unresolved() -> None:
    # Re-review Critical-1 (регрессия, кусает eb6d0cd: было ok): корреляция на
    # производном источнике ВНЕШНЕГО скоупа — обёртка CTE не легализует связь
    # customers↔campaigns (ребра нет) — fail-closed, как JOIN-операнд
    result = _free(
        "WITH s AS (SELECT id, region FROM customers) "
        "SELECT s.id FROM s WHERE EXISTS "
        "(SELECT 1 FROM campaigns m WHERE m.channel = s.region)"
    )
    assert result.verdict.outcome is ValidationOutcome.HALLUCINATION


def test_correlation_on_derived_table_unresolved() -> None:
    # Та же дыра с производной таблицей во FROM внешнего скоупа (было ok)
    result = _free(
        "SELECT s.id FROM (SELECT id, region FROM customers) s WHERE EXISTS "
        "(SELECT 1 FROM campaigns m WHERE m.channel = s.region)"
    )
    assert result.verdict.outcome is ValidationOutcome.HALLUCINATION


def test_correlation_on_cte_fail_closed_even_on_real_edge() -> None:
    # Консистентность с JOIN-операндами (set aside re-review): корреляция на CTE
    # fail-closed ДАЖЕ по реальному ребру customers↔orders — единое правило без
    # исключений; модель перепишет запрос на таблицы (issues объясняют)
    result = _free(
        "WITH s AS (SELECT id FROM customers) "
        "SELECT s.id FROM s WHERE EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = s.id)"
    )
    assert result.verdict.outcome is ValidationOutcome.HALLUCINATION
    assert any("не сверяется с реестром" in issue for issue in result.verdict.issues)


def test_correlation_pairs_only_predicate_tables() -> None:
    # Re-review I3 (регрессия, кусает eb6d0cd: ложный отказ campaigns↔customers):
    # корреляция паруется с таблицами СВОЕГО сравнения (orders), локальный JOIN
    # o⋈c не приписывает связь m↔c — законный запрос проходит
    result = _free(
        "SELECT m.id FROM campaigns m WHERE EXISTS "
        "(SELECT 1 FROM orders o JOIN customers c ON c.id = o.customer_id "
        "WHERE o.campaign_id = m.id)",
        join_edges=CHAIN_EDGES,
    )
    assert result.verdict.outcome is ValidationOutcome.OK
    # а выдуманная связь в самом сравнении корреляции по-прежнему ловится
    invented = _free(
        "SELECT m.id FROM campaigns m WHERE EXISTS "
        "(SELECT 1 FROM orders o JOIN customers c ON c.id = o.customer_id "
        "WHERE c.region = m.channel)",
        join_edges=CHAIN_EDGES,
    )
    assert invented.verdict.outcome is ValidationOutcome.HALLUCINATION
    assert any("campaigns↔customers" in issue for issue in invented.verdict.issues)


def test_comma_three_tables_unresolved_even_with_all_edges() -> None:
    # Re-review M9: регрессия кусает до-фикс ПО ИСХОДУ (было ok на 4c27e01) —
    # все соседние пары на рёбрах, fail-closed именно из-за произвольности
    # «соседних» пар comma при ≥3 источниках
    result = _free(
        "SELECT o.id FROM customers c, orders o, campaigns m "
        "WHERE o.customer_id = c.id AND o.campaign_id = m.id",
        join_edges=ALL_TRIANGLE_EDGES,
    )
    assert result.verdict.outcome is ValidationOutcome.HALLUCINATION
    assert any("не сверяется с реестром" in issue for issue in result.verdict.issues)


def test_using_join_is_pair_checked_not_cartesian() -> None:
    # Re-review M11: USING — равенство по именованным колонкам утверждено, это
    # НЕ неявное произведение: цепочка из 3 источников с USING не fail-closed
    # как comma (детектор различает), пары цепочки сверяются как у явного JOIN...ON
    ok = _free("SELECT o.id FROM orders o JOIN customers c USING (customer_id) LIMIT 10")
    assert ok.verdict.outcome is ValidationOutcome.OK
    three = _free(
        "SELECT o.id FROM orders o JOIN customers c USING (customer_id) "
        "JOIN customers c2 USING (customer_id) LIMIT 10"
    )
    assert three.verdict.outcome is ValidationOutcome.OK  # orders↔customers, self-join skip


def test_on_true_is_cartesian_fail_closed_on_three_tables() -> None:
    # Re-review M11: ON TRUE — произведение явным синтаксисом (равенств нет):
    # на ≥3 источниках та же fail-closed семантика, что у comma; на двух —
    # единственная пара цепочки, сверяется
    result = _free(
        "SELECT o.id FROM orders o JOIN customers c ON TRUE JOIN campaigns m ON TRUE",
        join_edges=ALL_TRIANGLE_EDGES,
    )
    assert result.verdict.outcome is ValidationOutcome.HALLUCINATION
    assert any("не сверяется с реестром" in issue for issue in result.verdict.issues)
    two = _free("SELECT o.id FROM orders o JOIN customers c ON TRUE LIMIT 10")
    assert two.verdict.outcome is ValidationOutcome.OK


# --- Re-review fix-волна 3 (I-1): критерий произведения — СЕМАНТИЧЕСКИЙ:
# --- JOIN без равенств колонок двух РАЗНЫХ источников скоупа — произведение,
# --- чем бы оно ни было записано (не синтаксический whitelist comma/ON TRUE)


def test_cross_join_three_tables_fail_closed() -> None:
    # Re-review I-1 (регрессия, кусает c7a4dcd: было ok): CROSS JOIN на ≥3
    # табличных источниках — то же произведение без равенств, что comma:
    # строки campaigns комбинируются с customers без ребра реестра
    result = _free(
        "SELECT m.id FROM campaigns m CROSS JOIN orders o CROSS JOIN customers c",
        join_edges=CHAIN_EDGES,
    )
    assert result.verdict.outcome is ValidationOutcome.HALLUCINATION
    assert any("не сверяется с реестром" in issue for issue in result.verdict.issues)


def test_on_literal_is_cartesian_fail_closed_on_three_tables() -> None:
    # Re-review I-1 (кусает c7a4dcd: ON 1=1 — не exp.Boolean, детектор молчал):
    # «всегда истинное» условие равенств колонок не утверждает — произведение
    result = _free(
        "SELECT m.id FROM campaigns m JOIN orders o ON 1 = 1 JOIN customers c ON 1 = 1",
        join_edges=CHAIN_EDGES,
    )
    assert result.verdict.outcome is ValidationOutcome.HALLUCINATION
    assert any("не сверяется с реестром" in issue for issue in result.verdict.issues)


def test_on_true_in_parens_is_cartesian_fail_closed() -> None:
    # Re-review I-1 (кусает c7a4dcd: (TRUE) парсится как Paren, не exp.Boolean):
    # скобки семантику не меняют — произведение
    result = _free(
        "SELECT m.id FROM campaigns m JOIN orders o ON (TRUE) JOIN customers c ON (TRUE)",
        join_edges=CHAIN_EDGES,
    )
    assert result.verdict.outcome is ValidationOutcome.HALLUCINATION


def test_on_same_source_comparison_is_cartesian() -> None:
    # Re-review I-1 (кусает c7a4dcd): самосравнение m.id = m.id — колонки ОДНОГО
    # источника по обе стороны, связь двух источников не утверждается
    result = _free(
        "SELECT m.id FROM campaigns m JOIN orders o ON m.id = m.id JOIN customers c ON o.id = o.id",
        join_edges=CHAIN_EDGES,
    )
    assert result.verdict.outcome is ValidationOutcome.HALLUCINATION


def test_on_subquery_equality_does_not_tie_join_sources() -> None:
    # Re-review I-1: равенство ВНУТРИ подзапроса ON (колонки вложенного скоупа)
    # не связывает источники внешнего джойна — c⋈m без равенств fail-closed;
    # контраст: то же соединение с настоящим равенством колонок проходит
    # (порядок источников — оба соседних пары на рёбрах, отличие только в ON)
    fail = _free(
        "SELECT c.id FROM campaigns m JOIN orders o ON m.id = o.campaign_id "
        "JOIN customers c ON EXISTS "
        "(SELECT 1 FROM customers c2 WHERE c2.id = o.customer_id)",
        join_edges=CHAIN_EDGES,
    )
    assert fail.verdict.outcome is ValidationOutcome.HALLUCINATION
    assert any("не сверяется с реестром" in issue for issue in fail.verdict.issues)
    ok = _free(
        "SELECT c.id FROM campaigns m JOIN orders o ON m.id = o.campaign_id "
        "JOIN customers c ON c.id = o.customer_id",
        join_edges=CHAIN_EDGES,
    )
    assert ok.verdict.outcome is ValidationOutcome.OK


def test_cross_join_two_tables_pair_checked() -> None:
    # Set aside re-review (сохраняется): на двух табличных источниках правило
    # произведения НЕ расширяется — единственная пара цепочки сверяется как раньше
    ok = _free("SELECT o.id FROM orders o CROSS JOIN customers c LIMIT 10", join_edges=CHAIN_EDGES)
    assert ok.verdict.outcome is ValidationOutcome.OK
    invented = _free("SELECT m.id FROM campaigns m CROSS JOIN customers c", join_edges=CHAIN_EDGES)
    assert invented.verdict.outcome is ValidationOutcome.HALLUCINATION
    assert any("campaigns↔customers" in issue for issue in invented.verdict.issues)


# --- Re-review fix-волна 4 (I-1/I-2): функциональный критерий связи —
# --- равенство обязано ЗАДЕВАТЬ присоединяемый источник и уже присоединённые
# --- (не «существует EQ двух источников»), и стоять в конъюнктивном скелете ON


def test_on_conjunct_not_tied_to_joined_source() -> None:
    # Re-review волна 4 (регрессия, кусает 6a68c2d: было ok): равенство
    # m.id = o.campaign_id не задевает присоединяемый c — c соединяется
    # безусловно, произведение campaign×customer без ребра реестра
    result = _free(
        "SELECT c.id FROM campaigns m JOIN orders o ON m.id = o.campaign_id "
        "JOIN customers c ON m.id = o.campaign_id",
        join_edges=CHAIN_EDGES,
    )
    assert result.verdict.outcome is ValidationOutcome.HALLUCINATION
    assert any("не сверяется с реестром" in issue for issue in result.verdict.issues)
    # негатив-компаньон: равенство связывает c с ранее присоединённым o —
    # локальный фильтр-неравенство ложных отказов не добавляет
    ok = _free(
        "SELECT c.id FROM campaigns m JOIN orders o ON m.id = o.campaign_id "
        "JOIN customers c ON c.id = o.customer_id AND c.region <> 'X'",
        join_edges=CHAIN_EDGES,
    )
    assert ok.verdict.outcome is ValidationOutcome.OK


def test_on_same_source_conjunct_not_a_tie() -> None:
    # Та же дыра: самосравнение c.id = c.id не связывает c, связка в конъюнкте —
    # между ЧУЖИМИ источниками m/o (кусает 6a68c2d: было ok)
    result = _free(
        "SELECT c.id FROM campaigns m JOIN orders o ON m.id = o.campaign_id "
        "JOIN customers c ON c.id = c.id AND m.id = o.campaign_id",
        join_edges=CHAIN_EDGES,
    )
    assert result.verdict.outcome is ValidationOutcome.HALLUCINATION


def test_on_or_true_does_not_tie_join_sources() -> None:
    # Re-review волна 4 (кусает 6a68c2d: было ok): OR TRUE делает равенство
    # тавтологией — полный произведение по c при виде «связанного» джойна
    result = _free(
        "SELECT c.id FROM campaigns m JOIN orders o ON m.id = o.campaign_id "
        "JOIN customers c ON c.id = o.customer_id OR TRUE",
        join_edges=CHAIN_EDGES,
    )
    assert result.verdict.outcome is ValidationOutcome.HALLUCINATION


def test_or_branches_across_pairs_not_a_tie() -> None:
    # Re-review волна 4 (кусает 6a68c2d: было ok): дизъюнкция равенств —
    # ветка m=o не связывает c; связь утверждает только конъюнкция
    result = _free(
        "SELECT c.id FROM campaigns m JOIN orders o ON m.id = o.campaign_id "
        "JOIN customers c ON (c.id = o.customer_id) OR (m.id = o.campaign_id)",
        join_edges=CHAIN_EDGES,
    )
    assert result.verdict.outcome is ValidationOutcome.HALLUCINATION
    # негатив: связывающее равенство в КОНЪЮНКЦИИ с дополнительным сравнением
    # (даже с чужой парой c-m, обе уже на рёбрах через o) — проходит
    ok = _free(
        "SELECT c.id FROM campaigns m JOIN orders o ON m.id = o.campaign_id "
        "JOIN customers c ON (c.id = o.customer_id) AND (c.region = m.channel)",
        join_edges=CHAIN_EDGES,
    )
    assert ok.verdict.outcome is ValidationOutcome.OK


def test_on_case_is_not_a_tie() -> None:
    # Re-review волна 4 (кусает 6a68c2d: было ok): равенство внутри CASE —
    # условная связка, конъюнктивного скелета нет
    result = _free(
        "SELECT c.id FROM campaigns m JOIN orders o ON m.id = o.campaign_id "
        "JOIN customers c ON CASE WHEN TRUE THEN c.id = o.customer_id "
        "ELSE c.id = o.customer_id END",
        join_edges=CHAIN_EDGES,
    )
    assert result.verdict.outcome is ValidationOutcome.HALLUCINATION


def test_non_equi_on_is_fail_closed_on_three_tables() -> None:
    # Пин волны 4 (Minor 5 re-review 3): неравенства/range в ON равенств колонок
    # не утверждают — на ≥3 источниках fail-closed (канон «без равенств»);
    # поведение существует с волны 3 — без пина правка молча вернёт fail-open
    for condition in ("o.total > 5", "o.total BETWEEN 1 AND 5", "m.id <> o.campaign_id"):
        result = _free(
            f"SELECT c.id FROM campaigns m JOIN orders o ON {condition} "
            "JOIN customers c ON c.id = o.customer_id",
            join_edges=CHAIN_EDGES,
        )
        assert result.verdict.outcome is ValidationOutcome.HALLUCINATION, condition

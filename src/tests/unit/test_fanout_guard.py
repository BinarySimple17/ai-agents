"""Юнит-тесты feat-023 (S32): fan-out guard — валидатор не пускает двойной счёт.

Инвариант ADR-006 §4.6/ADR-004 (hw-01 §3.3, prd §6 «0 случаев двойного счёта»),
принятый каноном без реализации (аудит 2026-11-23, E3a). Правило — строгий
v1′ (ruling R2 ARCH-GATE S32: правило 3 плана в буквальной записи пропускало
двойной счёт — SUM стороны «1» остаётся ×N_i даже с GROUP BY по ключу):

- SUM и COUNT-без-distinct над колонкой стороны «1» guard-ребра → отклоняются
  ВСЕГДА (ключ GROUP BY не спасает: в группе зерна «1» N_i копий значения);
- AVG стороны «1» → отклоняется только без ключа джойна стороны «1» в GROUP BY
  (среднее N одинаковых = само значение — математически корректно);
- проходят: MIN/MAX, COUNT(DISTINCT), COUNT(*) (ловушка 3 — нет колонки),
  агрегаты стороны «n», агрегат целиком в подзапросе, план без guard-рёбер;
- исход нарушения — hallucination (retry оправдан: ошибка содержания SQL, не
  прав — ADR-005 §4), подсказка детерминированная и только выразимая.

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

# --- Фикстуры мира агента: план orders → order_items (guard, 1:n) ---

PERMISSIONS: dict[str, list[str]] = {
    "orders": ["order_id", "customer_id", "total", "status", "created_at"],
    "order_items": ["item_id", "order_id", "customer_id", "product_id", "amount", "quantity"],
    "customers": ["id", "name", "region"],
}
CATALOG = frozenset({"orders", "order_items", "customers", "products"})


def _guarded_plan(**overrides: Any) -> JoinPlan:
    """План с guard-ребром orders → order_items (аналог e_orders_items реестра:
    1:n, orders — сторона «1»). Ключи следуют своей таблице."""
    edges = overrides.get(
        "edges",
        [
            JoinPlanEdge(
                rel_id=overrides.get("rel_id", "rel-orders-items"),
                from_table="orders",
                to_table="order_items",
                from_columns=overrides.get("from_columns", ("order_id",)),
                to_columns=overrides.get("to_columns", ("order_id",)),
                join_type="inner",
                fanout_guard=True,
                fanout_one_side=overrides.get("fanout_one_side", "orders"),
            )
        ],
    )
    return JoinPlan(
        tables=["orders", "order_items"],
        edges=edges,
        graph_version=overrides.get("graph_version", 1),
    )


def _skeleton(plan: JoinPlan | None = None) -> Skeleton:
    return build_skeleton(plan or _guarded_plan())


def _validate(
    sql: str,
    *,
    plan: JoinPlan | None = None,
    skeleton_hash: str | None = None,
) -> ValidateSqlResult:
    """Прогон через сервис с фикстурами guard-плана (все входы обязательны)."""
    plan = plan or _guarded_plan()
    skeleton = _skeleton(plan)
    return ValidateSqlService().validate(
        sql,
        skeleton=skeleton,
        join_plan=plan,
        skeleton_hash=skeleton_hash if skeleton_hash is not None else skeleton.skeleton_hash,
        permissions={k: list(v) for k, v in PERMISSIONS.items()},
        data_catalog=CATALOG,
        row_limit=ROW_LIMIT,
    )


def _hint_issues(result: ValidateSqlResult) -> list[str]:
    return [i for i in result.verdict.issues if "fan-out" in i or "fanout" in i]


# --- Отклоняются: SUM/COUNT над стороной «1» — ВСЕГДА (ruling R2) ---


class TestSensitiveAlwaysRejected:
    def test_sum_one_side_rejected(self):
        result = _validate(
            "SELECT o.order_id, SUM(o.total) FROM orders o "
            "INNER JOIN order_items oi ON o.order_id = oi.order_id"
        )
        assert result.verdict.outcome is ValidationOutcome.HALLUCINATION
        hints = _hint_issues(result)
        assert len(hints) == 1
        assert "sum(total)" in hints[0]
        assert "orders" in hints[0] and "rel-orders-items" in hints[0]

    def test_sum_one_side_with_group_by_key_still_rejected(self):
        # ядро строгого v1′: ключ GROUP BY коллапсирует группу, но N_i копий
        # total в группе не убирает — SUM = N_i×total (план в буквальной
        # записи правила это пропускал — ruling R2)
        result = _validate(
            "SELECT o.order_id, SUM(o.total) FROM orders o "
            "INNER JOIN order_items oi ON o.order_id = oi.order_id GROUP BY o.order_id"
        )
        assert result.verdict.outcome is ValidationOutcome.HALLUCINATION
        assert _hint_issues(result)

    def test_count_one_side_rejected(self):
        result = _validate(
            "SELECT COUNT(o.order_id) FROM orders o "
            "INNER JOIN order_items oi ON o.order_id = oi.order_id"
        )
        assert result.verdict.outcome is ValidationOutcome.HALLUCINATION
        assert _hint_issues(result)

    def test_count_one_side_with_group_by_key_still_rejected(self):
        result = _validate(
            "SELECT o.order_id, COUNT(o.order_id) FROM orders o "
            "INNER JOIN order_items oi ON o.order_id = oi.order_id GROUP BY o.order_id"
        )
        assert result.verdict.outcome is ValidationOutcome.HALLUCINATION

    def test_sum_unqualified_column_with_one_side_candidate(self):
        # unqualified `total` — кандидаты все источники скоупа (Ловушка 5),
        # среди них сторона «1» → чувствителен (fail-closed)
        result = _validate(
            "SELECT SUM(total) FROM orders o INNER JOIN order_items oi ON o.order_id = oi.order_id"
        )
        assert result.verdict.outcome is ValidationOutcome.HALLUCINATION

    def test_sum_window_over_rejected(self):
        # обход через оконную форму невозможен: узел агрегата под exp.Window
        # доходит обходом IR (ruling R2, adversarial-корпус)
        result = _validate(
            "SELECT o.order_id, SUM(o.total) OVER () FROM orders o "
            "INNER JOIN order_items oi ON o.order_id = oi.order_id"
        )
        assert result.verdict.outcome is ValidationOutcome.HALLUCINATION

    def test_sum_in_having_rejected(self):
        result = _validate(
            "SELECT o.order_id FROM orders o "
            "INNER JOIN order_items oi ON o.order_id = oi.order_id GROUP BY o.order_id "
            "HAVING SUM(o.total) > 100"
        )
        assert result.verdict.outcome is ValidationOutcome.HALLUCINATION

    def test_sum_case_column_rejected(self):
        # колонка стороны «1» под CASE внутри агрегата (few-shot
        # «выручка-по-кампаниям» на guard-ребре)
        result = _validate(
            "SELECT SUM(CASE WHEN o.status = 'paid' THEN o.total END) FROM orders o "
            "INNER JOIN order_items oi ON o.order_id = oi.order_id"
        )
        assert result.verdict.outcome is ValidationOutcome.HALLUCINATION

    def test_sum_in_order_by_rejected(self):
        result = _validate(
            "SELECT o.order_id FROM orders o "
            "INNER JOIN order_items oi ON o.order_id = oi.order_id GROUP BY o.order_id "
            "ORDER BY SUM(o.total) DESC"
        )
        assert result.verdict.outcome is ValidationOutcome.HALLUCINATION

    def test_broken_one_side_rejects_sensitive(self):
        # чекпойнт-дрейф: guard без корректной стороны «1» — fail-closed
        # (validate_join_plan ловит на плане-времени; здесь — защита в глубину)
        plan = _guarded_plan(fanout_one_side="")
        result = _validate(
            "SELECT SUM(o.total) FROM orders o "
            "INNER JOIN order_items oi ON o.order_id = oi.order_id",
            plan=plan,
        )
        assert result.verdict.outcome is ValidationOutcome.HALLUCINATION
        assert any("план дефектен" in i for i in result.verdict.issues)


# --- Проходят: нечувствительные агрегаты и выразимые обходы ---


class TestAllowed:
    def test_avg_one_side_with_group_by_key_ok(self):
        # AVG N одинаковых в группе зерна «1» = само значение — корректно
        result = _validate(
            "SELECT o.order_id, AVG(o.total) FROM orders o "
            "INNER JOIN order_items oi ON o.order_id = oi.order_id GROUP BY o.order_id"
        )
        assert result.verdict.outcome is ValidationOutcome.OK

    def test_avg_one_side_without_key_rejected(self):
        result = _validate(
            "SELECT o.customer_id, AVG(o.total) FROM orders o "
            "INNER JOIN order_items oi ON o.order_id = oi.order_id GROUP BY o.customer_id"
        )
        assert result.verdict.outcome is ValidationOutcome.HALLUCINATION
        hints = _hint_issues(result)
        assert len(hints) == 1
        # выразимые фиксы в подсказке: GROUP BY по ключу джойна стороны «1»
        assert "сгруппируй" in hints[0] and "orders.order_id" in hints[0]

    def test_avg_composite_key_requires_all_columns(self):
        # составной ключ: AVG спасает только GROUP BY по ВСЕМ колонкам ключа
        plan = _guarded_plan(
            from_columns=("order_id", "customer_id"), to_columns=("order_id", "customer_id")
        )
        ok = _validate(
            "SELECT o.order_id, AVG(o.total) FROM orders o "
            "INNER JOIN order_items oi ON o.order_id = oi.order_id "
            "AND o.customer_id = oi.customer_id "
            "GROUP BY o.order_id, o.customer_id",
            plan=plan,
        )
        assert ok.verdict.outcome is ValidationOutcome.OK
        partial = _validate(
            "SELECT o.customer_id, AVG(o.total) FROM orders o "
            "INNER JOIN order_items oi ON o.order_id = oi.order_id "
            "AND o.customer_id = oi.customer_id "
            "GROUP BY o.customer_id",
            plan=plan,
        )
        assert partial.verdict.outcome is ValidationOutcome.HALLUCINATION

    def test_count_distinct_ok(self):
        result = _validate(
            "SELECT COUNT(DISTINCT o.order_id) FROM orders o "
            "INNER JOIN order_items oi ON o.order_id = oi.order_id"
        )
        assert result.verdict.outcome is ValidationOutcome.OK

    def test_count_star_ok(self):
        # ловушка 3: COUNT(*) без колонки под агрегатом — кратность строк не
        # переносится на агрегат; правило v1′ его не отклоняет (осознанно)
        result = _validate(
            "SELECT COUNT(*) FROM orders o INNER JOIN order_items oi ON o.order_id = oi.order_id"
        )
        assert result.verdict.outcome is ValidationOutcome.OK

    def test_min_max_ok(self):
        result = _validate(
            "SELECT MIN(o.total), MAX(o.created_at) FROM orders o "
            "INNER JOIN order_items oi ON o.order_id = oi.order_id"
        )
        assert result.verdict.outcome is ValidationOutcome.OK

    def test_sum_n_side_ok(self):
        # «сумма по заказу» — легальный few-shot паттерн (ловушка 2):
        # агрегат над стороной «n», GROUP BY по ключу стороны «1»
        result = _validate(
            "SELECT o.order_id, SUM(oi.amount) FROM orders o "
            "INNER JOIN order_items oi ON o.order_id = oi.order_id GROUP BY o.order_id"
        )
        assert result.verdict.outcome is ValidationOutcome.OK

    def test_aggregate_in_subquery_ok(self):
        # «агрегат целиком внутри подзапроса» (предагрегация — выразимый фикс,
        # таблицы подзапроса ⊆ плана, BL-051(a)); корневой скоуп без агрегатов
        result = _validate(
            "SELECT o.order_id, oi.amount, "
            "(SELECT SUM(o2.total) FROM orders o2 WHERE o2.order_id = o.order_id) AS order_total "
            "FROM orders o INNER JOIN order_items oi ON o.order_id = oi.order_id"
        )
        assert result.verdict.outcome is ValidationOutcome.OK

    def test_no_guard_edge_sum_ok(self):
        # план без guard-рёбер (orders → customers, n:1): SUM стороны «n»
        # обычного ребра политикой не трогается
        plan = JoinPlan(
            tables=["orders", "customers"],
            edges=[
                JoinPlanEdge(
                    rel_id="rel-orders-customers",
                    from_table="orders",
                    to_table="customers",
                    from_columns=("customer_id",),
                    to_columns=("id",),
                    join_type="inner",
                )
            ],
            graph_version=1,
        )
        result = _validate(
            "SELECT c.name, SUM(o.total) FROM orders o "
            "INNER JOIN customers c ON o.customer_id = c.id GROUP BY c.name",
            plan=plan,
        )
        assert result.verdict.outcome is ValidationOutcome.OK

    def test_group_by_positional_avg_conservative_reject(self):
        # residual v1 (ruling R4): позиционный GROUP BY не матчится с ключом —
        # консервативный reject с выразимым фиксом («группируй по ключу явно»)
        result = _validate(
            "SELECT o.order_id, AVG(o.total) FROM orders o "
            "INNER JOIN order_items oi ON o.order_id = oi.order_id GROUP BY 1"
        )
        assert result.verdict.outcome is ValidationOutcome.HALLUCINATION

    def test_denied_dominates_fanout_hallucination(self):
        # INTO + fanout: denied доминирует (_merge_outcome) — degrade без retry
        result = _validate(
            "SELECT o.order_id, SUM(o.total) INTO orders_copy FROM orders o "
            "INNER JOIN order_items oi ON o.order_id = oi.order_id"
        )
        assert result.verdict.outcome is ValidationOutcome.DENIED


# --- Подсказка: детерминированная, только выразимые фиксы (S9/rule 4) ---


class TestHintQuality:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT SUM(o.total) FROM orders o "
            "INNER JOIN order_items oi ON o.order_id = oi.order_id",
            "SELECT COUNT(o.order_id) FROM orders o "
            "INNER JOIN order_items oi ON o.order_id = oi.order_id",
        ],
    )
    def test_hint_offers_expressible_fixes_only(self, sql: str):
        result = _validate(sql)
        hints = _hint_issues(result)
        assert len(hints) == 1
        hint = hints[0]
        # подзапрос-предагрегация — выразимый обход в корне (скелет фиксирует
        # FROM/JOIN: «предагрегация в CTE» недостижима, ловушка 1); фикс
        # «сгруппируй» SUM/COUNT в подсказке не предлагается (R2 — упоминание
        # «GROUP BY по ключу не спасает» есть, но как объяснение, не как фикс)
        assert "в подзапросе" in hint
        assert "сгруппируй" not in hint
        # имена недоступных таблиц в подсказке нет (S9): все имена — план
        for unavailable in ("products", "customers"):
            assert unavailable not in hint

    def test_hint_deterministic(self):
        sql = (
            "SELECT SUM(o.total) FROM orders o "
            "INNER JOIN order_items oi ON o.order_id = oi.order_id"
        )
        first = _validate(sql).verdict.issues
        second = _validate(sql).verdict.issues
        assert first == second

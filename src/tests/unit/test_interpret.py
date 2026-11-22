"""Юнит-тесты финального контура FSM (feat-015, S15): compact_result /
interpret_result / degrade + BudgetGuard.

LLM в тестах — только моки (S00 правило 7): скриптованный двойник шлюза
(тул-выходы/сбои) + RecordingTracer — прецедент test_diagnose (S14).
Бюджетный шов и снапшот ₽ — инъекции (rulings R4/R6 ARCH-GATE S15).
"""

import pytest

from text_to_sql_agent.application.services.compact_result_node import (
    make_compact_result_node,
)
from text_to_sql_agent.domain.result_compact import (
    MAX_COMPACT_ROWS,
    ResultCompact,
    compact_rows,
)

State = dict


# --- Task 1: compact_result — код (VO ResultCompact + узел) ---


def _rows(count: int, value: int = 1) -> list[dict]:
    """count строк одной числовой колонки (детерминированные значения)."""
    return [{"amount": i * value, "region": f"r{i % 3}"} for i in range(count)]


class TestResultCompactVO:
    """VO ResultCompact — поля по ER (ruling R2 ARCH-GATE S15)."""

    def test_sample_truncated_to_max_rows(self) -> None:
        compact = compact_rows(_rows(100), truncated=False, approximate=False)
        assert compact.row_count == 100
        assert len(compact.sample) == MAX_COMPACT_ROWS == 20
        assert compact.sample[0] == {"amount": 0, "region": "r0"}

    def test_small_result_keeps_all_rows(self) -> None:
        rows = _rows(5)
        compact = compact_rows(rows, truncated=False, approximate=False)
        assert compact.row_count == 5
        assert compact.sample == rows

    def test_aggregates_numeric_column(self) -> None:
        rows = [{"amount": 10}, {"amount": 20}, {"amount": 30}]
        compact = compact_rows(rows, truncated=False, approximate=False)
        agg = compact.aggregates["amount"]
        assert agg.sum == 60
        assert agg.avg == 20
        assert agg.min == 10
        assert agg.max == 30

    def test_aggregates_skip_non_numeric_columns(self) -> None:
        rows = [{"amount": 1, "region": "r"}, {"amount": 2, "region": "r"}]
        compact = compact_rows(rows, truncated=False, approximate=False)
        assert "amount" in compact.aggregates
        assert "region" not in compact.aggregates

    def test_bool_is_not_numeric(self) -> None:
        # bool — подкласс int в Python: без явного исключения «активные» считались бы
        rows = [{"active": True}, {"active": False}]
        compact = compact_rows(rows, truncated=False, approximate=False)
        assert "active" not in compact.aggregates

    def test_aggregates_skip_none_values(self) -> None:
        rows = [{"amount": None}, {"amount": 4}, {"amount": 6}]
        compact = compact_rows(rows, truncated=False, approximate=False)
        assert compact.aggregates["amount"].sum == 10

    def test_all_none_column_has_no_aggregate(self) -> None:
        rows = [{"amount": None}, {"amount": None}]
        compact = compact_rows(rows, truncated=False, approximate=False)
        assert "amount" not in compact.aggregates

    def test_mixed_type_column_has_no_aggregate(self) -> None:
        # «сумма» по смешанным типам — ложь в payload: колонка пропускается
        rows = [{"amount": 1}, {"amount": "2"}]
        compact = compact_rows(rows, truncated=False, approximate=False)
        assert "amount" not in compact.aggregates

    def test_empty_rows(self) -> None:
        compact = compact_rows([], truncated=False, approximate=False)
        assert compact.row_count == 0
        assert compact.sample == []
        assert compact.aggregates == {}

    def test_flags_are_explicit_fields(self) -> None:
        # truncated — зеркало state.truncated (исполнитель LIMIT), approximate —
        # T2-флаг (False до выпускной) — ruling R2 ARCH-GATE S15
        compact = compact_rows([], truncated=True, approximate=True)
        assert compact.truncated is True
        assert compact.approximate is True

    def test_sample_cannot_exceed_limit(self) -> None:
        # fail-closed VO: сэмпл больше бюджета промпта интерпретатора невозможен
        with pytest.raises(ValueError, match="бюджета промпта"):
            ResultCompact(row_count=3, sample=_rows(MAX_COMPACT_ROWS + 1))

    def test_aggregate_deterministic_column_order(self) -> None:
        rows = [{"b": 1, "a": 2}]
        compact = compact_rows(rows, truncated=False, approximate=False)
        assert list(compact.aggregates) == sorted(["a", "b"])


class TestCompactResultNode:
    """Узел compact_result: state.rows → state.compact (владение — карта AgentState)."""

    def test_writes_compact_from_rows(self) -> None:
        node = make_compact_result_node()
        updated = node({"rows": _rows(100), "row_count": 100, "truncated": False})
        compact = updated["compact"]
        assert compact["row_count"] == 100
        assert len(compact["sample"]) == MAX_COMPACT_ROWS

    def test_mirrors_truncation_flags(self) -> None:
        node = make_compact_result_node()
        updated = node({"rows": [], "truncated": True, "approximate": True})
        assert updated["compact"]["truncated"] is True
        assert updated["compact"]["approximate"] is True

    def test_none_rows_is_empty_compact(self) -> None:
        # T2-approximate путь доходит до compact без исполнения (S12, D-19):
        # честный пустой компакт с approximate-флагом, не дефект
        node = make_compact_result_node()
        updated = node({"rows": None, "approximate": True})
        assert updated["compact"]["row_count"] == 0

    def test_no_llm_calls_charged(self) -> None:
        # компакт — код-узел (hw-01 §3.4): бюджет прогона не платит
        node = make_compact_result_node()
        updated = node({"rows": _rows(1)})
        assert "budget_used" not in updated

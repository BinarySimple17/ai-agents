"""Юнит-тесты domain VO (S13): RunContext — fail-closed защёлка RLS-tripwire'а;
AgentState — контракт стыка 1 (S00, S04): состав полей и терминальных исходов.

Пустой user_id прошёл бы `IS NOT NULL` в RLS-политике миграции 0004 и молча
открыл бы employees/bonuses (ловушка 5) — валидатор порта обязан отсекать.
"""

import pytest
from pydantic import ValidationError

from text_to_sql_agent.domain.entities import (
    TERMINAL_OUTCOMES,
    AgentState,
    RunContext,
)

# Контракт стыка 1 S00 дословно (плюс обвязочные поля orchestration — R3/R4)
CONTRACT_1_FIELDS = {
    "question",
    "user_id",  # verified identity (R4 S05) — точка входа, resolve_access читает
    "rewritten_question",
    "assumptions",
    "permissions",
    "graph_version",
    "join_plan",
    "skeleton",
    "skeleton_hash",
    "sql",
    "validation",
    "rows",
    "row_count",
    "compact",
    "error",
    "retry_count",
    "budget_used",
    "route_flags",
    "truncated",
    "approximate",
    "incomplete",
    # Обвязка orchestration (порядок обхода / исход / дедлайн) — синк S00 тем
    # же коммитом (ruling R3/R4 ARCH-GATE S04)
    "visited_nodes",
    "outcome",
    "deadline_at",
}


def test_run_context_ok() -> None:
    ctx = RunContext(trace_id="t-1", user_id="u-1")
    assert ctx.token_ref is None and ctx.scope is None


@pytest.mark.parametrize("field", ["trace_id", "user_id"])
@pytest.mark.parametrize("bad", ["", "   "])
def test_empty_identity_fields_rejected(bad: str, field: str) -> None:
    values = {"trace_id": "t-1", "user_id": "u-1"}
    values[field] = bad
    with pytest.raises(ValidationError):
        RunContext(**values)


def test_whitespace_identity_trimmed() -> None:
    ctx = RunContext(trace_id=" t-1 ", user_id=" u-1 ")
    assert ctx.trace_id == "t-1" and ctx.user_id == "u-1"


def test_agent_state_contract_fields_complete() -> None:
    # Расхождение состава полей с контрактом стыка 1 = рассинхрон канона
    assert set(AgentState.__annotations__) == CONTRACT_1_FIELDS


def test_agent_state_is_stdlib_typed_dict() -> None:
    # domain без внешних зависимостей: LangGraph потребляет TypedDict нативно,
    # импорт langgraph в domain запрещён (ARCH-GATE S04 Q1)
    import typing

    assert typing.is_typeddict(AgentState)


def test_terminal_outcomes_match_fsm_canon() -> None:
    # 9 терминальных исходов state-agent v1.5 — различимый payload (ADR-002 §4)
    assert TERMINAL_OUTCOMES == (
        "done",
        "degrade",
        "short_circuit",
        "overflow",
        "fail",
        "rejected",
        "degraded_out",
        "budget_stop",
        "clarification",
    )


def test_initial_state_defaults_are_neutral() -> None:
    # Дефолты не выдают заглушку за содержательный результат (S00-8)
    from text_to_sql_agent.application.services.stub_nodes import initial_state

    state = initial_state("вопрос?")
    assert state["question"] == "вопрос?"
    assert state["permissions"] == {}  # пустой scope, не «все права»
    assert state["graph_version"] is None  # пин появится в resolve_access
    assert state["retry_count"] == 0
    assert state["route_flags"] == {}
    assert state["truncated"] is False and state["incomplete"] is False
    assert state["visited_nodes"] == []
    assert state["outcome"] is None

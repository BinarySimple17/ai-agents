"""Юнит-тесты domain VO (S13): RunContext — fail-closed защёлка RLS-tripwire'а.

Пустой user_id прошёл бы `IS NOT NULL` в RLS-политике миграции 0004 и молча
открыл бы employees/bonuses (ловушка 5) — валидатор порта обязан отсекать.
"""

import pytest
from pydantic import ValidationError

from text_to_sql_agent.domain.entities import RunContext


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

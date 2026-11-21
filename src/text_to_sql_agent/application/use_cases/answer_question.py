"""Оркестратор FSM-прогона — точка входа use case (S00 раскладка: use_cases).

Обязательства прогона (S04 Task 3/4): reset бюджетного аккумулятора S03 на
старте (шов ruling Q7 ARCH-GATE S09 — ``LLMProvider.reset_request_budget``, второй
аккумулятор не заводится), прогон через engine-neutral GraphRunner (ruling R2:
LangGraph знает только orchestration), эксплуатационные сбои шлюза — валидные
терминальные исходы, не traceback (S00 правило 8; ADR-007 M6 — деградацию решает
FSM, не провайдер).

Движковые предохранители (per-node ≤ 3, recursion_limit, глобальные капы)
реализованы в orchestration/routing.py + orchestration/runner.py — use case
получает готовый исход.
"""

from dataclasses import dataclass
from typing import Any

from text_to_sql_agent.application.ports.graph_runner import GraphRunner
from text_to_sql_agent.application.ports.llm_provider import (
    BudgetExceededError,
    LLMProvider,
    LLMProviderError,
)
from text_to_sql_agent.application.services.stub_nodes import initial_state
from text_to_sql_agent.domain.entities import TERMINAL_OUTCOMES, AgentState, TerminalOutcome

DEGRADE_MESSAGE_TEMPLATE = "шлюз LLM недоступен: {error}"
BUDGET_STOP_MESSAGE_TEMPLATE = "исчерпан бюджет прогона: {error}"


@dataclass(frozen=True, slots=True)
class FSMResult:
    """Результат прогона FSM: различимый исход + финальный state (payload наружу)."""

    outcome: TerminalOutcome
    state: dict[str, Any]

    @property
    def visited_nodes(self) -> list[str]:
        return list(self.state.get("visited_nodes") or [])

    @property
    def answer(self) -> dict[str, Any] | None:
        return self.state.get("compact")


class AnswerQuestion:
    """Один бизнес-вопрос → терминальный исход FSM (single «вопрос → ответ», S00)."""

    def __init__(self, runner: GraphRunner, llm: LLMProvider) -> None:
        self._runner = runner
        self._llm = llm

    def run(self, question: str, *, user_id: str = "") -> FSMResult:
        """Прогнать вопрос через FSM: reset бюджета → invoke → исход.

        ``user_id`` — verified identity (ruling R4 ARCH-GATE S05): точка входа
        проверяет токен через `AuthProvider.verify` ДО прогона; пусто — узел
        resolve_access fail-closed → rejected (прогон не начинается без identity).
        """
        # Обязательство S03: прогон начинает запрос с нуля — аккумулятор прошлого
        # прогона ложно уменьшил бы остаток для эскалаций M1 (ruling Q7 S09)
        self._llm.reset_request_budget()
        state: AgentState = initial_state(question, user_id)
        try:
            final = self._runner.run(state)
        except BudgetExceededError as exc:
            # Кап бюджета сработал до вызова шлюза (S03-адаптер) — budget_stop
            return _with_error("budget_stop", state, BUDGET_STOP_MESSAGE_TEMPLATE, exc)
        except LLMProviderError as exc:
            # Недоступность шлюза (ретраи/breaker/таймаут) — деградация-исход
            return _with_error("degrade", state, DEGRADE_MESSAGE_TEMPLATE, exc)
        outcome = final.get("outcome")
        if outcome not in TERMINAL_OUTCOMES:
            # Движок обязан вернуть терминальный payload (GraphRunner.run);
            # отсутствие — дефект сборки графа: честный fail, не silent pass
            final["error"] = {
                "class": "missing_outcome",
                "message": f"FSM завершилась без терминального исхода (outcome={outcome!r})",
            }
            return FSMResult(outcome="fail", state=final)
        return FSMResult(outcome=outcome, state=final)


def _with_error(
    outcome: TerminalOutcome, state: dict[str, Any], template: str, exc: Exception
) -> FSMResult:
    return FSMResult(
        outcome=outcome,
        state={
            **state,
            "error": {"class": type(exc).__name__, "message": template.format(error=exc)},
        },
    )

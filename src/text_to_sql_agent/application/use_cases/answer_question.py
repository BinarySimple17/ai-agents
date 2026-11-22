"""Оркестратор FSM-прогона — точка входа use case (S00 раскладка: use_cases).

Обязательства прогона (S04 Task 3/4): reset бюджетного аккумулятора S03 на
старте (шов ruling Q7 ARCH-GATE S09 — ``LLMProvider.reset_request_budget``, второй
аккумулятор не заводится), прогон через engine-neutral GraphRunner (ruling R2:
LangGraph знает только orchestration), эксплуатационные сбои шлюза — валидные
терминальные исходы, не traceback (S00 правило 8; ADR-007 M6 — деградацию решает
FSM, не провайдер).

Приостановка (S08/feat-008, rulings R3/R6 ARCH-GATE): уточняющий вопрос —
``interrupt()`` + checkpointer (S00 стык 1); ``run`` возвращает FSMResult с
``outcome=None`` (пауза, вопрос в state.clarify_question); ``resume`` с тем же
``thread_id`` продолжает ТОТ ЖЕ прогон — reset бюджета НЕ делается (капы
персистятся в checkpointer'е), дедлайн обработки перевзводит раннер.

Движковые предохранители (per-node ≤ 3, recursion_limit, глобальные капы)
реализованы в orchestration/routing.py + orchestration/runner.py — use case
получает готовый исход.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

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
    """Результат прогона FSM: различимый исход + финальный state (payload наружу).

    ``outcome=None`` — прогон приостановлен interrupt()-ом (ruling R3 ARCH-GATE
    S08): вопрос в state.clarify_question, продолжение — ``resume`` тем же
    ``thread_id`` (поле непусто всегда — use case генерирует его сам, ревью
    S08 Important-1: пауза без возвращённого id — тупик, resume нечем вызвать).
    """

    outcome: TerminalOutcome | None
    state: dict[str, Any]
    thread_id: str = ""

    @property
    def paused(self) -> bool:
        """Прогон приостановлен: ждёт ответ пользователя (interrupt-механика S08)."""
        return self.outcome is None

    @property
    def clarify_question(self) -> str | None:
        return self.state.get("clarify_question")

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

    def run(self, question: str, *, user_id: str = "", thread_id: str | None = None) -> FSMResult:
        """Прогнать вопрос через FSM: reset бюджета → invoke → исход.

        ``user_id`` — verified identity (ruling R4 ARCH-GATE S05): точка входа
        проверяет токен через `AuthProvider.verify` ДО прогона; пусто — узел
        resolve_access fail-closed → rejected (прогон не начинается без identity).
        ``thread_id`` — сессия checkpointer'а (interrupt/resume); None —
        генерируется здесь и ВОЗВРАЩАЕТСЯ в FSMResult.thread_id (пауза с
        ненаружным id непродолжаема — ревью S08 Important-1).
        """
        # Обязательство S03: прогон начинает запрос с нуля — аккумулятор прошлого
        # прогона ложно уменьшил бы остаток для эскалаций M1 (ruling Q7 S09)
        self._llm.reset_request_budget()
        state: AgentState = initial_state(question, user_id)
        tid = thread_id or uuid4().hex
        return self._invoke(
            lambda: self._runner.run(state, thread_id=tid), fallback_state=state, thread_id=tid
        )

    def resume(self, answer: str, *, thread_id: str, question: str | None = None) -> FSMResult:
        """Продолжить приостановленный прогон ответом (тот же thread — тот же прогон).

        БЕЗ reset_request_budget: капы бюджета персистятся в checkpointer'е
        (ruling R6 ARCH-GATE S08 — «ответ пользователя резюмирует тот же прогон»);
        ``question`` — для clarification-payload при истёкшем TTL сессии (R3).
        ``thread_id`` возвращается в FSMResult (инвариант поля — ревью S08
        re-review Minor-1: повторная пауза на resume — ребро plan_joins →
        clarify, S10 — с пустым id была бы непродолжаема).
        """
        fallback = initial_state(question or "", "")
        return self._invoke(
            lambda: self._runner.resume(answer, thread_id=thread_id, question=question),
            fallback_state=fallback,
            thread_id=thread_id,
        )

    def _invoke(
        self,
        call: Callable[[], dict[str, Any]],
        *,
        fallback_state: dict[str, Any],
        thread_id: str = "",
    ) -> FSMResult:
        try:
            final = call()
        except BudgetExceededError as exc:
            # Кап бюджета сработал до вызова шлюза (S03-адаптер) — budget_stop;
            # incomplete — как на графе (TERMINAL_PAYLOADS, ruling R3 S15):
            # exception-fallback не должен обходить флаг честности
            result = _with_error(
                "budget_stop", fallback_state, BUDGET_STOP_MESSAGE_TEMPLATE, exc, thread_id
            )
            result.state["incomplete"] = True
            return result
        except LLMProviderError as exc:
            # Недоступность шлюза (ретраи/breaker/таймаут) — деградация-исход
            return _with_error("degrade", fallback_state, DEGRADE_MESSAGE_TEMPLATE, exc, thread_id)
        if final.get("interrupted"):
            # Приостановка — не терминал: вопрос ждёт ответа (ruling R3 S08)
            return FSMResult(outcome=None, state=final, thread_id=thread_id)
        outcome = final.get("outcome")
        if outcome not in TERMINAL_OUTCOMES:
            # Движок обязан вернуть терминальный payload (GraphRunner.run);
            # отсутствие — дефект сборки графа: честный fail, не silent pass
            final["error"] = {
                "class": "missing_outcome",
                "message": f"FSM завершилась без терминального исхода (outcome={outcome!r})",
            }
            return FSMResult(outcome="fail", state=final, thread_id=thread_id)
        return FSMResult(outcome=outcome, state=final, thread_id=thread_id)


def _with_error(
    outcome: TerminalOutcome,
    state: dict[str, Any],
    template: str,
    exc: Exception,
    thread_id: str = "",
) -> FSMResult:
    return FSMResult(
        outcome=outcome,
        thread_id=thread_id,
        state={
            **state,
            "error": {"class": type(exc).__name__, "message": template.format(error=exc)},
        },
    )

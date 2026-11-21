"""Адаптер GraphRunner для LangGraph (ruling R2 ARCH-GATE S04).

Граница движка: GraphRecursionError перехватывается ЗДЕСЬ (orchestration знает
LangGraph — ADR-002 §5), наружу — валидный fail-исход в state (ADR-002 §4:
«исчерпание любого уровня — отказ с уточняющим вопросом, не исключение наверх»).
Дедлайн прогона раннер вшивает в state (``deadline_at``, monotonic) — гвард
капов на рёбрах (routing.llm_guard) проверяет его до вызова узлов с LLM.
"""

import time
from typing import Any

from langgraph.errors import GraphRecursionError

from text_to_sql_agent.orchestration.graph import build_fsm_app
from text_to_sql_agent.orchestration.routing import FSMGuardCaps

# Класс ошибки в fail-payload при исчерпании recursion_limit (различимость причин)
RECURSION_LIMIT_ERROR_CLASS = "recursion_limit"
RECURSION_LIMIT_MESSAGE = (
    "прогон превысил лимит шагов графа (recursion_limit) — отказ с уточняющим вопросом"
)


class LangGraphRunner:
    """Реализация порта GraphRunner: invoke с recursion_limit + дедлайном прогона."""

    def __init__(
        self,
        app: Any,
        *,
        deadline_s: float,
        recursion_limit: int,
    ) -> None:
        self._app = app
        self._deadline_s = deadline_s
        self._recursion_limit = recursion_limit

    def run(self, state: dict[str, Any]) -> dict[str, Any]:
        """Один прогон графа; GraphRecursionError → fail-исход, не исключение."""
        started = dict(state)
        # Дедлайн — monotonic-метка в state: гвард капов (routing.llm_guard)
        # сверяет её до вызова узлов с LLM (кап 90 s, hw-01 §6). Уже заданное
        # значение (инъекция теста) не перезаписывается — fuse проверяется честно
        if started.get("deadline_at") is None:
            started["deadline_at"] = time.monotonic() + self._deadline_s
        try:
            final = self._app.invoke(started, config={"recursion_limit": self._recursion_limit})
        except GraphRecursionError:
            # Второй предохранитель (ADR-002 §4): валидный отказ с уточняющим
            # вопросом — различимый payload, не traceback
            return {
                **started,
                "outcome": "fail",
                "error": {
                    "class": RECURSION_LIMIT_ERROR_CLASS,
                    "message": RECURSION_LIMIT_MESSAGE,
                },
            }
        return dict(final)


def build_fsm_runner(
    *,
    tracer: Any | None = None,
    caps: FSMGuardCaps | None = None,
    deadline_s: float,
    recursion_limit: int,
) -> LangGraphRunner:
    """Сборка раннера из графа (вызывает composition — единственное место сборки)."""
    app = build_fsm_app(tracer=tracer, caps=caps)
    return LangGraphRunner(app, deadline_s=deadline_s, recursion_limit=recursion_limit)

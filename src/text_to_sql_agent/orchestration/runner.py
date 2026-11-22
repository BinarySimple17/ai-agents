"""Адаптер GraphRunner для LangGraph (ruling R2 ARCH-GATE S04).

Граница движка: GraphRecursionError и приостановка interrupt перехватываются
ЗДЕСЬ (orchestration знает LangGraph — ADR-002 §5), наружу — валидные исходы в
state (ADR-002 §4: «исчерпание любого уровня — отказ с уточняющим вопросом, не
исключение наверх»). Приостановка (уточняющий вопрос — interrupt + checkpointer,
S00 стык 1) переводится в маркер ``interrupted=True`` — ruling R3 ARCH-GATE
S08: «outcome заполнен либо прогон приостановлен»; resume тем же thread_id
продолжает ТОТ ЖЕ прогон — капы бюджета/вызовов персистятся в checkpointer'е,
дедлайн обработки перевзводится (ruling R6: 90 s — кап обработки, не ожидания
пользователя). TTL сессии истёк → ``resume`` синтезирует clarification-терминал
(валидный исход, S00 правило 8), не исключение.

Дедлайн прогона раннер вшивает в state (``deadline_at``, monotonic) — гвард
капов на рёбрах (routing.llm_guard) проверяет его до вызова узлов с LLM;
``trace_id`` прогона (uuid) ставится там же — сквозная корреляция RunContext и
трейса (ADR-010).
"""

import time
from collections.abc import Callable, Mapping
from typing import Any
from uuid import uuid4

from langgraph.errors import GraphInterrupt, GraphRecursionError
from langgraph.types import Command

from text_to_sql_agent.application.ports.tracer import EVENT_CLARIFY_INTERRUPT
from text_to_sql_agent.domain.entities import Trace
from text_to_sql_agent.orchestration.graph import build_fsm_app
from text_to_sql_agent.orchestration.routing import FSMGuardCaps

# Класс ошибки в fail-payload при исчерпании recursion_limit (различимость причин)
RECURSION_LIMIT_ERROR_CLASS = "recursion_limit"
RECURSION_LIMIT_MESSAGE = (
    "прогон превысил лимит шагов графа (recursion_limit) — отказ с уточняющим вопросом"
)
# interrupt-маркер в результате invoke (LangGraph 0.6+) — движковая деталь,
# наружу переводится в state.interrupted (ruling R3 ARCH-GATE S08)
_INTERRUPT_KEY = "__interrupt__"
# Misuse-исходы (не traceback): interrupt без checkpointer (Ловушка 1 S08) и
# resume по thread без активной приостановки — дефект сборки/протокола вызова
INTERRUPT_NO_CHECKPOINT_CLASS = "interrupt_without_checkpointer"
INTERRUPT_NO_CHECKPOINT_MESSAGE = (
    "узел запросил уточнение, но граф собран без checkpointer — "
    "interrupt-механика (S00 стык 1) требует сессию (Ловушка 1 S08)"
)
RESUME_NO_INTERRUPT_CLASS = "resume_without_interrupt"
RESUME_NO_INTERRUPT_MESSAGE = (
    "resume по thread_id без активной приостановки — прогон не прерывался или уже завершён"
)
# TTL-исход (ruling R3 ARCH-GATE S08): сессия уточнения истекла — валидный
# clarification-терминал, не исключение; вопрос не доходит до ответа
CLARIFY_SESSION_EXPIRED_CLASS = "clarify_session_expired"
CLARIFY_SESSION_EXPIRED_MESSAGE = (
    "сессия уточнения истекла — начните новый вопрос: ответ на предыдущий не сохранён"
)


class LangGraphRunner:
    """Реализация порта GraphRunner: invoke с recursion_limit + дедлайн прогона.

    ``tracer`` — событие фактической паузы (``clarify.interrupt``) пишет раннер,
    не узел: resume-визит ре-исполняет узел с начала, узел не отличает паузу от
    продолжения — одно событие на фактическую приостановку (живой прогон S08).
    """

    def __init__(
        self,
        app: Any,
        *,
        deadline_s: float,
        recursion_limit: int,
        tracer: Any | None = None,
    ) -> None:
        self._app = app
        self._deadline_s = deadline_s
        self._recursion_limit = recursion_limit
        self._tracer = tracer

    def _config(self, thread_id: str) -> dict[str, Any]:
        return {
            "recursion_limit": self._recursion_limit,
            "configurable": {"thread_id": thread_id},
        }

    def _as_result(self, final: Mapping[str, Any], *, thread_id: str) -> dict[str, Any]:
        """Результат invoke → state: interrupt-маркер переводится в ``interrupted``.

        Движковый ключ ``__interrupt__`` наружу не идёт (ruling R3): вопрос уже
        в state (узел записал его до паузы) — CLI печатает и ждёт ответ. Факт
        паузы — событие clarify.interrupt (один на фактическую приостановку).
        """
        if _INTERRUPT_KEY not in final:
            return dict(final)
        payload = dict(final)
        payload.pop(_INTERRUPT_KEY, None)
        payload["interrupted"] = True
        if self._tracer is not None:
            self._tracer.record(
                Trace(
                    event=EVENT_CLARIFY_INTERRUPT,
                    payload={
                        "thread_id": thread_id,
                        "question": final.get("clarify_question"),
                    },
                )
            )
        return payload

    @staticmethod
    def _recursion_fail(state: Mapping[str, Any]) -> dict[str, Any]:
        return {
            **state,
            "outcome": "fail",
            "interrupted": False,
            "error": {
                "class": RECURSION_LIMIT_ERROR_CLASS,
                "message": RECURSION_LIMIT_MESSAGE,
            },
        }

    def run(self, state: Mapping[str, Any], *, thread_id: str | None = None) -> dict[str, Any]:
        """Один прогон графа: GraphRecursionError → fail, interrupt → маркер.

        ``thread_id`` — сессия checkpointer'а; None — генерируется (прогон без
        продолжения). Дедлайн и trace_id вшиваются в state, если не заданы
        (инъекция теста не перезаписывается — fuse проверяется честно).
        """
        started = dict(state)
        tid = thread_id or uuid4().hex
        if started.get("deadline_at") is None:
            started["deadline_at"] = time.monotonic() + self._deadline_s
        if started.get("trace_id") is None:
            started["trace_id"] = uuid4().hex
        try:
            final = self._app.invoke(started, config=self._config(tid))
        except GraphRecursionError:
            # Второй предохранитель (ADR-002 §4): валидный отказ с уточняющим
            # вопросом — различимый payload, не traceback
            return self._recursion_fail(started)
        except GraphInterrupt:
            # Ловушка 1 S08: interrupt, проброшенный движком как исключение —
            # misuse сборки; честный fail, не GraphInterrupt наружу (граница
            # движка — runner)
            return self._interrupt_no_checkpoint_fail(started)
        if _INTERRUPT_KEY in final and getattr(self._app, "checkpointer", None) is None:
            # Ловушка 1 S08 (семантика LangGraph 1.x): interrupt БЕЗ checkpointer
            # не бросает исключение — invoke возвращает паузу, которую продолжить
            # НЕВОЗМОЖНО (resume требует сессию). Маркер interrupted с
            # непродолжаемой паузой — обман протокола: fail, не пауза
            return self._interrupt_no_checkpoint_fail(started)
        return self._as_result(final, thread_id=tid)

    def _interrupt_no_checkpoint_fail(self, state: Mapping[str, Any]) -> dict[str, Any]:
        return {
            **state,
            "outcome": "fail",
            "interrupted": False,
            "error": {
                "class": INTERRUPT_NO_CHECKPOINT_CLASS,
                "message": INTERRUPT_NO_CHECKPOINT_MESSAGE,
            },
        }

    def resume(self, answer: str, *, thread_id: str, question: str | None = None) -> dict[str, Any]:
        """Продолжить приостановленный прогон ответом (тот же thread — тот же прогон).

        Капы персистятся в checkpointer'е (ruling R6), дедлайн обработки
        перевзводится (90 s — кап обработки, не ожидания пользователя). Сессия
        истекла (TTL) → синтез clarification-терминала из ``question`` (R3);
        resume без активной приостановки — fail (дефект протокола вызова:
        LangGraph молча начал бы НОВЫЙ прогон — Ловушка 2 S08, допущения
        потерялись бы).
        """
        config = self._config(thread_id)
        snapshot = self._app.get_state(config)
        base = dict(snapshot.values or {})
        if not base:
            # TTL истёк — чекпоинта нет (ruling R3): валидный clarification-исход,
            # state восстановить неоткуда — payload из ``question``
            return {
                "question": question or "",
                "clarify_question": None,
                "outcome": "clarification",
                "interrupted": False,
                "error": {
                    "class": CLARIFY_SESSION_EXPIRED_CLASS,
                    "message": CLARIFY_SESSION_EXPIRED_MESSAGE,
                },
            }
        if not any(task.interrupts for task in snapshot.tasks or ()):
            # Чекпоинт есть, приостановки нет — Command(resume) начал бы новый
            # прогон под видом resume (Ловушка 2 S08); честный fail
            return {
                **base,
                "outcome": "fail",
                "interrupted": False,
                "error": {
                    "class": RESUME_NO_INTERRUPT_CLASS,
                    "message": RESUME_NO_INTERRUPT_MESSAGE,
                },
            }
        try:
            final = self._app.invoke(
                Command(
                    resume=answer,
                    update={"deadline_at": time.monotonic() + self._deadline_s},
                ),
                config=config,
            )
        except GraphRecursionError:
            # Перевзвод дедлайна не восстанавливает счётчик супер-шагов: resume
            # продолжает шаги графа — лимит может кончиться на продолжении
            return self._recursion_fail(base)
        except GraphInterrupt:
            return {
                **base,
                "outcome": "fail",
                "interrupted": False,
                "error": {
                    "class": INTERRUPT_NO_CHECKPOINT_CLASS,
                    "message": INTERRUPT_NO_CHECKPOINT_MESSAGE,
                },
            }
        except ValueError as exc:
            # Command(resume) по thread без pending interrupt (LangGraph) —
            # дефект протокола вызова, наружу не проходит (граница движка)
            return {
                **base,
                "outcome": "fail",
                "interrupted": False,
                "error": {
                    "class": RESUME_NO_INTERRUPT_CLASS,
                    "message": RESUME_NO_INTERRUPT_MESSAGE + f" ({exc})",
                },
            }
        return self._as_result(final, thread_id=thread_id)


def build_fsm_runner(
    *,
    tracer: Any | None = None,
    caps: FSMGuardCaps | None = None,
    checkpointer: Any | None = None,
    deadline_s: float,
    recursion_limit: int,
    node_overrides: Mapping[str, Callable[[Mapping[str, Any]], dict[str, Any]]] | None = None,
) -> LangGraphRunner:
    """Сборка раннера из графа (вызывает composition — единственное место сборки).

    ``node_overrides`` — подмена заглушек содержательными узлами (S05+),
    прозрачно в build_fsm_app; ``checkpointer`` — сессия interrupt-механики
    (ruling R2 ARCH-GATE S08).
    """
    app = build_fsm_app(
        tracer=tracer, caps=caps, checkpointer=checkpointer, node_overrides=node_overrides
    )
    return LangGraphRunner(
        app, deadline_s=deadline_s, recursion_limit=recursion_limit, tracer=tracer
    )

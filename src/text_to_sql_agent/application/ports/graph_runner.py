"""Порт `GraphRunner` — engine-neutral запуск FSM (ruling R2 ARCH-GATE S04).

Application не знает движок графа (ADR-002 §5: LangGraph знают только слои
orchestration/composition): use case ``answer_question`` получает раннер через
DI из composition. Реализация S04 — ``orchestration/runner.py`` (LangGraphRunner);
смена движка — новый адаптер без правки application.

Эксплуатационные сбои движка (исчерпание recursion_limit) раннер переводит в
валидный исход state (fail-терминал, ADR-002 §4: «не исключение наверх») —
границу движка пересекают только данные, не исключения движка.

Контракт приостановки (ruling R3 ARCH-GATE S08): ``run`` возвращает state с
заполненным ``outcome`` ЛИБО с маркером приостановки ``interrupted=True``
(уточняющий вопрос — interrupt + checkpointer, S00 стык 1); resume тем же
``thread_id`` продолжает ТОТ ЖЕ прогон (капы бюджета/вызовов персистятся —
ruling R6). TTL сессии истёк → ``resume`` синтезирует clarification-терминал
(валидный исход, S00 правило 8 — не исключение).
"""

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

State = Mapping[str, Any]


@runtime_checkable
class GraphRunner(Protocol):
    def run(self, state: State, *, thread_id: str | None = None) -> dict[str, Any]:
        """Прогон FSM: стартовый state → финальный state с ``outcome``.

        Реализация обязана вернуть state с заполненным ``outcome``
        (TerminalOutcome) либо ``interrupted=True`` (прогон приостановлен —
        уточняющий вопрос ждёт ответа, S00 стык 1). ``thread_id`` — сессия
        checkpointer'а (тот же id обязателен для resume); None — движок
        генерирует сам (одноразовый прогон без продолжения). Исключения
        движка наружу не проходят.
        """
        ...

    def resume(self, answer: str, *, thread_id: str, question: str | None = None) -> dict[str, Any]:
        """Продолжить приостановленный прогон ответом пользователя.

        Тот же ``thread_id`` — тот же прогон (S00 стык 1: «ответ пользователя
        резюмирует тот же прогон»); капы бюджета/вызовов персистятся, дедлайн
        обработки перевзводится (ruling R6). Сессия истекла (TTL) → синтез
        clarification-терминала из ``question`` (валидный исход, R3), не
        исключение.
        """
        ...

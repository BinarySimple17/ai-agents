"""Порт `GraphRunner` — engine-neutral запуск FSM (ruling R2 ARCH-GATE S04).

Application не знает движок графа (ADR-002 §5: LangGraph знают только слои
orchestration/composition): use case ``answer_question`` получает раннер через
DI из composition. Реализация S04 — ``orchestration/runner.py`` (LangGraphRunner);
смена движка — новый адаптер без правки application.

Эксплуатационные сбои движка (исчерпание recursion_limit) раннер переводит в
валидный исход state (fail-терминал, ADR-002 §4: «не исключение наверх») —
границу движка пересекают только данные, не исключения движка.
"""

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

State = Mapping[str, Any]


@runtime_checkable
class GraphRunner(Protocol):
    def run(self, state: State) -> dict[str, Any]:
        """Один прогон FSM: стартовый state → финальный state с ``outcome``.

        Реализация обязана вернуть state с заполненным ``outcome``
        (TerminalOutcome) — различимым payload исхода; исключения движка
        наружу не проходят.
        """
        ...

"""Fan-out трейсер: одно событие — во все приёмники (BL-004, D-022).

Собирает JSONL-персистентность и консольный рендер в один `Tracer` для порта:
`TeeTracer(JsonlTracer(...), ConsoleTracer())`. Политика best-effort — по
каждому приёмнику независимо (как JsonlTracer): сбой одного не рвёт запрос
и не лишает события остальные.
"""

import warnings

from text_to_sql_agent.domain.entities import Trace


class TeeTracer:
    """Композит `Tracer`: рассылает каждое событие всем приёмникам."""

    def __init__(self, *tracers: object) -> None:  # приёмники — реализации Tracer
        self._tracers = tracers

    def record(self, trace: Trace) -> None:
        for tracer in self._tracers:
            try:
                tracer.record(trace)  # type: ignore[attr-defined] — утинная типизация порта
            except Exception as exc:  # best-effort: приёмник не рвёт запрос и остальных
                warnings.warn(
                    f"TeeTracer: приёмник {type(tracer).__name__} не записал событие "
                    f"({exc}) — запрос продолжается",
                    RuntimeWarning,
                    stacklevel=2,
                )

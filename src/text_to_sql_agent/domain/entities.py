"""Доменные сущности и value objects — без IO, без внешних зависимостей, без LLM
(AGENTS.md Architecture Boundaries; S00 правило 6).
"""

from typing import Any

from pydantic import BaseModel, Field, field_validator


class Trace(BaseModel):
    """Событие наблюдаемости узла/адаптера (FR-11, поля node_timings).

    VO без LLM-концепций: только имя события и метаданные; формат ответа LLM
    знает порт `LLMResponse`, не domain. Временную метку ставит трейсер при записи.
    """

    event: str
    payload: dict[str, Any] = Field(default_factory=dict)


class RunContext(BaseModel):
    """Контекст исполнения запроса (ADR-011 §4.2 v1.2; ruling ARCH-GATE S13 Q1).

    Кто и зачем исполняет: trace_id — сквозная корреляция (ADR-010), user_id —
    владелец scope: канал исполнения выставляет его в `SET LOCAL app.user_id`
    (рубеж 5 — ADR-005 §4), token_ref — ссылка на токен внешнего источника
    (SAP — ADR-011; PG-каналом не используется), scope — карта прав (S05; в
    S13 зарезервирована). user_id fail-closed непустое: пустая строка прошла бы
    `IS NOT NULL` RLS-tripwire'а и открыла бы чувствительные таблицы молча.
    """

    trace_id: str
    user_id: str
    token_ref: str | None = None
    scope: dict[str, Any] | None = None

    @field_validator("trace_id", "user_id")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("RunContext.trace_id/user_id обязаны быть непустыми (fail-closed)")
        return value

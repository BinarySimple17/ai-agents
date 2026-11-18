"""Доменные сущности и value objects — без IO, без внешних зависимостей, без LLM
(AGENTS.md Architecture Boundaries; S00 правило 6).
"""

from typing import Any

from pydantic import BaseModel, Field


class Trace(BaseModel):
    """Событие наблюдаемости узла/адаптера (FR-11, поля node_timings).

    VO без LLM-концепций: только имя события и метаданные; формат ответа LLM
    знает порт `LLMResponse`, не domain. Временную метку ставит трейсер при записи.
    """

    event: str
    payload: dict[str, Any] = Field(default_factory=dict)

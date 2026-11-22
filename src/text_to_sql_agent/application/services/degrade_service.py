"""Сервис degrade — формулировка отказа бизнес-понятиями (feat-015, S15).

Живой канон ADR-005:171 + prd FR-6: «код фиксирует, чего не хватает; LLM
формулирует» — код собрал типизированный вердикт-причину (domain.degradation,
ruling R8), модель mid формулирует отказ + доступный вариант (одна попытка,
без эскалации/ретрая — ruling R1: текст короткий и шаблонно-инвариантный).

Гарантии S9 — двухслойные (ruling R1):
- промпт: чужие имена в контекст не попадают (модель не может их «спросить»),
  инструкция «без имён недоступных таблиц, без подтверждения существования»;
- **санитайзер** (runtime-гарантия, «текст LLM не доверенный» — AGENTS
  Constraints): запрещённые маркеры (forbidden_topics + слова существования)
  в выходе ⇒ нарушение ⇒ шаблон-fallback (domain.fallback_text, 0 вызовов).
Шаблон-fallback также при недоступности шлюза/бюджета (NFR-8: breaker
завершается деградацией) и невалидном выходе тула.

Structured output через тул ``report_refusal`` (ADR-008 слой 2).
"""

import json

from pydantic import BaseModel, ConfigDict, Field

from text_to_sql_agent.application.ports.llm_provider import (
    BudgetExceededError,
    LLMProvider,
    LLMProviderError,
    LLMResponse,
    Role,
)
from text_to_sql_agent.domain.degradation import DegradationCase, fallback_text

# --- Тул structured output (ADR-008 слой 2) ---

_REFUSAL_TOOL_NAME = "report_refusal"
_REFUSAL_TOOL_SPEC: dict[str, object] = {
    "type": "function",
    "function": {
        "name": _REFUSAL_TOOL_NAME,
        "description": (
            "Вежливый отказ пользователю. Вызывается ровно один раз: отказ "
            "бизнес-понятиями + доступный вариант, если есть."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": (
                        "Отказ одним-двумя предложениями: без имён недоступных "
                        "таблиц, без утверждений о существовании данных, с "
                        "доступным вариантом, если он указан."
                    ),
                },
            },
            "required": ["text"],
        },
    },
}

_REFUSAL_SYSTEM_PROMPT = (
    "Ты — ассистент аналитика данных. Пользователю отказано в части данных. "
    "Вызови тул report_refusal ровно один раз: сформулируй отказ бизнес-"
    "понятиями по описанной ситуации. Строго запрещено: упоминать имена "
    "таблиц, которых нет среди доступных тем; утверждать, что недоступные "
    "данные существуют («есть строки», «содержит данные»). Доступные темы "
    "упоминать можно — предложи их как вариант."
)

# Роль формулировки отказа — mid (ruling R1 ARCH-GATE S15, фиксируется amend'ом
# ADR-007 v1.3; одна попытка — ретрай формулировки не помогает, шаблон страхует)
_DEGRADE_ROLE: Role = "mid"

# Слова подтверждения существования данных (S9): стем-подход покрывает
# морфологию («существует/существуют/существование», «присутствуют»);
# ложное срабатывание безопасно — шаблон-fallback вместо текста модели
_EXISTENCE_MARKERS: tuple[str, ...] = (
    "существ",
    "присутству",
    "есть строки",
    "есть записи",
    "есть данные",
    "содержит данные",
    "содержат данные",
    "содержит строки",
    "содержат строки",
)


class DegradeResult(BaseModel):
    """Результат узла degrade: готовый текст отказа (модель или шаблон)."""

    model_config = ConfigDict(frozen=True)

    text: str
    used_llm: bool = False  # False — шаблон (санитайзер/сбой/бюджет), 0 или 1 вызов
    llm_calls: int = Field(ge=0, default=0)
    error: str | None = None  # причина отклонения текста модели — оператору (S9)


def sanitize_refusal(text: str, case: DegradationCase) -> str | None:
    """Runtime-гарантия S9 (R1): нарушение → причина, чисто → None.

    Проверка без учёта регистра; «свои» темы (available_topics) разрешены —
    ADR-005:78 (граница знаний = граница прав).
    """
    lowered = text.lower()
    for topic in case.forbidden_topics:
        if topic.lower() in lowered:
            return f"текст упомянул недоступную тему: {topic}"
    for marker in _EXISTENCE_MARKERS:
        if marker in lowered:
            return f"текст подтвердил существование данных (маркер: {marker!r})"
    return None


def _parse_refusal(response: LLMResponse) -> str:
    """Тул-выход → текст; отклонения — ValueError (деталь для S9-трейса)."""
    calls = [tc for tc in response.tool_calls if tc.name == _REFUSAL_TOOL_NAME]
    if not calls:
        raise ValueError("выход шлюза без вызова тула report_refusal")
    if len(calls) > 1:
        raise ValueError(f"ожидался один вызов тула {_REFUSAL_TOOL_NAME}, получено {len(calls)}")
    try:
        arguments = json.loads(calls[0].arguments)
    except json.JSONDecodeError as exc:
        raise ValueError(f"аргументы тула не JSON: {exc}") from None
    if not isinstance(arguments, dict):
        raise ValueError("аргументы тула — не JSON-объект")
    raw_text = arguments.get("text")
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise ValueError("текст отказа пуст — формулировка бессодержательна")
    return raw_text.strip()


class DegradeService:
    """Формулировка отказа: mid-LLM одна попытка + санитайзер + шаблон-fallback."""

    def __init__(self, *, llm: LLMProvider) -> None:
        self._llm = llm

    def formulate(self, *, case: DegradationCase) -> DegradeResult:
        """Сформулировать отказ: текст модели (санитированный) или шаблон."""
        try:
            response = self._llm.complete(
                _DEGRADE_ROLE,
                self._messages(case),
                tools=[_REFUSAL_TOOL_SPEC],
            )
        except (LLMProviderError, BudgetExceededError) as exc:
            # NFR-8: недоступность шлюза / кап бюджета — шаблон (0 вызовов)
            return DegradeResult(text=fallback_text(case), error=f"шлюз: {exc}")
        try:
            text = _parse_refusal(response)
        except ValueError as exc:
            return DegradeResult(
                text=fallback_text(case), used_llm=True, llm_calls=1, error=str(exc)
            )
        violation = sanitize_refusal(text, case)
        if violation is not None:
            return DegradeResult(
                text=fallback_text(case), used_llm=True, llm_calls=1, error=violation
            )
        return DegradeResult(text=text, used_llm=True, llm_calls=1)

    def _messages(self, case: DegradationCase) -> list[dict[str, str]]:
        """Промпт отказа: ситуация + доступные темы (чужие имена — НЕ входят, R8)."""
        topics = ", ".join(case.available_topics) if case.available_topics else "(нет)"
        parts = [
            f"Ситуация: {case.situation}",
            f"Доступные пользователю темы: {topics}",
        ]
        return [
            {"role": "system", "content": _REFUSAL_SYSTEM_PROMPT},
            {"role": "user", "content": "\n".join(parts)},
        ]

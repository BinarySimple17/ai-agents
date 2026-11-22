"""Сервис interpret_result — финальный текст на языке бизнеса (feat-015, S15).

Последняя миля прогона (strong, glm-5.3-prime — ADR-007 §4.2): объясняет
результат по КОМПАКТНОМУ виду (behavior feat-015: сырые строки в промпт не
попадают — ~1150 токенов входа, ADR-007 §4.3). Ответ обязателен целиком:
текст (модель) + SQL + допущения + флаги + стоимость (код собирает payload —
модель не может придумать SQL/флаги/цену). usage_notes рёбер (JoinPlan,
S10) раскрываются человеческим языком — преемственность флагмана ДЗ-04.

Structured output через тул ``report_answer`` (ADR-008 слой 2, прецедент
report_diagnosis S14). Ретраи M1 ≤ 3 (D-16): вершина каскада — эскалации
нет, все попытки strong (попытка 2 — та же модель + текст ошибки парсинга).
Любой сбой (M1 исчерпан / шлюз / бюджет адаптера) — честный INCOMPLETE:
данные уже получены, узел собирает шаблонный отказ с row_count (Ловушка 5:
проверка бюджета ДО interpret — на рёбрах llm_guard + исключение адаптера).

``finish_reason="length"`` с валидным тул-выходом — тихая усечённость шлюза
(S19-хвост, поле LLMResponse.finish_reason): ANSWER с incomplete=True —
«неполное не выдаётся за полное» (state-agent v1.5).
"""

import json
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from text_to_sql_agent.application.ports.llm_provider import (
    BudgetExceededError,
    LLMProvider,
    LLMProviderError,
    LLMResponse,
    Role,
)
from text_to_sql_agent.application.services.m1_policy import MAX_ATTEMPTS

# --- Тул structured output (ADR-008 слой 2) ---

_ANSWER_TOOL_NAME = "report_answer"
_ANSWER_TOOL_SPEC: dict[str, object] = {
    "type": "function",
    "function": {
        "name": _ANSWER_TOOL_NAME,
        "description": (
            "Финальный ответ пользователю. Вызывается ровно один раз: связный "
            "текст на языке бизнеса — прямой ответ на вопрос с числами из "
            "агрегатов компакта."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": (
                        "Ответ одним-двумя абзацами: числа из агрегатов, "
                        "оговорки про усечение/допущения, если данные помечены."
                    ),
                },
            },
            "required": ["text"],
        },
    },
}

_ANSWER_SYSTEM_PROMPT = (
    "Ты — аналитик данных, объясняешь результат запроса пользователю на языке "
    "бизнеса. Тебе дают вопрос, исполненный SQL, компактный вид результата "
    "(row_count, сэмпл строк, агрегаты), допущения и замечания связей, флаги "
    "усечения и стоимость прогона. Вызови тул report_answer ровно один раз: "
    "прямой ответ на вопрос с числами из агрегатов; если данные помечены "
    "truncated/approximate/incomplete — скажи об этом честно. Не выдумывай "
    "числа за пределами компакта и не упоминай служебных деталей "
    "инфраструктуры."
)

# Роль финального объяснения — вершина каскада (ADR-007 §4.2: interpret — strong)
_INTERPRET_ROLE: Role = "strong"


class InterpretOutcome(StrEnum):
    """Исход финального объяснения: ANSWER — текст готов, INCOMPLETE — честный отказ."""

    ANSWER = "answer"
    INCOMPLETE = "incomplete"


class InterpretVerdict(BaseModel):
    """Pydantic-выход интерпретатора: текст ответа (непустой — validator)."""

    model_config = ConfigDict(frozen=True)

    text: str

    @field_validator("text")
    @classmethod
    def _text_non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("текст ответа пуст — объяснение бессодержательно")
        return value


class InterpretResult(BaseModel):
    """Результат узла interpret_result: ответ или честный incomplete.

    ``outcome=INCOMPLETE`` — текст не собран (M1/шлюз/бюджет): узел собирает
    шаблонный отказ с row_count и ставит state.incomplete (деталь сбоя — S9-трейс,
    пользователю — фиксированный текст). ``incomplete`` при ANSWER —
    finish_reason=length (тихая усечённость, S19-хвост).
    """

    model_config = ConfigDict(frozen=True)

    outcome: InterpretOutcome
    text: str | None = None
    incomplete: bool = False
    error: str | None = None  # причина сбоя — оператору в трейс (S9)
    llm_calls: int = Field(ge=0, default=0)


def _parse_answer(response: LLMResponse) -> str:
    """Тул-выход → текст; отклонения — ValueError (текст для ретрая M1).

    Reasoning-only текст без тул-вызова (D-021) — ValueError, не «понимание»
    текста (Ловушка 3 S08: структурный парсер обязан падать).
    """
    calls = [tc for tc in response.tool_calls if tc.name == _ANSWER_TOOL_NAME]
    if not calls:
        raise ValueError(
            "выход шлюза без вызова тула report_answer"
            + (" (текст — вероятно reasoning, D-021)" if response.text else "")
        )
    if len(calls) > 1:
        raise ValueError(f"ожидался один вызов тула {_ANSWER_TOOL_NAME}, получено {len(calls)}")
    try:
        arguments = json.loads(calls[0].arguments)
    except json.JSONDecodeError as exc:
        raise ValueError(f"аргументы тула не JSON: {exc}") from None
    if not isinstance(arguments, dict):
        raise ValueError("аргументы тула — не JSON-объект")
    # Валидация тем же правилом, что в VO (пустой текст → ретрай M1)
    raw_text = arguments.get("text")
    verdict = InterpretVerdict(text=raw_text if isinstance(raw_text, str) else "")
    return verdict.text


class InterpretService:
    """Финальный текст по компакту: strong + тул report_answer, M1 без эскалации."""

    def __init__(self, *, llm: LLMProvider) -> None:
        # Шов бюджета не нужен: эскалаций нет (вершина каскада), кап ловит
        # адаптер (BudgetExceededError → INCOMPLETE) и рёбра llm_guard
        self._llm = llm

    def interpret(
        self,
        *,
        question: str,
        sql: str | None,
        compact: dict,
        assumptions: list[str] | tuple[str, ...] = (),
        usage_notes: list[str] | tuple[str, ...] = (),
        cost_rub: float | None,
        truncated: bool,
        approximate: bool,
        incomplete: bool,
    ) -> InterpretResult:
        """Объяснить компакт: ANSWER (текст) или INCOMPLETE (честный отказ).

        ``assumptions`` — допущения clarify (тексты Assumption, S08);
        ``usage_notes`` — замечания рёбер из state.join_plan (S10) — оба списка
        входят в промпт и в финальный payload (код склеивает — модель не
        редактирует список допущений, только объясняет).
        """
        calls = 0
        error_text: str | None = None
        for _attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self._llm.complete(
                    _INTERPRET_ROLE,
                    self._messages(
                        question=question,
                        sql=sql,
                        compact=compact,
                        assumptions=assumptions,
                        usage_notes=usage_notes,
                        cost_rub=cost_rub,
                        truncated=truncated,
                        approximate=approximate,
                        incomplete=incomplete,
                        error_text=error_text,
                    ),
                    tools=[_ANSWER_TOOL_SPEC],
                )
            except BudgetExceededError as exc:
                return InterpretResult(
                    outcome=InterpretOutcome.INCOMPLETE,
                    error=f"бюджет прогона исчерпан до вызова: {exc}",
                    llm_calls=calls,
                )
            except LLMProviderError as exc:
                return InterpretResult(
                    outcome=InterpretOutcome.INCOMPLETE,
                    error=f"шлюз недоступен: {exc}",
                    llm_calls=calls,
                )
            calls += 1
            try:
                text = _parse_answer(response)
            except ValueError as exc:
                error_text = str(exc)
                continue
            # S19-хвост: «length» с текстом — тихая усечённость шлюза
            length_cut = response.finish_reason == "length"
            return InterpretResult(
                outcome=InterpretOutcome.ANSWER,
                text=text,
                incomplete=length_cut,
                llm_calls=calls,
            )
        return InterpretResult(
            outcome=InterpretOutcome.INCOMPLETE,
            error=f"объяснение не удалось (M1): последняя ошибка: {error_text}",
            llm_calls=calls,
        )

    def _messages(
        self,
        *,
        question: str,
        sql: str | None,
        compact: dict,
        assumptions: list[str] | tuple[str, ...],
        usage_notes: list[str] | tuple[str, ...],
        cost_rub: float | None,
        truncated: bool,
        approximate: bool,
        incomplete: bool,
        error_text: str | None,
    ) -> list[dict[str, str]]:
        """Промпт объяснения: компакт + SQL + допущения + флаги + стоимость."""
        known = ", ".join(assumptions) if assumptions else "(нет)"
        notes = "; ".join(usage_notes) if usage_notes else "(нет)"
        cost = f"{cost_rub:.4f} ₽" if cost_rub is not None else "(недоступна)"
        parts = [
            f"Вопрос пользователя: {question}",
            f"Исполненный SQL: {sql or '(не сохранён)'}",
            "Компактный вид результата (сырые строки недоступны): "
            + json.dumps(compact, ensure_ascii=False),
            f"Допущения уточнения: {known}",
            f"Замечания связей (usage_notes): {notes}",
            f"Флаги: усечение={truncated}, approximate={approximate}, incomplete={incomplete}",
            f"Стоимость прогона: {cost}",
        ]
        messages = [
            {"role": "system", "content": _ANSWER_SYSTEM_PROMPT},
            {"role": "user", "content": "\n".join(parts)},
        ]
        if error_text is not None:
            # Ретрай M1: ошибка предыдущего парсинга (невалидный выход тула)
            messages.append({"role": "assistant", "content": "(выход без тула)"})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"Ошибка предыдущей попытки: {error_text}. Вызови тул "
                        f"{_ANSWER_TOOL_NAME} повторно, строго по схеме."
                    ),
                }
            )
        return messages

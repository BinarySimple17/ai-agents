"""Сервис diagnose_error — разбор ошибки + подсказка регенерации (feat-014, S14).

Вход ретрай-цикла M1 (D-016, ADR-007 §4.1) на FSM-визитах: после SQL-ошибки
(S13), вердикта валидатора (S11 «галлюцинация») или probe-подозрения на
потерянный джойн разбирает сбой и даёт ПОДСКАЗКУ регенерации — она уходит в
retry-контекст generate_sql (вместе с последним SQL), не «патч» SQL: SQL пишет
только генератор (Ловушка 5 плана S14).

Два контура классификации:
- **Детерминированный (код, без LLM)** — классы, при которых регенерация
  БЕСПОЛЕЗНА: права (permission_denied/read_only/acl_denied — ADR-005: модель
  не «уговаривает» валидатор/RLS) и недоступное хранилище (connection — S00-8).
  Решение «повтор бессмыслен» — КОДА, не поле модели: security-исход не
  делегируется LLM (fail-closed).
- **LLM-разбор (qwen, cheap — ADR-007 §4.2)** — синтаксис/семантика/таймаут/
  пустой результат: модель возвращает класс + короткую подсказку одним абзацем
  (structured output через тул — ADR-008 слой 2, прецеденты fill_skeleton
  S10 / rank_paths S09 / record_clarification S08). ``retry_futile`` в
  pydantic-выходе считается КОДОМ по классу — карта едина для обоих контуров.

Ретраи M1 (невалидный выход тулa → повтор): попытка 1–2 — cheap, попытка 3 —
эскалация cheap→mid при остатке бюджета выше резерва (шов ruling Q7 ARCH-GATE
S09 — тот же, что plan_joins/clarify/generate_sql). Шлюз недоступен — честный
«разбор не удался» (узел не назначит ретрай: деградация — валидный исход).
"""

import json
from collections.abc import Callable
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from text_to_sql_agent.application.ports.llm_provider import (
    LLMProvider,
    LLMProviderError,
    LLMResponse,
    Role,
)
from text_to_sql_agent.application.services.m1_policy import (
    ESCALATION_ATTEMPT,
    ESCALATION_RESERVE_ROUBLES,
    MAX_ATTEMPTS,
    ROLE_UP,
)

# --- Классы разбора (домен диагностики; имя SQLFailureClass сохраняется) ---


class DiagnoseClass(StrEnum):
    """Класс сбоя для ретрай-политики: ретрабельные vs бесполезные повторы."""

    SYNTAX = "syntax"  # разбор/синтаксис — ретрай оправдан
    SEMANTICS = "semantics"  # не существует/неверная колонка — переписать выборку
    TIMEOUT = "timeout"  # тяжёлый запрос — ужать
    EMPTY = "empty"  # пустой результат/потерянный джойн — проверить фильтры/связь
    PERMISSIONS = "permissions"  # права/RLS — регенерация бессмысленна (ADR-005)
    CONNECTION = "connection"  # хранилище недоступно — регенерация бессмысленна


# Классы, при которых ретрай генерации не назначается — решение КОДА.
# permissions — ADR-005 («модель не уговорит валидатор»); connection — S00-8
# (инфраструктурный сбой чинится не переписыванием SQL).
_FUTILE_CLASSES: frozenset[DiagnoseClass] = frozenset(
    {DiagnoseClass.PERMISSIONS, DiagnoseClass.CONNECTION}
)

# Пред-классфикация сырья: класс ошибки state.error (SQLFailureClass S13,
# вердикт валидатора S11, «empty» — probe S14) → домен диагностики.
# «validate_hallucination» — семантика (модель назвала несуществующее);
# «empty» — пустой результат (LLM-разбор: фильтры или связь);
# «generation» — битый выход генератора (ruling R3 ARCH-GATE: до diagnose не
# доходит — ребро generate_sql → retry_generate_sql, но карта полная).
_DETERMINISTIC_MAP: dict[str, DiagnoseClass] = {
    "permission_denied": DiagnoseClass.PERMISSIONS,
    "read_only": DiagnoseClass.PERMISSIONS,
    "acl_denied": DiagnoseClass.PERMISSIONS,
    "connection": DiagnoseClass.CONNECTION,
    "validate_hallucination": DiagnoseClass.SEMANTICS,
    "syntax": DiagnoseClass.SYNTAX,
    "timeout": DiagnoseClass.TIMEOUT,
    "empty": DiagnoseClass.EMPTY,
    "generation": DiagnoseClass.SEMANTICS,
}

# Механические подсказки futile-классов (LLM не тратится): текст для трейса/
# диагностического поля, не патч SQL
_PERMISSIONS_HINT = (
    "часть данных недоступна по правам — регенерация SQL не изменит вердикт (ADR-005)"
)
_CONNECTION_HINT = "хранилище недоступно — регенерация не поможет, повторите запрос позже (S00-8)"

# --- Тул structured output (ADR-008 слой 2) ---

_DIAG_TOOL_NAME = "report_diagnosis"
_DIAG_TOOL_SPEC: dict[str, object] = {
    "type": "function",
    "function": {
        "name": _DIAG_TOOL_NAME,
        "description": (
            "Разбор ошибки выполнения SQL. Вызывается ровно один раз: класс ошибки "
            "и короткая подсказка регенерации (что исправить при переписывании "
            "запроса, одним абзацем)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "error_class": {
                    "type": "string",
                    "enum": [item.value for item in DiagnoseClass],
                    "description": "Класс ошибки: синтаксис/семантика/таймаут/пустой результат.",
                },
                "regeneration_hint": {
                    "type": "string",
                    "description": "Подсказка регенерации одним абзацем: что исправить в запросе.",
                },
            },
            "required": ["error_class", "regeneration_hint"],
        },
    },
}

_DIAG_SYSTEM_PROMPT = (
    "Ты — диагност SQL-ошибок аналитического агента данных. Тебе дают SQL, текст "
    "ошибки и (если есть) нарушения валидатора или данные пробы COUNT. Вызови тул "
    "report_diagnosis ровно один раз: классифицируй ошибку и дай короткую "
    "подсказку регенерации — что конкретно исправить при переписывании запроса "
    "(условие фильтра, имя колонки, агрегат). Не пиши SQL сам, не предлагай "
    "таблицы вне упомянутых в контексте."
)

# Роль разбора — матрица ADR-007 §4.2: разбор ошибки — cheap (qwen)
_DIAG_ROLE_START: Role = "cheap"


class DiagnoseVerdict(BaseModel):
    """Pydantic-выход разбора (Task 1): класс + подсказка + флаг «повтор бессмыслен»."""

    model_config = ConfigDict(frozen=True)

    error_class: DiagnoseClass
    regeneration_hint: str
    retry_futile: bool = False  # КОД по _FUTILE_CLASSES — не поле модели (ADR-005)

    @field_validator("regeneration_hint")
    @classmethod
    def _hint_non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("подсказка регенерации пуста — разбор бессодержателен")
        return value


class DiagnoseResult(BaseModel):
    """Результат узла diagnose_error: вердикт или честный «разбор не удался».

    ``verdict is None`` — шлюз недоступен/невалидные выходы M1 исчерпаны: узел
    НЕ назначает ретрай (деградация — валидный исход, S00-8), деталь — S9-трейс.
    ``llm_calls`` — учёт прогона для state.budget_used (гвард капов ≤ 8 —
    routing.llm_guard; единый источник расхода, как в generate_sql_service).
    """

    model_config = ConfigDict(frozen=True)

    verdict: DiagnoseVerdict | None = None
    error: str | None = None  # причина сбоя разбора — оператору в трейс (S9)
    llm_calls: int = Field(ge=0, default=0)


def _parse_diagnosis(response: LLMResponse) -> tuple[DiagnoseClass, str]:
    """Тул-выход → (класс, подсказка); отклонения — ValueError (текст для ретрая M1).

    Reasoning-only текст без тул-вызова (D-021) — ValueError, не «понимание»
    текста (Ловушка 3 S08: структурный парсер обязан падать).
    """
    calls = [tc for tc in response.tool_calls if tc.name == _DIAG_TOOL_NAME]
    if not calls:
        raise ValueError(
            "выход шлюза без вызова тула report_diagnosis"
            + (" (текст — вероятно reasoning, D-021)" if response.text else "")
        )
    if len(calls) > 1:
        raise ValueError(f"ожидался один вызов тула {_DIAG_TOOL_NAME}, получено {len(calls)}")
    try:
        arguments = json.loads(calls[0].arguments)
    except json.JSONDecodeError as exc:
        raise ValueError(f"аргументы тула не JSON: {exc}") from None
    if not isinstance(arguments, dict):
        raise ValueError("аргументы тула — не JSON-объект")
    raw_class = arguments.get("error_class")
    hint = arguments.get("regeneration_hint")
    try:
        error_class = DiagnoseClass(raw_class)
    except ValueError:
        raise ValueError(f"класс ошибки вне домена диагностики: {raw_class!r}") from None
    # Валидация подсказки тем же правилом, что в VO (пустая → ретрай M1)
    DiagnoseVerdict(
        error_class=error_class, regeneration_hint=hint if isinstance(hint, str) else ""
    )
    return error_class, (hint if isinstance(hint, str) else "")


class DiagnoseService:
    """Разбор ошибки + подсказка регенерации: futile — код, остальное — cheap-LLM."""

    def __init__(
        self,
        *,
        llm: LLMProvider,
        budget_remaining_roubles: Callable[[], float] | None = None,
    ) -> None:
        # budget_remaining_roubles — шов бюджета (ruling Q7 ARCH-GATE S09):
        # None = бюджетный контур не подключён — эскалация без проверки
        # (документированный fallback до S15 BudgetGuard, прецедент generate_sql)
        self._llm = llm
        self._budget_remaining_roubles = budget_remaining_roubles

    def diagnose(
        self,
        *,
        error_class: str,
        error_message: str,
        sql: str | None = None,
        validation_issues: tuple[str, ...] | list[str] = (),
        probe_context: str | None = None,
    ) -> DiagnoseResult:
        """Разобрать сбой: futile — без LLM, иначе cheap-LLM с ретраями M1.

        ``error_class`` — сырой класс из state.error (SQLFailureClass/вердикт
        валидатора/«empty» пробы); ``sql`` — последняя попытка (D-016: в контексте
        только она); ``validation_issues`` — внутренний контекст валидатора (S11);
        ``probe_context`` — факты COUNT-пробы из error["probe"] (execute_sql, S14 —
        S9: пользовательский message фиксированный, факты — внутренний канал).
        """
        mapped = _DETERMINISTIC_MAP.get(error_class)
        if mapped in _FUTILE_CLASSES:
            hint = _PERMISSIONS_HINT if mapped is DiagnoseClass.PERMISSIONS else _CONNECTION_HINT
            return DiagnoseResult(
                verdict=DiagnoseVerdict(
                    error_class=mapped,
                    regeneration_hint=hint,
                    retry_futile=True,
                )
            )

        if mapped is None:
            # Неизвестный класс — разбор всё равно LLM-ом (модель может уточнить),
            # ретрай оправдан по умолчанию: класс вернёт тул
            mapped = DiagnoseClass.SYNTAX

        role: Role = _DIAG_ROLE_START
        error_text: str | None = error_message
        calls = 0
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if attempt == ESCALATION_ATTEMPT and role != ROLE_UP[role]:
                if (
                    self._budget_remaining_roubles is not None
                    and self._budget_remaining_roubles() <= ESCALATION_RESERVE_ROUBLES
                ):
                    return DiagnoseResult(
                        error=(
                            f"эскалация разбора отклонена: остаток бюджета не покрывает "
                            f"резерв {ESCALATION_RESERVE_ROUBLES} ₽ (ADR-007 §4.3)"
                        ),
                        llm_calls=calls,
                    )
                role = ROLE_UP[role]
            try:
                response = self._llm.complete(
                    role,
                    self._messages(
                        error_class=mapped,
                        error_message=error_message,
                        sql=sql,
                        validation_issues=validation_issues,
                        probe_context=probe_context,
                        error_text=error_text,
                    ),
                    tools=[_DIAG_TOOL_SPEC],
                )
            except LLMProviderError as exc:
                # Политика провайдера исчерпана — ретрай M1 поверх бессмыслен
                return DiagnoseResult(error=f"шлюз недоступен: {exc}", llm_calls=calls)
            calls += 1
            try:
                llm_class, hint = _parse_diagnosis(response)
            except ValueError as exc:
                error_text = str(exc)
                continue
            return DiagnoseResult(
                verdict=DiagnoseVerdict(
                    error_class=llm_class,
                    regeneration_hint=hint,
                    retry_futile=llm_class in _FUTILE_CLASSES,
                ),
                llm_calls=calls,
            )
        return DiagnoseResult(
            error=f"разбор не удался (M1): последняя ошибка: {error_text}",
            llm_calls=calls,
        )

    def _messages(
        self,
        *,
        error_class: DiagnoseClass,
        error_message: str,
        sql: str | None,
        validation_issues: tuple[str, ...] | list[str],
        probe_context: str | None,
        error_text: str | None,
    ) -> list[dict[str, str]]:
        """Промпт разбора: SQL + ошибка + контекст источника (D-016: не цепочка попыток)."""
        issues = "; ".join(validation_issues) if validation_issues else "(нет)"
        parts = [
            f"Класс ошибки (предварительно): {error_class.value}",
            f"Текст ошибки: {error_message}",
            f"SQL последней попытки: {sql or '(не сохранён)'}",
            f"Нарушения валидатора: {issues}",
            f"Данные пробы COUNT: {probe_context or '(не было)'}",
        ]
        messages = [
            {"role": "system", "content": _DIAG_SYSTEM_PROMPT},
            {"role": "user", "content": "\n".join(parts)},
        ]
        if error_text is not None and error_text != error_message:
            # Ретрай M1: текст ошибки предыдущего парсинга (невалидный выход тулa)
            messages.append({"role": "assistant", "content": "(выход без тула)"})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"Ошибка предыдущей попытки: {error_text}. Вызови тул "
                        f"{_DIAG_TOOL_NAME} повторно, строго по схеме."
                    ),
                }
            )
        return messages

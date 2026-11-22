"""Сервис узла clarify — классификация однозначности вопроса (feat-008, S08).

Классификация (qwen/cheap, матрица ADR-007 §4): однозначен ли вопрос для
построения аналитики; вердикт + явные допущения (unambiguous) или РОВНО ОДИН
уточняющий вопрос (ambiguous/insufficient_data — behavior фичи). Механику
«вопрос → interrupt → resume» ведёт узел (clarify_node.py) — сервис
детерминирован и не знает о графе.

Структурированный выход — тул (ADR-008 слой 2, ruling R7 ARCH-GATE S08;
прецедент — тул rank_paths S09): JSON-текст-парсинг = отвергнутый вариант B.
pydantic ниже — валидация СУЩНОСТИ вердикта (допущения — закрытый набор схемы,
вопрос обязателен при неоднозначности), формат гарантирует механизм тула.
Reasoning-only выход шлюза (D-021, Ловушка 3 S08) тулом не является — парсер
обязан упасть и уйти в ретрай.

Retrieval-нога — через порт VectorStore (контекст БЗ по правам пользователя,
паттерн S09): сбой → честный FAIL (классификация без контекста — ложные
вердикты; S00 правило 8). Ретраи M1 (D-016, ADR-007 §4.1): попытка 1 — cheap;
попытка 2 — та же роль + текст ошибки (в контекст — только последний выход и
ошибка, не цепочка попыток); попытка 3 — роль вверх при остатке бюджета с
резервом (ruling Q7 ARCH-GATE S09: инъекция budget_remaining_roubles(),
подключена к резидентному аккумулятору адаптера).
"""

import json
from collections.abc import Callable
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from text_to_sql_agent.application.ports.llm_provider import (
    LLMProvider,
    LLMProviderError,
    LLMResponse,
    Role,
)
from text_to_sql_agent.application.ports.vector_store import (
    DocKind,
    KnowledgeDocument,
    VectorStore,
    VectorStoreError,
)
from text_to_sql_agent.application.services.m1_policy import (
    ESCALATION_ATTEMPT,
    ESCALATION_RESERVE_ROUBLES,
    MAX_ATTEMPTS,
    ROLE_UP,
)
from text_to_sql_agent.domain.entities import Assumption, RunContext

# --- Константы (AGENTS.md: магические числа — именованные, с «почему») ---

# top_k retrieval: баланс полноты/шума (как в plan_joins — решение S24 #1а)
_RETRIEVAL_TOP_K = 8
# Сниппет одного документа в контексте классификации: классификатору достаточно
# заголовков/определений — полный контекст (~2К токенов) нужен планеру (S09), не здесь
_SNIPPET_CHARS = 240
# Кап суммарного контекста БЗ в промпте (классификация дешёвого входа — экономия
# бюджета; вопрос пользователя — главный сигнал, контекст лишь уточняет сущности)
_CONTEXT_MAX_CHARS = 1200
# Ретраи M1 / резерв эскалации / роль попытки 3 — общие константы политики
# (application/services/m1_policy.py; унификация при третьем потребителе
# generate_sql — S10, как и предсказывал этот комментарий в S08)
# Роль попытки 1 — матрица ADR-007 §4: clarify = дешёвая модель (классификация
# + один вопрос; вызывается на каждый запрос — скорость и минимальная цена)
_CLASSIFIER_ROLE_START: Role = "cheap"
# Кап допущений вердикта: их сверяет кэш (S16 — ключ кэша) и раскрывает
# interpret (S15) — рост числа раздувает ключ и ответ
_MAX_ASSUMPTIONS = 5

# ADR-008 слой 2: structured output — через тул (tool-based enforcement)
_CLARIFY_TOOL_NAME = "record_clarification"
_CLARIFY_TOOL_SPEC: dict[str, object] = {
    "type": "function",
    "function": {
        "name": _CLARIFY_TOOL_NAME,
        "description": (
            "Фиксация вердикта об однозначности вопроса пользователя. Вызывается "
            "ровно один раз: вердикт + явные допущения + при необходимости один "
            "уточняющий вопрос."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ambiguity": {
                    "type": "string",
                    "enum": ["unambiguous", "ambiguous", "insufficient_data"],
                    "description": (
                        "unambiguous — вопрос однозначен; ambiguous — есть равнозначные "
                        "интерпретации; insufficient_data — данных для ответа не хватает"
                    ),
                },
                "assumptions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": _MAX_ASSUMPTIONS,
                    "description": (
                        "Явные допущения, которые фиксирует код (каждое — одно конкретное: "
                        "какой фильтр применён, какая атрибуция/период). Пусто — допущений нет."
                    ),
                },
                "clarifying_question": {
                    "type": "string",
                    "description": (
                        "Один уточняющий вопрос пользователю (обязателен при "
                        "ambiguous/insufficient_data). Без раскрытия внутренней схемы БД."
                    ),
                },
            },
            "required": ["ambiguity"],
        },
    },
}

_CLARIFY_SYSTEM_PROMPT = (
    "Ты — классификатор однозначности вопросов к аналитическому агенту данных. "
    "Оцени вопрос пользователя для построения SQL-аналитики: понятны ли сущности, "
    "метрика, фильтры и период — с учётом контекста базы знаний (DDL, глоссарий, "
    "связи). Вызови тул record_clarification ровно один раз. В assumptions перечисли "
    "явные допущения, принятые при трактовке вопроса (одно допущение — один элемент). "
    "Если вопрос неоднозначен или данных не хватает — задай РОВНО ОДИН короткий "
    "уточняющий вопрос пользователю в clarifying_question. Не выдумывай таблицы и "
    "колонки; не раскрывай внутреннее устройство базы."
)


class ClarifyOutcome(StrEnum):
    """Исход сервиса clarify: вердикт или честный отказ (FSM-маршрут — routing)."""

    UNAMBIGUOUS = "unambiguous"  # допущения зафиксированы — дальше план_joins
    CLARIFY = "clarify"  # нужен уточняющий вопрос — interrupt-механика узла
    FAIL = "fail"  # LLM/retrieval недоступны, ретраи исчерпаны — отказ узла


class ClarifyVerdict(BaseModel):
    """Structured output классификатора (ADR-008): валидация СУЩНОСТИ выхода.

    Допущения — закрытый набор схемы (короткие строки, ≤ _MAX_ASSUMPTIONS);
    при неоднозначности вопрос обязателен, при однозначности запрещён — модель
    не решает, валидирует код.
    """

    model_config = ConfigDict(frozen=True)

    ambiguity: Literal["unambiguous", "ambiguous", "insufficient_data"]
    assumptions: list[str] = Field(default_factory=list, max_length=_MAX_ASSUMPTIONS)
    clarifying_question: str | None = None

    @field_validator("assumptions")
    @classmethod
    def _assumptions_non_empty(cls, value: list[str]) -> list[str]:
        for text in value:
            if not text.strip():
                raise ValueError("допущение вердикта не может быть пустым")
        return value

    @model_validator(mode="after")
    def _question_by_ambiguity(self) -> "ClarifyVerdict":
        if self.ambiguity == "unambiguous":
            if self.clarifying_question:
                raise ValueError("однозначный вопрос не требует уточняющего")
            return self
        if not (self.clarifying_question or "").strip():
            raise ValueError(f"ambiguity={self.ambiguity!r} требует непустой clarifying_question")
        return self


class ClarifyResult(BaseModel):
    """Результат узла clarify: вердикт + допущения (VO с провенансом — ruling R5).

    ``llm_calls`` — учёт прогона для state.budget_used (гвард капов ≤ 8 —
    routing.llm_guard); ₽-аккумулятор — резидент адаптера (S03), в state —
    BudgetGuard (S15), как в каркасе S04.
    """

    model_config = ConfigDict(frozen=True)

    outcome: ClarifyOutcome
    assumptions: list[Assumption] = Field(default_factory=list)
    question: str | None = None  # уточняющий вопрос (outcome=clarify)
    error: str | None = None  # причина отказа (outcome=fail) — в трейс, не пользователю
    llm_calls: int = Field(ge=0, default=0)


def _parse_verdict(response: LLMResponse) -> ClarifyVerdict:
    """Тул-вызов → вердикт; любые отклонения — ValueError (ретрай M1).

    Reasoning-only текст без тул-вызова (D-021) — ValueError, не «понимание»
    текста (Ловушка 3 S08: структурный парсер обязан падать).
    """
    tool_calls = [tc for tc in response.tool_calls if tc.name == _CLARIFY_TOOL_NAME]
    if not tool_calls:
        raise ValueError(
            "выход шлюза без вызова тула record_clarification"
            + (" (текст — вероятно reasoning, D-021)" if response.text else "")
        )
    try:
        args = json.loads(tool_calls[0].arguments)
    except json.JSONDecodeError as exc:
        raise ValueError(f"аргументы тула не JSON: {exc}") from None
    if not isinstance(args, dict):
        raise ValueError("аргументы тула — не JSON-объект")
    try:
        return ClarifyVerdict.model_validate(args)
    except ValidationError as exc:
        raise ValueError(f"вердикт не прошёл схему: {exc}") from None


def _last_output_text(response: LLMResponse) -> str:
    """Последний выход шлюза для M1-контекста ретрая (D-016: не цепочка попыток)."""
    if response.text:
        return response.text
    if response.tool_calls:
        return "; ".join(tc.arguments for tc in response.tool_calls)
    return ""


def _context_block(documents: list[KnowledgeDocument]) -> str:
    """Сниппеты хитов БЗ для промпта (дистракторы NOISE в промпт не идут — S26)."""
    parts: list[str] = []
    total = 0
    for doc in documents:
        if doc.doc_kind == DocKind.NOISE:
            continue
        snippet = f"[{doc.doc_kind}] {doc.content[:_SNIPPET_CHARS]}"
        if total + len(snippet) > _CONTEXT_MAX_CHARS:
            break
        parts.append(snippet)
        total += len(snippet)
    return "\n---\n".join(parts)


class ClarifyService:
    """Узел clarify: retrieval-нога + классификация с ретраями M1."""

    def __init__(
        self,
        *,
        llm: LLMProvider,
        vector_store: VectorStore,
        budget_remaining_roubles: Callable[[], float] | None = None,
    ) -> None:
        # budget_remaining_roubles — шов бюджета (ruling Q7 ARCH-GATE S09):
        # composition подключает к резидентному аккумулятору адаптера; None =
        # бюджетный контур ещё не подключён — эскалация без проверки
        # (документированный fallback до S15 BudgetGuard, прецедент plan_joins)
        self._llm = llm
        self._vector_store = vector_store
        self._budget_remaining_roubles = budget_remaining_roubles

    def clarify(
        self,
        question: str,
        *,
        ctx: RunContext,
        permissions: dict[str, object] | None = None,
    ) -> ClarifyResult:
        """Классифицировать вопрос под правами пользователя (S00 контракт 3).

        permissions — карта прав из state (фильтр retrieval живёт в самом
        поиске, NFR-4 ACL-leak=0); параметр — документация контракта вызова,
        сервис фильтр не дублирует.
        """
        # --- Нога 1: retrieval (контекст БЗ; сбой → FAIL: классификация без
        # контекста — ложные вердикты, S00 правило 8; прецедент S09) ---
        try:
            [query_embedding] = self._llm.embed([question])
            documents = self._vector_store.search(
                question, query_embedding, ctx=ctx, top_k=_RETRIEVAL_TOP_K
            )
        except (LLMProviderError, VectorStoreError) as exc:
            return self._fail(f"retrieval недоступен: {exc}")
        context = _context_block(documents)

        # --- Классификация: ретраи M1 (попытка 1 — cheap; 2 — cheap + ошибка;
        # 3 — mid при достатке бюджета с резервом) ---
        error_text: str | None = None
        last_output: str | None = None
        role: Role = _CLASSIFIER_ROLE_START
        calls = 0
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if attempt == ESCALATION_ATTEMPT:
                if (
                    self._budget_remaining_roubles is not None
                    and self._budget_remaining_roubles() <= ESCALATION_RESERVE_ROUBLES
                ):
                    return self._fail(
                        f"эскалация попытки {ESCALATION_ATTEMPT} отклонена: остаток "
                        f"бюджета не покрывает резерв {ESCALATION_RESERVE_ROUBLES} ₽ "
                        "(ADR-007 §4.3) — ретраи исчерпаны",
                        llm_calls=calls,
                    )
                role = ROLE_UP[role]
            messages = self._messages(question, context, error_text, last_output)
            try:
                response = self._llm.complete(role, messages, tools=[_CLARIFY_TOOL_SPEC])
            except LLMProviderError as exc:
                # Политика провайдера исчерпана (backoff/breaker/бюджет) — ретрай
                # M1 поверх неё бессмыслен: честный отказ (S00 правило 8)
                return self._fail(f"шлюз недоступен: {exc}", llm_calls=calls + 1)
            calls += 1
            try:
                verdict = _parse_verdict(response)
            except ValueError as exc:
                error_text = str(exc)
                last_output = _last_output_text(response)
                continue
            if verdict.ambiguity == "unambiguous":
                return ClarifyResult(
                    outcome=ClarifyOutcome.UNAMBIGUOUS,
                    assumptions=[
                        Assumption(source="classifier", text=text) for text in verdict.assumptions
                    ],
                    llm_calls=calls,
                )
            return ClarifyResult(
                outcome=ClarifyOutcome.CLARIFY,
                assumptions=[
                    Assumption(source="classifier", text=text) for text in verdict.assumptions
                ],
                question=verdict.clarifying_question,
                llm_calls=calls,
            )
        return self._fail(
            f"классификация не удалась за {MAX_ATTEMPTS} попытки (M1): "
            f"последняя ошибка: {error_text}",
            llm_calls=calls,
        )

    def _messages(
        self,
        question: str,
        context: str,
        error_text: str | None,
        last_output: str | None,
    ) -> list[dict[str, str]]:
        """M1-контекст ретрая: последний выход + текст ошибки, не цепочка попыток (D-016)."""
        messages = [
            {"role": "system", "content": _CLARIFY_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Вопрос: {question}\n\n"
                    f"Контекст базы знаний:\n{context or '(релевантных документов не нашлось)'}"
                ),
            },
        ]
        if error_text is not None:
            messages.append({"role": "assistant", "content": last_output or ""})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"Предыдущий выход не прошёл валидацию: {error_text}. "
                        "Вызови тул record_clarification повторно, строго по схеме."
                    ),
                }
            )
        return messages

    @staticmethod
    def _fail(error: str, *, llm_calls: int = 0) -> ClarifyResult:
        return ClarifyResult(outcome=ClarifyOutcome.FAIL, error=error, llm_calls=llm_calls)

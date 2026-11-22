"""Сервис узла generate_sql — наполнение JOIN-скелета моделью (feat-010, S10).

«Код строит джойны, модель пишет остальное» (hw-01 §2.1): скелет построен кодом
(domain/skeleton.py — FROM/JOIN/ON + обязательные предикаты), модель deepseek
(mid, матрица ADR-007 §4.2) вызывает тул ``fill_skeleton`` ровно один раз с
ЧЕТЫРЬМЯ полями — select_list/where/group_by/order_by (ruling R1 ARCH-GATE
S10); финальный SQL собирает КОД (``assemble_sql``) — модель физически не пишет
джойны, «галлюцинация связи» невозможна по построению.

Структурированный выход — тул (ADR-008 слой 2, прецеденты rank_paths S09 /
record_clarification S08); pydantic ниже — валидация СУЩНОСТИ наполнения.
Reasoning-only выход (D-021) тулом не является — парсер обязан упасть и уйти
в ретрай. Defense-in-depth поверх сборки кодом: наполнение, упоминающее таблицы
вне скелета (мир агента = права − таблицы плана), отклоняется парсером → ретрай
(ruling R5 ARCH-GATE: deny-by-default контракта 3; behavior feat-010 «выдуманные
таблицы невозможны»).

Retrieval-нога — через порт VectorStore (контекст БЗ по правам, паттерн S09/S08):
сбой → честный FAIL (генерация без контекста — SQL не по семантике реестра).
Ретраи M1 (D-016): попытка 1 — mid; попытка 2 — mid + текст ошибки (в контексте —
только последний выход и ошибка, не цепочка попыток, контракт 2 S00); попытка 3 —
strong (эскалация mid→strong) при остатке бюджета с резервом 0,6 ₽, иначе честный
отказ (не молчаливое продолжение на mid). FSM-ретраи через diagnose_error — S14:
стартовая роль/попытка и контекст последней ошибки приходят параметром (ruling
R7 ARCH-GATE).
"""

import json
import re
from collections.abc import Callable, Mapping, Sequence
from enum import StrEnum

import sqlglot
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlglot import exp as sql_exp
from sqlglot.errors import ParseError as SqlParseError
from sqlglot.errors import TokenError as SqlTokenError

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
from text_to_sql_agent.domain.skeleton import Skeleton, assemble_sql

# --- Константы (AGENTS.md: магические числа — именованные, с «почему») ---

# top_k retrieval: баланс полноты/шума (как в plan_joins/clarify — решение S24 #1а)
_RETRIEVAL_TOP_K = 8
# Сниппет одного документа: генератору нужны определения/usage_notes, не полные DDL
_SNIPPET_CHARS = 400
# Кап суммарного контекста БЗ: генерация — самый дорогой промпт узла, канон §6
# держит весь прогон ~6,6К токенов (скелет+вопрос уже занимают основную часть)
_CONTEXT_MAX_CHARS = 2000

# Роль попытки 1 — матрица ADR-007 §4.2: generate_sql = deepseek mid («модель
# наполняет готовый скелет — средней достаточно, эскалация редка»)
_GENERATOR_ROLE_START: Role = "mid"

# ADR-008 слой 2: structured output — через тул (tool-based enforcement)
_FILL_TOOL_NAME = "fill_skeleton"
_FILL_TOOL_SPEC: dict[str, object] = {
    "type": "function",
    "function": {
        "name": _FILL_TOOL_NAME,
        "description": (
            "Наполнение готового скелета SQL-запроса. Вызывается ровно один раз: "
            "четыре поля наполнения; FROM/JOIN/ON фиксированы скелетом и в поля "
            "не входят."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "select_list": {
                    "type": "string",
                    "description": (
                        "Выражения SELECT: столбцы и/или агрегаты через запятую (без слова SELECT)."
                    ),
                },
                "where": {
                    "type": "string",
                    "description": (
                        "Условия фильтрации вопроса (без слова WHERE; обязательные "
                        "предикаты скелета уже включены — не дублировать). Пусто/null — "
                        "фильтров нет."
                    ),
                },
                "group_by": {
                    "type": "string",
                    "description": "Столбцы группировки (без слов GROUP BY). Пусто/null — нет.",
                },
                "order_by": {
                    "type": "string",
                    "description": ("Сортировка (без слов ORDER BY). Пусто/null — нет."),
                },
            },
            "required": ["select_list"],
        },
    },
}

_GENERATE_SQL_SYSTEM_PROMPT = (
    "Ты — генератор SQL для аналитического агента данных. Тебе дают ГОТОВЫЙ скелет "
    "запроса: FROM/JOIN/ON и обязательные предикаты построены кодом из реестра "
    "связей и ИЗМЕНЯТЬ ИХ НЕЛЬЗЯ. Вызови тул fill_skeleton ровно один раз: "
    "select_list — выражения SELECT; where — условия вопроса (обязательные "
    "предикаты уже включены, их не повторяй); group_by/order_by — при "
    "необходимости. Не добавляй FROM/JOIN/ON/подзапросы; не упоминай таблицы, "
    "которых нет в скелете; не выдумывай колонки; не используй алиасы таблиц — "
    "алиасы скелетом не объявлены, обращайся к таблицам по полным именам. "
    "Учитывай допущения, принятые ранее."
)

# Честный отказ с уточняющим вопросом (Task 3: исчерпание → fail-вердикт с
# вопросом, не выдуманный SQL) — шаблон кода
_FAIL_CLARIFY_QUESTION = (
    "Не удалось подготовить SQL по этому вопросу — переформулируйте его: какая "
    "метрика, за какой период и с какими фильтрами вас интересуют?"
)


class GenerateSqlOutcome(StrEnum):
    """Исход сервиса generate_sql: SQL или честный отказ (FSM-маршрут — routing)."""

    SQL = "sql"  # валидный по форме SELECT собран кодом — дальше validate_sql
    FAIL = "fail"  # ретраи исчерпаны / шлюз или retrieval недоступны — отказ узла


class FillFields(BaseModel):
    """Structured output генератора (ADR-008): 4 поля наполнения скелета.

    select_list обязателен и непуст (SQL без SELECT-списка бессмысленен);
    остальные поля опциональны (пусто = секции нет).
    """

    model_config = ConfigDict(frozen=True)

    select_list: str
    where: str | None = None
    group_by: str | None = None
    order_by: str | None = None

    @field_validator("select_list")
    @classmethod
    def _select_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("select_list пуст — наполнение без SELECT-списка бессмысленно")
        return value


class GenerateSqlResult(BaseModel):
    """Результат узла generate_sql: SQL (собран кодом) или вердикт отказа.

    ``llm_calls`` — учёт прогона для state.budget_used (гвард капов ≤ 8 —
    routing.llm_guard; ₽-аккумулятор — резидент адаптера, в state — S15);
    ``escalated`` — фактическая эскалация на strong (пейлоад трейса, ruling R6).
    """

    model_config = ConfigDict(frozen=True)

    outcome: GenerateSqlOutcome
    sql: str | None = None
    error: str | None = None  # причина отказа — в трейс, не пользователю (S9)
    clarifying_question: str | None = None  # честный отказ — вопрос переформулировки
    llm_calls: int = Field(ge=0, default=0)
    escalated: bool = False


def _parse_fill(response: LLMResponse) -> FillFields:
    """Тул-вызов → поля наполнения; любые отклонения — ValueError (текст для ретрая M1).

    Reasoning-only текст без тул-вызова (D-021) — ValueError, не «понимание»
    текста (Ловушка 3 S08: структурный парсер обязан падать).
    """
    calls = [tc for tc in response.tool_calls if tc.name == _FILL_TOOL_NAME]
    if not calls:
        raise ValueError(
            "выход шлюза без вызова тула fill_skeleton"
            + (" (текст — вероятно reasoning, D-021)" if response.text else "")
        )
    if len(calls) > 1:
        raise ValueError(f"ожидался один вызов тула {_FILL_TOOL_NAME}, получено {len(calls)}")
    try:
        arguments = json.loads(calls[0].arguments)
    except json.JSONDecodeError as exc:
        raise ValueError(f"аргументы тула не JSON: {exc}") from None
    if not isinstance(arguments, dict):
        raise ValueError("аргументы тула — не JSON-объект")
    try:
        return FillFields.model_validate(arguments)
    except ValidationError as exc:
        raise ValueError(f"наполнение не прошло схему: {exc}") from None


def _ensure_tables_from_skeleton(
    fields: FillFields, *, skeleton_tables: Sequence[str], world_tables: set[str]
) -> None:
    """Defense-in-depth (ruling R5): наполнение не упоминает таблицы вне скелета.

    world_tables — права пользователя (мир агента, S00 контракт 3): таблица из
    прав, но не из плана, в наполнении — выдуманная для этого запроса → ретрай
    (таблицы вне прав и так не существуют в мире агента). Структурную
    невозможность чужого FROM/JOIN даёт _validate_fill_structure (ревью C1) —
    эта проверка ловит упоминания ИЗВЕСТНЫХ агенту таблиц мимо плана.
    """
    foreign = sorted(world_tables - set(skeleton_tables))
    if not foreign:
        return
    blob = " ".join(
        part
        for part in (
            fields.select_list,
            fields.where or "",
            fields.group_by or "",
            fields.order_by or "",
        )
        if part
    )
    for table in foreign:
        if re.search(rf"\b{re.escape(table)}\b", blob):
            raise ValueError(
                f"наполнение упоминает таблицу {table}, которой нет в скелете — "
                "модель не выбирает таблицы (только SELECT/WHERE/GROUP BY/ORDER BY)"
            )


def _validate_fill_structure(field: str, text: str) -> None:
    """Структурная проверка поля наполнения sqlglot-ом (ревью S10 C1).

    Текст модели — НЕ доверенный: парсим поле в контексте пробного SELECT
    (диалект postgres) и отклоняем → ретрай M1: ошибки парсинга, JOIN,
    подзапросы, UNION/INTERSECT/EXCEPT (модель не пишет структуры SQL — только
    содержимое 4 секций). Лексический барьер (комментарии/«;»/скобки) —
    ``domain.skeleton.validate_fill_text``, вызывается assemble_sql; здесь —
    структурный уровень, полная валидация — S11.
    """
    if not text.strip():
        return
    probes = {
        "select_list": f"SELECT {text}",
        "where": f"SELECT 1 WHERE {text}",
        "group_by": f"SELECT 1 GROUP BY {text}",
        "order_by": f"SELECT 1 ORDER BY {text}",
    }
    try:
        tree = sqlglot.parse_one(probes[field], read="postgres")
    except (SqlParseError, SqlTokenError) as exc:
        # TokenError — токенизатор шлюза sqlglot (незакрытый «/*»): не наследует
        # ParseError, но тот же исход — отклонение поля (ревью C1-регресс)
        raise ValueError(f"поле {field} — не валидный SQL-фрагмент: {exc}") from None
    nested = [node for node in tree.find_all(sql_exp.Select) if node is not tree]
    forbidden = [
        node.sql()
        for node in tree.find_all(
            sql_exp.Join, sql_exp.Subquery, sql_exp.Union, sql_exp.Intersect, sql_exp.Except
        )
    ]
    if nested or forbidden:
        raise ValueError(
            f"поле {field} содержит структуру SQL (подзапрос/JOIN/UNION) — модель "
            "наполняет только содержимое секций; FROM/JOIN/ON фиксированы скелетом"
        )


def _last_output_text(response: LLMResponse) -> str:
    """Последний выход шлюза для M1-контекста ретрая (D-016: не цепочка попыток)."""
    if response.tool_calls:
        return "; ".join(tc.arguments for tc in response.tool_calls)
    return response.text or ""


def _context_block(documents: Sequence[KnowledgeDocument]) -> str:
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


class GenerateSqlService:
    """Узел generate_sql: retrieval-нога + наполнение скелета с ретраями M1."""

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

    def generate(
        self,
        *,
        skeleton: Skeleton,
        question: str,
        ctx: RunContext,
        assumptions: Sequence[Assumption] = (),
        permissions: Mapping[str, Sequence[str]] | None = None,
        last_sql: str | None = None,
        last_error: str | None = None,
        start_role: Role = _GENERATOR_ROLE_START,
        start_attempt: int = 1,
    ) -> GenerateSqlResult:
        """Наполнить скелет: SQL поверх фиксированных джойнов или честный отказ.

        ``last_sql``/``last_error`` — вход FSM-ретрая (diagnose_error, S14):
        в контексте только последняя попытка + текст ошибки (контракт 2 S00);
        ``start_role``/``start_attempt`` — сеются узлом из ``attempts_used``
        (ruling R7 ARCH-GATE: S14 подключит ребро, сервису знать state не нужно).
        ``permissions`` — мир агента (S00 контракт 3) для defense-in-depth
        парсера (ruling R5).
        """
        if not question.strip():
            return self._fail("вопрос пуст — наполнение скелета невозможно")
        if start_attempt > MAX_ATTEMPTS:
            return self._fail(
                f"попытки исчерпаны (старт с попытки {start_attempt} > {MAX_ATTEMPTS})",
                clarifying_question=_FAIL_CLARIFY_QUESTION,
            )

        # --- Нога 1: retrieval (контекст БЗ; сбой → FAIL: генерация без
        # контекста даёт SQL не по семантике реестра, S00 правило 8) ---
        try:
            [query_embedding] = self._llm.embed([question])
            documents = self._vector_store.search(
                question, query_embedding, ctx=ctx, top_k=_RETRIEVAL_TOP_K
            )
        except (LLMProviderError, VectorStoreError) as exc:
            return self._fail(f"retrieval недоступен: {exc}")
        context = _context_block(documents)

        # --- Наполнение: ретраи M1 (попытка 1 — mid; 2 — mid + ошибка;
        # 3 — strong при достатке бюджета с резервом) ---
        world_tables = set(permissions or {})
        role: Role = start_role
        error_text: str | None = last_error
        last_output: str | None = last_sql
        calls = 0
        escalated = False
        for attempt in range(max(start_attempt, 1), MAX_ATTEMPTS + 1):
            if attempt == ESCALATION_ATTEMPT and role != ROLE_UP[role]:
                # Эскалация — только вверх и только при остатке бюджета с
                # резервом (ADR-007 §4.3); не хватает — честный отказ
                if (
                    self._budget_remaining_roubles is not None
                    and self._budget_remaining_roubles() <= ESCALATION_RESERVE_ROUBLES
                ):
                    return self._fail(
                        f"эскалация попытки {ESCALATION_ATTEMPT} отклонена: остаток "
                        f"бюджета не покрывает резерв {ESCALATION_RESERVE_ROUBLES} ₽ "
                        "(ADR-007 §4.3) — ретраи исчерпаны",
                        llm_calls=calls,
                        clarifying_question=_FAIL_CLARIFY_QUESTION,
                    )
                role = ROLE_UP[role]
                escalated = True
            messages = self._messages(
                skeleton=skeleton,
                question=question,
                assumptions=assumptions,
                context=context,
                error_text=error_text,
                last_output=last_output,
            )
            try:
                response = self._llm.complete(role, messages, tools=[_FILL_TOOL_SPEC])
            except LLMProviderError as exc:
                # Политика провайдера исчерпана (backoff/breaker/бюджет) — ретрай
                # M1 поверх неё бессмыслен: честный отказ (S00 правило 8)
                return self._fail(f"шлюз недоступен: {exc}", llm_calls=calls + 1)
            calls += 1
            try:
                fields = _parse_fill(response)
                _validate_fill_structure("select_list", fields.select_list)
                for optional_field in ("where", "group_by", "order_by"):
                    _validate_fill_structure(optional_field, getattr(fields, optional_field) or "")
                _ensure_tables_from_skeleton(
                    fields,
                    skeleton_tables=skeleton.tables,
                    world_tables=world_tables,
                )
                sql = assemble_sql(
                    skeleton,
                    select_list=fields.select_list,
                    where=fields.where,
                    group_by=fields.group_by,
                    order_by=fields.order_by,
                )
            except ValueError as exc:
                # Сюда же попадает лексический барьер assemble_sql (ревью C1:
                # комментарии/«;»/скобки — assemble «заряжен» ни для кого)
                error_text = str(exc)
                last_output = _last_output_text(response)
                continue
            return GenerateSqlResult(
                outcome=GenerateSqlOutcome.SQL,
                sql=sql,
                llm_calls=calls,
                escalated=escalated,
            )
        return self._fail(
            f"наполнение скелета не удалось за {MAX_ATTEMPTS - max(start_attempt, 1) + 1} "
            f"попытки (M1): последняя ошибка: {error_text}",
            llm_calls=calls,
            clarifying_question=_FAIL_CLARIFY_QUESTION,
        )

    def _messages(
        self,
        *,
        skeleton: Skeleton,
        question: str,
        assumptions: Sequence[Assumption],
        context: str,
        error_text: str | None,
        last_output: str | None,
    ) -> list[dict[str, str]]:
        """M1-контекст ретрая: последний выход + текст ошибки, не цепочка попыток (D-016)."""
        skeleton_text = "\n".join([f"FROM {skeleton.from_table}", *skeleton.join_sections])
        predicates = (
            "; ".join(skeleton.mandatory_predicates) if skeleton.mandatory_predicates else "(нет)"
        )
        assumption_text = "; ".join(a.text for a in assumptions) if assumptions else "(нет)"
        messages = [
            {"role": "system", "content": _GENERATE_SQL_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Вопрос: {question}\n\n"
                    f"Принятые допущения: {assumption_text}\n\n"
                    f"Скелет запроса (ИЗМЕНЯТЬ ЗАПРЕЩЕНО):\n{skeleton_text}\n\n"
                    f"Обязательные предикаты (уже в WHERE, не дублируй): {predicates}\n\n"
                    f"Контекст базы знаний:\n{context or '(релевантных документов не нашлось)'}"
                ),
            },
        ]
        if error_text is not None:
            # Пустой assistant-ход часть шлюзов отвергает (ревью S10 minor) —
            # добавляем только непустой последний выход
            if last_output:
                messages.append({"role": "assistant", "content": last_output})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"Ошибка предыдущей попытки: {error_text}. Вызови тул "
                        f"{_FILL_TOOL_NAME} повторно, строго по схеме."
                    ),
                }
            )
        return messages

    @staticmethod
    def _fail(
        error: str,
        *,
        llm_calls: int = 0,
        clarifying_question: str | None = None,
    ) -> GenerateSqlResult:
        return GenerateSqlResult(
            outcome=GenerateSqlOutcome.FAIL,
            error=error,
            llm_calls=llm_calls,
            clarifying_question=clarifying_question,
        )

"""Доменные сущности и value objects — без IO, без внешних зависимостей, без LLM
(AGENTS.md Architecture Boundaries; S00 правило 6).
"""

from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field, field_validator


class Trace(BaseModel):
    """Событие наблюдаемости узла/адаптера (FR-11, поля node_timings).

    VO без LLM-концепций: только имя события и метаданные; формат ответа LLM
    знает порт `LLMResponse`, не domain. Временную метку ставит трейсер при записи.
    """

    event: str
    payload: dict[str, Any] = Field(default_factory=dict)


class Assumption(BaseModel):
    """Допущение прогонa (ruling R5 ARCH-GATE S08): структура фиксирована,
    содержимое — короткий текст.

    «Структурированный список, не свободная проза» (Ловушка 4 S08): их сверяет
    кэш (S16 — ключ кэша с assumptions) и раскрывает interpret_result (S15).
    ``source`` — провенанс: ``classifier`` — допущение вердикта классификатора
    (закрытый набор схемы тула, валидирует код), ``user_answer`` — ответ
    пользователя на уточняющий вопрос (interrupt/resume — свободный текст по
    природе, структура даёт машиночитаемость).
    """

    model_config = {"frozen": True}

    source: Literal["classifier", "user_answer"]
    text: str

    @field_validator("text")
    @classmethod
    def _text_non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Assumption.text обязано быть непустым (fail-closed)")
        return value


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


# --- Терминальные исходы FSM (state-agent v1.5; S00 стык 1) ---
# Все — валидные done-состояния протокола, не исключения (ADR-002 §4: «исчерпание
# любого уровня — отказ с уточняющим вопросом, не исключение наверх»). done —
# полный ответ после interpret_result; degrade — отказ/доступный вариант по правам
# (маркером после узла degrade); clarification — уточняющий вопрос (interrupt()
# с Checkpointer — S08, в каркасе S04 — терминал по флагу); rejected — identity
# не проверена (fail-closed); fail — отказ + уточняющий вопрос после исчерпания
# попыток; degraded_out — T2-исход async/отказ с рекомендацией; short_circuit —
# «данных нет» (пустой key-set шага, hw-01 §3.5.4); overflow — кап кардинальности
# key-set превышен; budget_stop — глобальная капа прогона (≤ 8 вызовов / ≤ 90 s /
# ≤ 2 ₽) или превышение стоимости на плановой точке check_cost.
TerminalOutcome = Literal[
    "done",
    "degrade",
    "short_circuit",
    "overflow",
    "fail",
    "rejected",
    "degraded_out",
    "budget_stop",
    "clarification",
]

TERMINAL_OUTCOMES: tuple[TerminalOutcome, ...] = (
    "done",
    "degrade",
    "short_circuit",
    "overflow",
    "fail",
    "rejected",
    "degraded_out",
    "budget_stop",
    "clarification",
)


class AgentState(TypedDict, total=False):
    """State между узлами FSM — контракт стыка 1 (S00; решение D-004, ADR-002).

    Стандартный ``typing.TypedDict``: LangGraph потребляет его нативно (канон S00
    «typed dict LangGraph»), при этом domain остаётся без внешних зависимостей —
    импорт langgraph сюда запрещён (ARCH-GATE S04, вопрос Q1).

    Инвариант «узел читает только свои поля и пишет только свои» (S00 стык 1) —
    карта владения полей:

    ================  ==========================================================
    Поле              Владелец (пишет)
    ================  ==========================================================
    question          точка входа (не узел); узлы только читают
    user_id           точка входа (AuthProvider.verify ДО прогона — ruling R4
                      S05: identity только из проверенного токена, сырой user_id
                      verify не обходит); resolve_access читает, fail-closed на
                      пустом
    rewritten_question  clarify
    assumptions       clarify (list[Assumption] — ruling R5 ARCH-GATE S08)
    clarify_question  вопрос пользователю, ожидающий ответа (interrupt-механика,
                      ruling R1/R2 ARCH-GATE S08): clarify пишет вопрос вердикта
                      классификатора, plan_joins — вопрос равных кандидатов (S10);
                      читают clarify (interrupt) и ребро routing (само-ребро
                      clarify → clarify)
    permissions       resolve_access
    graph_version     resolve_access (0 = снапшот не загружен — ADR-006 §4.3)
    join_plan         plan_joins
    skeleton          build_skeleton
    skeleton_hash     build_skeleton
    sql               generate_sql / generate_step_sql; validate_sql перезаписывает
                      исполненным текстом (внешний LIMIT добавлен/скламплен —
                      ruling R1 S11, звёзды развёрнуты в разрешённые колонки —
                      ruling R2 S11; исходный текст модели к исполнителю не идёт)
    validation        validate_sql: {outcome: ok|hallucination|denied (ADR-005 §4),
                      issues (внутренний контекст ретрая M1/diagnose_error — S14;
                      пользовательский текст формулирует degrade бизнес-понятиями,
                      БЕЗ имён таблиц — S9), enforced_limit}
    rows/row_count    execute_sql / merge_results (слияние шагов)
    compact           compact_result
    error             diagnose_error / узлы, поймавшие сбой; кодовое поле — читается
                      ребром execute_sql (SQL-ошибка → diagnose_error, routing),
                      успешный execute_sql ОБЯЗАН очищать его (ревью S04 M2)
    diagnosis         diagnose_error (S14): {error_class, hint, retry_futile} —
                      внутренний контекст ретрая M1 (подсказка регенерации,
                      D-016); читает generate_sql при FSM-ретрае; пользовательский
                      текст не смешивается (S9: state.error.message остаётся
                      фиксированным)
    retry_count       код-обвязка (гейт ребра ретрая; см. политику ниже)
    budget_used       код-обвязка (учёт узлов с LLM; заглушки имитируют — каждый
                      вход в LLM-узел «платит» вызовом, гвард капов живой)
    route_flags       ТОЛЬКО код (S00 стык 1): каждый флаг — своего узла/ребра
    truncated         execute_sql (LIMIT 1001+пост-срез, S13)
    approximate       check_cost (T2 → approximate/matview, S12)
    incomplete        BudgetGuard/interpret_result (S15: неполное не выдаётся
                      за полное — контракт budget_stop)
    ------ обвязка orchestration (не содержательные узлы; синк S00 стык 1) ----
    visited_nodes     обвязка graph.py (порядок обхода содержательных узлов —
                      для трейса и печати CLI; гейты/терминалы не пишут)
    outcome           терминальный маркер graph.py (различимый payload исхода)
    deadline_at       оркестратор-раннер (monotonic-дедлайн прогона, кап 90 s;
                      при resume перевзводится раннером — ruling R6 ARCH-GATE S08)
    trace_id          оркестратор-раннер (uuid прогона — сквозная корреляция
                      RunContext/трейса, ADR-010; ставится на старте прогона)
    interrupted       оркестратор-раннер (маркер приостановки GraphInterrupt:
                      прогон не завершён — ожидает resume, ruling R3 ARCH-GATE
                      S08; False = исходный маркер)
    ================  ==========================================================

    ``retry_count`` — один int: счётчик ретраев УЗЛА, который сейчас ретраится
    (политика M1, D-016: попытка 2 — та же модель + текст ошибки, попытка 3 —
    уровень вверх). Инкремент — кодом ребра ретрая (гейт-узел в orchestration,
    Task 3 S04: «инкремент — кодом ребра»), сброс — при смене ретрауемого узла
    (гейт читает ``route_flags[FLAG_RETRY_TARGET]``). Счётчик diagnose_error на
    ребре diagnose_error → generate_sql инкрементирует generate_sql: ретраится
    генератор, не диагност (ARCH-GATE S04).

    ``route_flags`` — словарь кодовых решений маршрута; состав флагов — константы
    ``FLAG_*`` в orchestration/routing.py (единственный источник имён). Пишут
    только узлы/обвязка кодом, модель не возвращает имя узла (ADR-002: карта
    решений). LLM-текст может попадать только в содержательные поля (sql,
    join_plan, …) — маршрут читает исключительно ``route_flags``/``retry_count``
    (инвариант, проверяемый тестом).
    """

    # Контракт стыка 1, S00 дословно
    question: str
    user_id: str  # verified identity (R4 S05) — точка входа, узел resolve_access читает
    rewritten_question: str | None
    assumptions: list[Assumption]  # ruling R5 ARCH-GATE S08: VO с провенансом
    clarify_question: str | None  # вопрос пользователю — interrupt-механика (S08, R1/R2)
    permissions: dict[str, Any]  # {tables: {table: [columns]}} — scope (стык 3)
    graph_version: int | None  # пин снапшота реестра на прогон (ADR-006 §4.3)
    join_plan: dict[str, Any] | None
    skeleton: dict[str, Any] | None
    skeleton_hash: str | None
    sql: str | None
    validation: dict[str, Any] | None
    rows: list[Any] | None
    row_count: int | None
    compact: dict[str, Any] | None  # ResultCompact (pydantic — S15/ADR-008)
    error: dict[str, Any] | None  # {class, message}
    diagnosis: dict[str, Any] | None  # {error_class, hint, retry_futile} — S14
    retry_count: int
    budget_used: dict[str, Any] | None  # {llm_calls: int, rub: float}
    route_flags: dict[str, Any]
    truncated: bool
    approximate: bool
    incomplete: bool
    # Обвязка orchestration (синк S00 стык 1 — тем же коммитом; ARCH-GATE R3/R4)
    visited_nodes: list[str]
    outcome: TerminalOutcome | None
    deadline_at: float | None  # time.monotonic() дедлайн прогона (кап 90 s)
    trace_id: str | None  # uuid прогона (корреляция RunContext/трейса — ADR-010)
    interrupted: bool  # прогон приостановлен GraphInterrupt — ждёт resume (R3 S08)

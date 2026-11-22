"""Валидатор SQL — доменная логика вердикта над нейтральным IR (feat-011, S11).

Главный рубеж безопасности (hw-01 §3.2, ADR-005 v1.2) между generate_sql и
исполнением. Валидатор — ЧИСТАЯ функция над (IR, скелет, права): без IO и LLM;
sqlglot-парсинг и обход AST живут в сервисе (ruling R4 ARCH-GATE S11: services
парсят → нейтральный IR → domain решает вердикт; whitelist типов узлов —
fail-closed в обходе services). Три исхода (ADR-005 §4): ``ok`` → check_cost;
``hallucination`` («не существует в мире агента») → diagnose_error, retry
оправдан; ``denied`` («существует, но вне прав») → degrade БЕЗ retry — модель
не должна «уговорить» валидатор.

Реализация по задачам плана S11 (сессия ◐, инкременты одной сессии):
- Task 1 (этот коммит) — форма AST: одна инструкция, корень SELECT, allow-list
  функций (план правило 2: «функции из allow-list; всё новое — отказ»), внешний
  LIMIT (ruling R1 ARCH-GATE: SQL от assemble_sql всегда БЕЗ LIMIT — валидатор
  добавляет/клампит до ROW_LIMIT);
- Task 2 — ACL: deny-list схем (мир агента — public), таблицы/колонки ∈
  permissions + каталог мира (data_catalog — классификация «несуществующее» vs
  «вне прав», plan правило 4), разворот ``*`` в разрешённые колонки (ADR-005
  §4 п.2, ruling R2);
- Task 3 — скелет: skeleton_hash (state ↔ VO), структура джойнов = JoinPlan
  (тип/таблица/пары ON/порядок), mandatory_predicates в WHERE внешнего запроса.

Параметры API полны с первого коммита (контракт wiring Task 4): узел FSM всегда
передаёт skeleton/join_plan/permissions/data_catalog — непроимплементированные
разделы не пропускают ничего лишнего только потому, что до Task 4 валидатор в
FSM не подключён (заглушка S04 продолжает работать).
"""

from collections.abc import Mapping, Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from text_to_sql_agent.domain.join_plan import JoinPlan
from text_to_sql_agent.domain.skeleton import Skeleton

# Allow-list функций (план S11 правило 2): аналитический минимум демо-корпуса —
# агрегаты, строки, даты, приведение типов. Опасные функции (dblink, pg_read_file,
# pg_sleep, lo_import, …) отсутствуют ПО ПОСТРОЕНИЮ — deny-by-default превращает
# неполноту списка из молчаливой утечки в явный отказ (ADR-005 §4 п.1). Имена —
# канонические sqlglot (date_trunc парсится в timestamp_trunc). Расширение списка —
# осознанное решение с тестом, не «поймали отказ — добавили».
ALLOWED_FUNCTIONS: frozenset[str] = frozenset(
    {
        "count",
        "sum",
        "avg",
        "min",
        "max",
        "coalesce",
        "nullif",
        "lower",
        "upper",
        "length",
        "abs",
        "round",
        "cast",
        "extract",
        "date_trunc",
        "timestamp_trunc",
        "current_date",
        "current_timestamp",
    }
)

# Мир агента — публичная схема (ADR-005 рубеж 3: pg_catalog/information_schema/
# agent_* и прочие — «не существуют для агента»; deny-by-default: квалификация
# любой другой схемой — отказ, один SELECT не утаскивает топологию БД).
PUBLIC_SCHEMA = "public"


class ValidationOutcome(StrEnum):
    """Исход валидации — носитель маршрута (routing.route_validate_sql)."""

    OK = "ok"
    HALLUCINATION = "hallucination"
    DENIED = "denied"


class SqlColumn(BaseModel):
    """Ссылка на колонку с кандидатами-источниками (резолюция алиасов — services).

    ``candidates`` — имена-источники: bare-имена таблиц, имена CTE или алиасы
    производных таблиц/LATERAL. Квалифицированная колонка — один кандидат;
    unqualified — все источники скоупа (Ловушка 5: неоднозначность/вне прав
    любого кандидата → отказ, не «повезёт с дефолтом»).
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    candidates: tuple[str, ...] = Field(min_length=1)


class SqlJoin(BaseModel):
    """Джойн внешнего запроса в порядке появления (сверка со скелетом — Task 3).

    ``join_type`` — нормализованный токен (inner/left/comma/...); ``on_pairs`` —
    канонизированные пары колонок ON (каждая пара отсортирована — направление
    равенства не семантично; общий порядок — по парам).
    """

    model_config = ConfigDict(frozen=True)

    join_type: str = Field(min_length=1)
    table: str = Field(min_length=1)
    on_pairs: tuple[tuple[str, str], ...] = ()


class SqlAudit(BaseModel):
    """Нейтральный IR запроса (ruling R4 ARCH-GATE): построен services из sqlglot.

    ``tables`` — все relation-ссылки как написано (``schema.table``/``table``,
    включая тела CTE; ссылки на CTE-имена — не таблицы и не входят).
    ``where_conjuncts`` — конъюнкты WHERE внешнего запроса, нормализованные
    тексты (единый источник нормализации — ``normalize_sql_fragment`` S10).
    """

    model_config = ConfigDict(frozen=True)

    from_table: str = ""  # bare-имя источника FROM внешнего запроса ("" у set-операций)
    tables: tuple[str, ...] = ()
    cte_names: tuple[str, ...] = ()
    columns: tuple[SqlColumn, ...] = ()
    joins: tuple[SqlJoin, ...] = ()
    where_conjuncts: tuple[str, ...] = ()
    outer_limit: int | None = None
    functions: tuple[str, ...] = ()
    star_sources: tuple[tuple[str, ...], ...] = ()  # источники скоупа на каждую «*»


class ValidationVerdict(BaseModel):
    """Вердикт валидатора: исход + человекочитаемые нарушения + исполненный LIMIT.

    ``issues`` — вход ретрая M1 (D-016: «попытка 2 — та же модель + текст ошибки»)
    и diagnose_error (S14); порядок: denied-нарушения первыми (сигнал безопасности).
    ``enforced_limit`` — внешний LIMIT итогового SQL: добавлен/скламплен/сохранён
    (ruling R1); подстановки текста выполняет services на AST.
    """

    model_config = ConfigDict(frozen=True)

    outcome: ValidationOutcome
    issues: tuple[str, ...] = ()
    enforced_limit: int


def validate_sql(
    audit: SqlAudit,
    *,
    skeleton: Skeleton,
    join_plan: JoinPlan,
    skeleton_hash: str,
    permissions: Mapping[str, Sequence[str]],
    data_catalog: frozenset[str] | set[str],
    row_limit: int,
) -> ValidationVerdict:
    """Вердикт над IR (чистая функция; все входы обязательны — fail-closed wiring).

    Task 1 (реализовано): allow-list функций; внешний LIMIT (ruling R1).
    Task 2/3 (следующие коммиты этой сессии): ACL и скелет по плану — параметры
    приняты контрактом API, проверки добавляются в своих задачах; до wiring
    (Task 4) в FSM работает заглушка S04, поэтому «недо-валидатор» недостижим.
    """
    if row_limit <= 0:
        raise ValueError(f"row_limit должен быть положительным, получен {row_limit}")
    issues_denied: list[str] = []
    issues_hallucination: list[str] = []
    _check_functions(audit, issues_hallucination)
    enforced = _enforced_limit(audit.outer_limit, row_limit)
    return ValidationVerdict(
        outcome=_merge_outcome(issues_denied, issues_hallucination),
        issues=tuple(issues_denied + issues_hallucination),
        enforced_limit=enforced,
    )


def _check_functions(audit: SqlAudit, issues: list[str]) -> None:
    """Allow-list функций (план правило 2): неизвестная — отказ, не «пропустили»."""
    for name in audit.functions:
        if name not in ALLOWED_FUNCTIONS:
            issues.append(
                f"функция {name} вне allow-list — разрешены только аналитические "
                "функции из фиксированного списка"
            )


def _enforced_limit(original: int | None, row_limit: int) -> int:
    """Внешний LIMIT итогового SQL (ruling R1 ARCH-GATE).

    SQL от assemble_sql всегда без LIMIT (S10 skeleton.py:157 «LIMIT добавляет
    validate_sql») — отсутствие НЕ отказ: подставляется ROW_LIMIT канала
    исполнения; явный лимит больше капа клампится, меньший сохраняется
    (внутренние LIMIT подзапросов не проверяются — кап канала у исполнителя S13).
    """
    if original is None:
        return row_limit
    return min(original, row_limit)


def _merge_outcome(denied: list[str], hallucination: list[str]) -> ValidationOutcome:
    """Слияние исходов: denied доминирует (degrade без retry — сигнал безопасности)."""
    if denied:
        return ValidationOutcome.DENIED
    if hallucination:
        return ValidationOutcome.HALLUCINATION
    return ValidationOutcome.OK

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
- Task 1 — форма AST: одна инструкция, корень SELECT, allow-list
  функций (план правило 2: «функции из allow-list; всё новое — отказ»), внешний
  LIMIT (ruling R1 ARCH-GATE: SQL от assemble_sql всегда БЕЗ LIMIT — валидатор
  добавляет/клампит до ROW_LIMIT);
- Task 2 (реализовано, a62b882) — ACL: deny-list схем (мир агента — public),
  таблицы/колонки ∈ permissions + каталог мира (data_catalog — классификация
  «несуществующее» vs «вне прав», план правило 4), разворот ``*`` в разрешённые
  колонки (ADR-005 §4 п.2, ruling R2); контракт прав BL-014 — значения строго
  «список колонок», CSV-строка → ValueError (свой CSV-парсер валидатор не заводит);
- Task 3 (реализовано, 71506a2) — скелет: skeleton_hash (state ↔ VO), структура
  джойнов = JoinPlan (тип/таблица/пары ON/порядок), mandatory_predicates в WHERE
  внешнего запроса; корневая структура (FROM/JOIN) фиксирована скелетом —
  CTE/UNION/LATERAL в корне = изменённый скелет (вложенные — тело проверяется
  теми же правилами).

Wiring (Task 4, ea6d11e): узел FSM ``make_validate_sql_node`` подключён через
node_overrides; fix-волна ревью S11 — whole-row/звёзды в выражениях (C1),
лексическая видимость CTE (C2), фиксированный пользовательский текст отказов (I1).
"""

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from text_to_sql_agent.domain.join_plan import JoinPlan, JoinPlanEdge
from text_to_sql_agent.domain.skeleton import Skeleton, normalize_sql_fragment

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
        # квантор сравнения ALL (зеркало ANY-узла exp.Any); sqlglot 30.31 парсит
        # «x = ALL(ARRAY[...])» как Anonymous('all') — ревью S11 M4, решение
        # «расширение — с тестом» выполнено
        "all",
    }
)

# Мир агента — публичная схема (ADR-005 рубеж 3: pg_catalog/information_schema/
# agent_* и прочие — «не существуют для агента»; deny-by-default: квалификация
# любой другой схемой — отказ, один SELECT не утаскивает топологию БД).
PUBLIC_SCHEMA = "public"

# Служебные таблицы агента (agent_permissions, agent_query_trace, …) — сборка
# префиксом: infraструктура существует, но вне мира аналитики (ADR-005 рубеж 3) —
# deny, degrade без retry (не «не существует»: она существует, просто не для SQL)
AGENT_TABLE_PREFIX = "agent_"


class ValidationOutcome(StrEnum):
    """Исход валидации — носитель маршрута (routing.route_validate_sql)."""

    OK = "ok"
    HALLUCINATION = "hallucination"
    DENIED = "denied"


class SqlSource(BaseModel):
    """Источник скоупа в IR: bare-имя сущности + природа источника.

    ``table`` — реальная таблица мира (единственный носитель прав — ACL-проверка);
    ``cte``/``sub`` — производный источник (CTE/подзапрос/LATERAL): права на него
    не назначаются — его тело уже проверено на собственном скоупе теми же
    правилами, наружу он отдаёт только свою проверенную проекцию.
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    kind: Literal["table", "cte", "sub"]


class SqlColumn(BaseModel):
    """Ссылка на колонку с кандидатами-источниками (резолюция алиасов — services).

    ``candidates`` — источники скоупа. Квалифицированная колонка — один
    кандидат; unqualified — все источники скоупа (Ловушка 5: вне прав/неоднозначно
    у любого кандидата-таблицы → отказ, не «повезёт с дефолтом»).
    ``whole_row`` — unqualified-ссылка, совпавшая с алиасом источника скоупа
    (PostgreSQL: колонки с таким именем нет → вся строка, ревью S11 C1):
    права проверяются как у литеральной звезды — '*' на каждой таблице-кандидате.
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    candidates: tuple[SqlSource, ...] = Field(min_length=1)
    whole_row: bool = False


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
    тексты с квалификаторами, переписанными на реальные имена источников
    (единый источник нормализации — ``normalize_sql_fragment`` S10).
    """

    model_config = ConfigDict(frozen=True)

    from_table: str = ""  # bare-имя источника FROM внешнего запроса ("" у set-операций)
    tables: tuple[str, ...] = ()
    columns: tuple[SqlColumn, ...] = ()
    joins: tuple[SqlJoin, ...] = ()
    where_conjuncts: tuple[str, ...] = ()
    outer_limit: int | None = None
    functions: tuple[str, ...] = ()
    # источники скоупа на каждую литеральную «*» проекции (COUNT(*) — не проекция)
    star_sources: tuple[tuple[SqlSource, ...], ...] = ()
    # S31 (свободный вход тул-контура): пары утверждённых связей JOIN по всем
    # скоупам — связывающие равенства ON (присоединяемый источник × контрагент;
    # формы без равенств — соседние операнды цепочки); каждый элемент отсортирован
    # (направление не семантично), дублей нет. FSM-вход игнорирует (структуру
    # фиксирует скелет), свободный сверяет с реестром
    join_table_pairs: tuple[tuple[str, str], ...] = ()
    # несверяемая форма: производный источник в операнде JOIN ИЛИ в корреляции,
    # соединение без равенства, связывающего присоединяемый источник с уже
    # присоединёнными в конъюнкции ON, корреляция со скоупом без табличных
    # источников — свободный вход fail-closed, FSM игнорирует
    unresolved_join_scopes: bool = False


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

    Task 1: allow-list функций; внешний LIMIT (ruling R1).
    Task 2 (реализовано): ACL — схемы/таблицы/колонки/литеральные звёзды/
    whole-row; классификация «несуществующее» vs «вне прав» по каталогу мира
    (план правило 4); контракт прав BL-014 (str-value → ValueError).
    Task 3 (реализовано): «скелет не изменён» — skeleton_hash (state ↔ VO),
    структура джойнов = рёбра JoinPlan, mandatory_predicates в WHERE внешнего
    запроса. Wiring (Task 4): узел FSM ``make_validate_sql_node`` подключён в
    composition — валидатор стоит на ребре generate_sql → executor.
    """
    if row_limit <= 0:
        raise ValueError(f"row_limit должен быть положительным, получен {row_limit}")
    _check_permissions_contract(permissions)
    issues_denied: list[str] = []
    issues_hallucination: list[str] = []
    _check_functions(audit, issues_hallucination)
    _check_tables(audit, permissions, data_catalog, issues_denied, issues_hallucination)
    _check_columns(audit, permissions, data_catalog, issues_denied, issues_hallucination)
    _check_stars(audit, permissions, issues_denied)
    _check_skeleton(audit, skeleton, join_plan, skeleton_hash, issues_hallucination)
    enforced = _enforced_limit(audit.outer_limit, row_limit)
    return ValidationVerdict(
        outcome=_merge_outcome(issues_denied, issues_hallucination),
        issues=tuple(issues_denied + issues_hallucination),
        enforced_limit=enforced,
    )


def validate_free_sql(
    audit: SqlAudit,
    *,
    permissions: Mapping[str, Sequence[str]],
    data_catalog: frozenset[str] | set[str],
    row_limit: int,
    join_edges: frozenset[tuple[str, str]],
) -> ValidationVerdict:
    """Вердикт над свободным SQL тул-контура (S31/D-033 п.5 вариант A).

    Общая часть с ``validate_sql`` — функции/таблицы/колонки/звёзды/LIMIT (правило 2
    сессии S31: один валидатор — две политики, второй копии ACL нет); сверки скелета
    НЕТ — плана у тул-контура нет. Вместо неё — инвариант реестра (R1 ARCH-GATE,
    prd FR-2/FR-4 «модель не может придумать связь»): пара операндов каждого JOIN
    обязана быть ребром активного снимка реестра (``join_edges`` — sorted lowercase
    пары от ``SchemaGraphStore.join_edges``); несверяемые формы (производный
    источник в операнде/корреляции, соединение ≥3 таблиц без равенства,
    связывающего присоединяемый источник с уже присоединёнными в конъюнкции ON) —
    fail-closed (``_check_join_pairs``, там же честная граница по semi-join).
    Права, каталог и рёбра читает вызывающий контур на каждый прогон (чистая
    функция, без IO — ruling R4 ARCH-GATE S11).
    """
    if row_limit <= 0:
        raise ValueError(f"row_limit должен быть положительным, получен {row_limit}")
    _check_permissions_contract(permissions)
    issues_denied: list[str] = []
    issues_hallucination: list[str] = []
    _check_functions(audit, issues_hallucination)
    _check_tables(audit, permissions, data_catalog, issues_denied, issues_hallucination)
    _check_columns(audit, permissions, data_catalog, issues_denied, issues_hallucination)
    _check_stars(audit, permissions, issues_denied)
    _check_join_pairs(audit, join_edges, issues_hallucination)
    enforced = _enforced_limit(audit.outer_limit, row_limit)
    return ValidationVerdict(
        outcome=_merge_outcome(issues_denied, issues_hallucination),
        issues=tuple(issues_denied + issues_hallucination),
        enforced_limit=enforced,
    )


def _check_join_pairs(
    audit: SqlAudit,
    join_edges: frozenset[tuple[str, str]],
    issues: list[str],
) -> None:
    """Инвариант реестра (R1 ARCH-GATE S31): соединение — только по рёбрам снапшота.

    Пары — из IR (``join_table_pairs``: пары связывающих равенств ON скоупов —
    присоединяемый источник × контрагент; формы без равенств (USING, 2-табличное
    произведение) — соседние операнды цепочки; пары корреляций — цикл колонок в
    services); пары без ребра — hallucination («связь выдумана», ADR-006: ретрай
    оправдан — модель может перестроить запрос по реальным связям). Несверяемые
    формы (``unresolved_join_scopes``: производный источник в операнде JOIN или
    в корреляции, соединение без связывающего равенства в конъюнкции ON,
    корреляция со скоупом без таблиц) —
    fail-closed hallucination: обёртка CTE/подзапросом связь не легализует (ruling
    «CTE+CTE — алгебра над телами» отозван ревью S31; повторное ревью закрыло обход
    через корреляцию). Вне сверки BY DESIGN: semi-join и скалярный подзапрос БЕЗ
    корреляции (``IN (SELECT …)``, ``= (SELECT …)``, ``= ANY(…)``) сравнивают со
    значение-множеством и соединения строк не образуют — честная граница инварианта
    записана в ADR-006 §4 (BL-045). FSM-вход оба поля игнорирует: структура джойнов
    там сверяется со скелетом плана.
    """
    if audit.unresolved_join_scopes:
        issues.append(
            "соединение вне сверяемых пар таблиц (производный источник в операнде "
            "JOIN или в корреляции, нет равенства, связывающего присоединяемую "
            "таблицу с уже присоединёнными в конъюнкции ON) не сверяется "
            "с реестром — свободный режим поддерживает джойны только между таблицами"
        )
    for left, right in audit.join_table_pairs:
        if left == right:
            # self-join таблицы к себе — реестром не утверждается связь между
            # РАЗНЫМИ таблицами (ревью S31: симметрия с self-join через CTE)
            continue
        if (left, right) not in join_edges:
            issues.append(f"связь {left}↔{right} не существует в реестре связей — джойн придуман")


def _check_functions(audit: SqlAudit, issues: list[str]) -> None:
    """Allow-list функций (план правило 2): неизвестная — отказ, не «пропустили»."""
    for name in audit.functions:
        if name not in ALLOWED_FUNCTIONS:
            issues.append(
                f"функция {name} вне allow-list — разрешены только аналитические "
                "функции из фиксированного списка"
            )


def _check_permissions_contract(permissions: Mapping[str, Sequence[str]]) -> None:
    """Контракт стыка прав (BL-014): значения — списки колонок, не CSV-строки.

    Нормализация «id,total» → [id, total] — зона границы адаптера
    (PgPermissionStore, fail-closed S05); валидатор свой CSV-парсер не заводит —
    строка в правах означает разъезд стыка, честный ValueError, не тихое сужение.
    """
    for table, columns in permissions.items():
        if isinstance(columns, str) or not isinstance(columns, Sequence):
            raise ValueError(
                f"права {table!r}: ожидается список колонок, получено "
                f"{type(columns).__name__} — CSV-строки валидатор не парсит (BL-014)"
            )


def _check_tables(
    audit: SqlAudit,
    permissions: Mapping[str, Sequence[str]],
    data_catalog: frozenset[str] | set[str],
    issues_denied: list[str],
    issues_hallucination: list[str],
) -> None:
    """ACL таблиц (план правило 4, ADR-005 §4): схема → служебные → мир → права.

    Схема вне public и служебные agent_* — denied (инфраструктура существует, но
    вне мира аналитики). Таблица вне каталога — hallucination («не существует в
    мире агента», retry оправдан); в каталоге, но вне прав — denied («существует,
    но вне прав», degrade без retry). Каталог — SoT существования: права, ссылающиеся
    на отсутствующую в мире таблицу, существующей её не делают.
    """
    for qualified in audit.tables:
        schema: str | None = None
        name = qualified
        if "." in qualified:
            schema, name = qualified.split(".", 1)
        if name.startswith(AGENT_TABLE_PREFIX):
            issues_denied.append(
                f"таблица {qualified} — служебная инфраструктура агента (agent_*), "
                "вне мира аналитики"
            )
            continue
        if schema is not None and schema != PUBLIC_SCHEMA:
            issues_denied.append(
                f"таблица {qualified} вне схемы {PUBLIC_SCHEMA} — "
                "мир агента ограничен публичной схемой"
            )
            continue
        if name not in data_catalog:
            issues_hallucination.append(
                f"таблица {name} не существует в мире агента (каталог agent_data_sources)"
            )
        elif name not in permissions:
            issues_denied.append(
                f"таблица {name} существует, но вне прав (agent_permissions) — degrade без retry"
            )


def _check_columns(
    audit: SqlAudit,
    permissions: Mapping[str, Sequence[str]],
    data_catalog: frozenset[str] | set[str],
    issues_denied: list[str],
    issues_hallucination: list[str],
) -> None:
    """ACL колонок (Ловушка 5): решают кандидаты-таблицы мира, остальные — нет.

    Whole-row-ссылки (``whole_row``, ревью S11 C1) проверяются как литеральная
    звезда: '*' на каждой таблице-кандидате. Кандидаты cte/sub пропускаются:
    права назначаются таблицам мира, тело производного источника уже проверено
    на своём скоупе. Таблицы вне каталога тоже не участвуют: несуществующая
    таблица уже дала hallucination на уровне таблиц — её колонка не превращается
    в ложное «вне прав» (degrade без retry). Неоднозначность вычисляется по
    разрешённым кандидатам (схемы БД у валидатора нет — «колонка существует»
    проксируется правами): ≥2 разрешённых → hallucination («квалифицируйте
    алиасом», retry оправдан), ровно 1 → resolved, 0 → denied.
    """
    for column in audit.columns:
        if column.whole_row:
            # ревью S11 C1: whole-row-ссылка — семантика литеральной звезды;
            # явный список колонок её не покрывает — '*' на КАЖДОЙ таблице-
            # кандидате (неоднозначные multiple-кандидаты fail-closed); CTE/sub
            # безопасны — их тело уже проверено на собственном скоупе
            for c in column.candidates:
                if c.kind != "table":
                    continue
                if "*" not in (permissions.get(c.name) or ()):
                    issues_denied.append(
                        f"вся строка {c.name} требует '*' в правах — явный список "
                        "колонок не покрывает whole-row-ссылку"
                    )
            continue
        table_candidates = [
            c for c in column.candidates if c.kind == "table" and c.name in data_catalog
        ]
        if not table_candidates:
            continue
        permitted_in = [
            c for c in table_candidates if _column_permitted(column.name, c.name, permissions)
        ]
        if len(permitted_in) >= 2:
            issues_hallucination.append(
                f"колонка {column.name} неоднозначна — разрешена таблицам "
                f"{', '.join(c.name for c in permitted_in)}; квалифицируйте алиасом"
            )
        elif not permitted_in:
            issues_denied.append(
                f"колонка {column.name} вне прав — таблицы запроса: "
                f"{', '.join(c.name for c in table_candidates)}"
            )


def _column_permitted(column: str, table: str, permissions: Mapping[str, Sequence[str]]) -> bool:
    """Право на колонку: явное вхождение в список или '*' (все колонки, R5 S05)."""
    permitted = permissions.get(table)
    if permitted is None:
        return False
    return "*" in permitted or column in permitted


def _check_stars(
    audit: SqlAudit,
    permissions: Mapping[str, Sequence[str]],
    issues_denied: list[str],
) -> None:
    """ACL литеральных звёзд проекции (ruling R2 ARCH-GATE, ADR-005 §4 п.2).

    Звезда НЕ отклоняется — разворачивается в разрешённые колонки (подстановку
    в текст выполняет services: ``star_expansion_columns`` + механика AST).
    Отказ — только звезда над таблицей без разрешённых колонок: литеральная
    '*' у БД вернула бы весь набор (ACL-байпас). Звёзды над CTE/подзапросами
    безопасны — тело проверено, звезда открывает лишь его собственную проекцию.
    """
    for sources in audit.star_sources:
        for source in sources:
            if source.kind != "table":
                continue
            if not permissions.get(source.name):
                issues_denied.append(
                    f"звезда '*' над {source.name} — нет разрешённых колонок, "
                    "литеральная звезда открыла бы весь набор"
                )


def star_expansion_columns(
    source_name: str,
    source_kind: str,
    permissions: Mapping[str, Sequence[str]],
) -> tuple[str, ...] | None:
    """Политика разворота звезды — парная к ``_check_stars`` (ruling R2).

    Возвращает колонки подстановки или None — звезда остаётся литералом:
    CTE/производный источник — None (его тело уже проверено); '*'-разрешённая
    таблица — None (права и так открывают все колонки — суб-звезда ``ref.*``
    безопасна); явный список — сам список (подстановка в текст закрывает
    ACL-байпас литеральной звезды). Таблица вне прав → None — ветка
    недостижима при ok-вердикте (``_check_stars`` уже дал denied).
    """
    if source_kind != "table":
        return None
    permitted = permissions.get(source_name)
    if permitted is None or "*" in permitted:
        return None
    return tuple(permitted)


def _check_skeleton(
    audit: SqlAudit,
    skeleton: Skeleton,
    join_plan: JoinPlan,
    skeleton_hash: str,
    issues: list[str],
) -> None:
    """«Скелет не изменён» (hw-01 §2.1, Task 3 плана): нарушения — hallucination.

    Модель, переписавшая структуру (тип джойна, лишняя/переставленная таблица,
    выкинутый mandatory_predicate), — не «вне прав», а выдумка: diagnose_error
    с ретраем оправдан. Корневая структура (FROM/джойны) фиксирована скелетом —
    CTE/UNION/LATERAL в корне assemble_sql не производит (модель наполняет только
    4 поля, ruling R1 S10). skeleton_hash — defense-in-depth: state (чекпоинт)
    обязан соответствовать VO, собранному из того же плана; расхождение —
    подмена чекпоинта, дальнейшая сверка лишена основания (ранний выход).
    mandatory_predicates сверяются нормализованным вхождением (единый источник
    нормализации — ``normalize_sql_fragment`` S10; алиасы SQL резолвятся
    services к реальным именам — скелет рендерит предикаты именами таблиц).
    """
    if skeleton_hash != skeleton.skeleton_hash:
        issues.append(
            "skeleton_hash в state не соответствует скелету плана — сверка структуры "
            "на подменённом чекпоинте невозможна"
        )
        return
    if audit.from_table != skeleton.from_table:
        issues.append(
            f"FROM-источник {audit.from_table or '«нет»'} не соответствует скелету "
            f"(ожидался {skeleton.from_table}) — структура запроса изменена"
        )
    if audit.joins != _expected_joins(join_plan):
        issues.append(
            "структура джойнов не соответствует плану (тип/таблица/пары ON/порядок) — "
            "скелет рендерит рёбра JoinPlan, модель их не пишет"
        )
    conjuncts = set(audit.where_conjuncts)
    for predicate in skeleton.mandatory_predicates:
        if normalize_sql_fragment(predicate) not in conjuncts:
            issues.append(f"обязательный предикат ребра отсутствует в WHERE: {predicate}")


def _expected_joins(plan: JoinPlan) -> tuple[SqlJoin, ...]:
    """Эталон джойнов из рёбер плана: порядок = порядок рёбер (скелет рендерит
    рёбра по цепочке); пары ON — реальными именами таблиц, каждая пара
    отсортирована (направление равенства не семантично) — тот же канон, что
    в извлечении IR (services._on_pairs)."""

    def edge_pairs(edge: JoinPlanEdge) -> tuple[tuple[str, str], ...]:
        return tuple(
            sorted(
                tuple(sorted((f"{edge.from_table}.{fc}", f"{edge.to_table}.{tc}")))
                for fc, tc in zip(edge.from_columns, edge.to_columns, strict=True)
            )
        )

    return tuple(
        SqlJoin(join_type=edge.join_type, table=edge.to_table, on_pairs=edge_pairs(edge))
        for edge in plan.edges
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

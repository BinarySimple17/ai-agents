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
# префиксом: инфраструктура существует, но вне мира аналитики (ADR-005 рубеж 3) —
# deny, degrade без retry (не «не существует»: она существует, просто не для SQL)
AGENT_TABLE_PREFIX = "agent_"

# Fan-out guard (feat-023, ruling R2 ARCH-GATE S32) — чувствительность агрегата
# над колонкой стороны «1» ребра плана с fanout_guard=true. Математика двойного
# счёта: orders JOIN order_items (1:n) размножает строку заказа N_i раз, и ключ
# в GROUP BY группу до зерна заказа коллапсирует, но N_i копий total в группе
# НЕ убирает — SUM = N_i×total всегда. Отсюда строгий v1′:
#   SUM, COUNT без DISTINCT — отклоняются ВСЕГДА (ключ GROUP BY не спасает;
#   SUM(DISTINCT …) — тоже: ruling R2 предписывает «SUM всегда», консервативно);
#   AVG — математически корректен при GROUP BY по ключу стороны «1» (среднее
#   N одинаковых = само значение), без ключа взвешен кратностью позиций —
#   отклоняется при отсутствии ЛЮБОЙ колонки составного ключа (код требует
#   присутствия ВСЕХ — _group_key_present);
#   COUNT(DISTINCT …), MIN/MAX, агрегаты стороны «n», агрегат в подзапросе,
#   план без guard-рёбер — проходят; COUNT(*) — вне правила (нет колонки
#   стороны «1» под агрегатом, ловушка 3 плана).
_FANOUT_REJECTED_ALWAYS = frozenset({"sum", "count"})
_FANOUT_CONDITIONAL = "avg"
# сентинелы чувствительности _fanout_sensitivity (ревью S32 M-4: один литерал —
# одна именованная константа)
_FANOUT_SENSITIVITY_ALWAYS = "always"
_FANOUT_SENSITIVITY_AVG = "avg"


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


class SqlAggregate(BaseModel):
    """Агрегат корневого скоупа в IR (feat-023, S32): функция + колонки аргумента.

    ``columns`` — колонки аргумента агрегата с кандидатами-источниками (та же
    резолюция, что у ``columns``; рекурсивно сквозь выражения — CASE/fen —
    ruling R2 ARCH-GATE S32; скоупы подзапросов не входят — «агрегат целиком
    внутри подзапроса» остаётся заботой своего скоупа и политикой fan-out не
    проверяется). ``distinct`` — аргумент дистинктен (COUNT(DISTINCT …)/
    SUM(DISTINCT …)). Пустой ``columns`` = агрегат без колонки (COUNT(*),
    ловушка 3 плана S32 — вне правила fan-out).
    """

    model_config = ConfigDict(frozen=True)

    func: str = Field(min_length=1)
    columns: tuple[SqlColumn, ...] = ()
    distinct: bool = False


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
    # BL-055: признак SELECT … INTO (детекция — services: arg «into» корня или
    # узел exp.Into в любом скоупе); решение принимает domain (_check_into)
    has_into: bool = False
    # feat-023 (S32, ruling R2 ARCH-GATE): агрегаты корневого скоупа — проекция/
    # HAVING/ORDER BY, включая оконные формы (SUM(…) OVER — узел агрегата под
    # exp.Window); и колонки GROUP BY корневого скоупа (только голые колонки —
    # позиционные/выражения не собираются, R4). Носитель политики fan-out guard
    # (_check_fanout); извлечение — services (ruling R4, прецедент has_into)
    aggregates: tuple[SqlAggregate, ...] = ()
    group_columns: tuple[SqlColumn, ...] = ()


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
    BL-055 (S42): deny SELECT … INTO — запись/DDL-класс (Postgres переписывает
    в CREATE TABLE AS), сигнал безопасности (denied, degrade без retry);
    детекция — services (``has_into``), решение — здесь.
    feat-023 (S32): fan-out guard — двойной счёт при агрегации поверх рёбра
    плана с fanout_guard=true (инвариант ADR-006 §4.6/ADR-004, prd §6 «0
    случаев»); детекция — services (``aggregates``/``group_columns``), решение
    — здесь (``_check_fanout``), исход — hallucination (retry оправдан).
    """
    if row_limit <= 0:
        raise ValueError(f"row_limit должен быть положительным, получен {row_limit}")
    _check_permissions_contract(permissions)
    issues_denied: list[str] = []
    issues_hallucination: list[str] = []
    _check_into(audit, issues_denied)
    _check_functions(audit, issues_hallucination)
    _check_tables(audit, permissions, data_catalog, issues_denied, issues_hallucination)
    _check_columns(audit, permissions, data_catalog, issues_denied, issues_hallucination)
    _check_stars(audit, permissions, issues_denied)
    _check_skeleton(audit, skeleton, join_plan, skeleton_hash, issues_hallucination)
    _check_fanout(audit, join_plan, issues_hallucination)
    enforced = _enforced_limit(audit.outer_limit, row_limit)
    return ValidationVerdict(
        outcome=_merge_outcome(issues_denied, issues_hallucination),
        issues=tuple(issues_denied + issues_hallucination),
        enforced_limit=enforced,
    )


def _check_into(audit: SqlAudit, issues_denied: list[str]) -> None:
    """Гейт «запись/DDL вне контура чтения» (BL-055): SELECT … INTO — denied.

    sqlglot парсит INTO как arg ``into`` у ``exp.Select`` — корень легально
    проходит гейты формы (корень ∈ Select/Union), отдельной DML-инструкции класс
    не образует. Детекция —
    services (ruling R4: механика AST там), политика ЗДЕСЬ: write-попытка —
    сигнал безопасности (не «несуществующий синтаксис», retry не оправдан) —
    denied, degrade без retry (канон ADR-005 §4: «существует, но вне прав →
    degrade»; прецедент классового denied — служебные схемы/agent_*).
    """
    if audit.has_into:
        issues_denied.append(
            "SELECT … INTO — запись/DDL-класс (Postgres переписывает в "
            "CREATE TABLE AS), вне контура чтения"
        )


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
    # BL-051(a) (решение владельца 2026-11-26, D-041): «все таблицы запроса ⊆
    # план». Вложенные тела (CTE, подзапросы FROM/WHERE, LATERAL) проверены
    # ACL'ом, но таблица вне плана во вложенном теле — та же выдумка, что и
    # лишний JOIN: модель читает данные, которых план не касается. audit.tables
    # собирает все relation-ссылки, включая тела CTE (ссылки на CTE-имена — не
    # таблицы, _collect_tables их отсекает); квалификация схемой срезается —
    # план несёт bare-имена. Residual после правила — условия связи между
    # таблицами самого плана во вложенных телах (pin «known residual» снят;
    # revisit — вариант (b) BL-051 при первом кейсе злоупотребления формой).
    plan_tables = {table.split(".", 1)[-1] for table in join_plan.tables}
    for table in audit.tables:
        if table.split(".", 1)[-1] not in plan_tables:
            issues.append(
                f"таблица {table} вне плана — разрешены только таблицы плана "
                "(«таблицы ⊆ план»; ретрай оправдан)"
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


def _check_fanout(
    audit: SqlAudit,
    join_plan: JoinPlan,
    issues: list[str],
) -> None:
    """Fan-out guard (feat-023, ruling R2 ARCH-GATE S32): агрегация поверх
    ребра плана с fanout_guard=true без предагрегации — двойной счёт, самый
    частый класс тихих ошибок Text-to-SQL (ADR-006 §4.6, ADR-004, prd §6
    «0 случаев»). Инвариант принят каноном без реализации (аудит 2026-11-23,
    E3a: fanout_guard читался адаптером, но ни один узел его не использовал).

    Исход — hallucination, НЕ denied: ошибка содержания SQL (выдумка модели
    поверх корректных прав), прецедент ``_check_skeleton`` — «не вне прав,
    а выдумка»; retry оправдан, подсказка детерминированная и ТОЛЬКО
    выразимая (корень фиксирован скелетом — «предагрегация в CTE» модели
    недоступна, ловушка 1 плана): подзапрос-предагрегация в проекции или
    GROUP BY по ключу (для AVG). Классификация чувствительности —
    ``_fanout_sensitivity``; мэтчинг ключа GROUP BY — ``_group_key_present``
    (ruling R4: имя == ключевой колонке И one_side ∈ кандидаты).

    Нарушенный инвариант ребра (fanout_one_side вне {from_table, to_table})
    невозможен для планов кода (``validate_join_plan`` проверяет на
    плане-времени), но чекпойнт-дрейф fail-closed: чувствительность
    невычислима — reject при любом чувствительном агрегате корня.
    """
    seen: set[str] = set()
    for edge in join_plan.edges:
        if not edge.fanout_guard:
            continue
        # ревью S32 M-6: bare-нормализация стороны «1» — та же идиома, что в
        # _check_skeleton; план несёт bare-имена, но квалифицированное имя
        # не должно делать guard молчаливым no-op («читается, но не работает»)
        one_side = edge.fanout_one_side.split(".", 1)[-1]
        key_columns = _fanout_edge_key(edge)
        if key_columns is None:
            # Битый инвариант ребра: сторона «1» не резолвится — матчить
            # агрегаты по fanout_one_side нельзя (молчаливый проход был бы
            # потерей проверки). Fail-closed: ЛЮБОЙ чувствительный агрегат
            # корня → reject (нечувствительные агрегаты не трогаются).
            for aggregate in audit.aggregates:
                if _fanout_sensitivity(aggregate) is None:
                    continue
                column = aggregate.columns[0].name if aggregate.columns else "*"
                message = (
                    f"агрегат {aggregate.func}({column}) при ребре "
                    f"{edge.rel_id} с fanout_guard: fanout_one_side не указывает на "
                    "таблицу ребра — план дефектен, чувствительность невычислима"
                )
                if message not in seen:
                    seen.add(message)
                    issues.append(message)
            continue
        for aggregate in audit.aggregates:
            one_side_column = next(
                (
                    column
                    for column in aggregate.columns
                    if any(c.kind == "table" and c.name == one_side for c in column.candidates)
                ),
                None,
            )
            if one_side_column is None:
                continue
            sensitivity = _fanout_sensitivity(aggregate)
            if sensitivity is None:
                continue
            if sensitivity == _FANOUT_SENSITIVITY_AVG and _group_key_present(
                audit.group_columns, one_side, key_columns
            ):
                continue  # AVG с GROUP BY по ключу стороны «1» — корректен (R2)
            if sensitivity == _FANOUT_SENSITIVITY_AVG:
                # ревью S32 I-1: подсказка обязана называть ВСЕ колонки
                # составного ключа (_group_key_present требует все) — частичная
                # подсказка даёт детерминированный dead-end retry (ловушка 6)
                key_list = ", ".join(f"{one_side}.{k}" for k in key_columns)
                message = (
                    f"агрегат avg({one_side_column.name}) над {one_side} — "
                    f"стороной «1» fan-out ребра {edge.rel_id} — взвешен кратностью "
                    "строк другой стороны: сгруппируй по ключу джойна "
                    f"{key_list} или посчитай агрегат "
                    "в подзапросе до джойна (предагрегация)"
                )
            else:
                message = (
                    f"агрегат {aggregate.func}({one_side_column.name}) над "
                    f"{one_side} — стороной «1» fan-out ребра "
                    f"{edge.rel_id} — размножает строки джойна (×N строк другой "
                    "стороны, GROUP BY по ключу не спасает): посчитай агрегат "
                    "в подзапросе до джойна (предагрегация); если вопрос про "
                    "отдельные значения — верни колонку и ключ без агрегации, "
                    "GROUP BY не нужен"
                )
            if message not in seen:
                seen.add(message)
                issues.append(message)


def _fanout_edge_key(edge: JoinPlanEdge) -> tuple[str, ...] | None:
    """Ключ джойна стороны «1» (fanout_one_side); None — инвариант R3 нарушен.

    Ключи следуют своей таблице (ruling Q2 S09): from_columns принадлежат
    from_table, to_columns — to_table; сторона «1» — одна из таблиц ребра.
    """
    # ревью S32 M-6: bare-сравнение (идиома _check_skeleton) — квалификация
    # схемой не должна терять сторону «1»
    one_side = edge.fanout_one_side.split(".", 1)[-1]
    if one_side == edge.from_table:
        return edge.from_columns
    if one_side == edge.to_table:
        return edge.to_columns
    return None


def _fanout_sensitivity(aggregate: SqlAggregate) -> str | None:
    """Чувствительность агрегата над стороной «1» (ruling R2, строгий v1′):
    _FANOUT_SENSITIVITY_ALWAYS (SUM; COUNT без DISTINCT) |
    _FANOUT_SENSITIVITY_AVG (ключ в GROUP BY спасает) |
    None (MIN/MAX/COUNT(DISTINCT)/без колонок). COUNT(*) — колонок нет —
    None (ловушка 3: без колонки стороны «1» под агрегатом кратность строк
    не переносится на агрегат)."""
    if aggregate.func == _FANOUT_CONDITIONAL:
        return _FANOUT_SENSITIVITY_AVG
    if aggregate.func in _FANOUT_REJECTED_ALWAYS and not (
        aggregate.func == "count" and aggregate.distinct
    ):
        return _FANOUT_SENSITIVITY_ALWAYS
    return None


def _group_key_present(
    group_columns: tuple[SqlColumn, ...], one_side: str, key_columns: tuple[str, ...]
) -> bool:
    """Все колонки составного ключа стороны «1» присутствуют в GROUP BY
    (ruling R4): имя == ключевой колонке И one_side ∈ кандидаты-таблицы.
    Unqualified-ссылка с обоими источниками в кандидатах матчится lenient —
    обход не открывает: неоднозначность уронит PostgreSQL, а 'always'-класс
    от мэтчинга не зависит. Позиционные/выражения GROUP BY не собираются
    (ruling R4) — ключ «не найден» → консервативный reject (retry с
    подсказкой «группируй по ключу явно» — выразимый фикс)."""
    for key in key_columns:
        if not any(
            column.name == key
            and any(c.kind == "table" and c.name == one_side for c in column.candidates)
            for column in group_columns
        ):
            return False
    return True


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

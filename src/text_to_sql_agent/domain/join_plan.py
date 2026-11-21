"""JoinPlan — доменная сущность плана джойнов + валидация кодом (feat-009, S00 стык 1).

Инвариант hw-01 §2.1 «LLM наполняет, код решает» на узле plan_joins: маршрут и
валидация — код; модель только ранжирует кандидатов путей, построенных кодом по
снапшоту активных рёбер (ADR-007 §4: «план проверяет код»). rel_id — единственный
источник джойнов: валидатор отсекает рёбра вне активного снапшота («выдуманный
джойн» — Ловушка-1 S09) до ретраев; галлюцинация связи не доходит до SQL.

Без IO, без LLM (границы domain): входы валидатора — чистые данные, выход —
список нарушений (текст годится для ретрая M1: «попытка 2 — та же модель + текст
ошибки», D-016). Пустой список = план валиден.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# JoinPlan ≤ 4 таблиц (hw-01 §3.4: план джойна — верхняя граница сложности вопроса;
# больше — задача декомпозиции plan_query, не джойн-план).
MAX_JOIN_PLAN_TABLES = 4


class JoinPlanEdge(BaseModel):
    """Одно ребро плана — из активного ребра реестра (ADR-006 §4.1).

    Направление from/to — порядок обхода пути графа (может быть развёрнуто
    против left/right реестра — джойн физически симметричен); ключи всегда
    следуют своей таблице: from_columns принадлежат from_table. join_type —
    бизнес-дефолт реестра (inner | left), модель его не выбирает.
    """

    model_config = ConfigDict(frozen=True)

    rel_id: str = Field(min_length=1)  # адресация в плане/валидаторе/аудите
    from_table: str = Field(min_length=1)
    to_table: str = Field(min_length=1)
    from_columns: tuple[str, ...] = Field(min_length=1)  # составные ключи
    to_columns: tuple[str, ...] = Field(min_length=1)
    join_operator: str = "="  # из ребра реестра
    join_type: str = Field(min_length=1)  # inner | left — из ребра реестра


class JoinPlan(BaseModel):
    """План джойнов ≤ 4 таблиц по активным рёбрам снапшота (behavior feat-009).

    `graph_version` — пин ревизии снапшота, по которому построен план (ADR-006
    §4.3: сверяется с версией снапшота графовой ноги — Ловушка-3 S09).
    `assumptions` — допущения из usage_notes рёбер пути (+temporal-замечания):
    входят в ответ пользователю (атрибуция first-touch — то, чего нет у готовых
    Text-to-SQL; раскрытие — feat-015). `mandatory_predicates` — skeleton-якоря
    для build_skeleton (S10): rel_id → обязательные предикаты ребра
    (MANDT/lang/deleted=false — ADR-006 §4.6 инвариант plan_joins/validate_sql).
    """

    model_config = ConfigDict(frozen=True)

    tables: list[str] = Field(min_length=2, max_length=MAX_JOIN_PLAN_TABLES)
    edges: list[JoinPlanEdge] = Field(min_length=1)
    graph_version: int = Field(ge=0)
    assumptions: list[str] = Field(default_factory=list)
    mandatory_predicates: dict[str, dict[str, Any]] = Field(default_factory=dict)


def validate_join_plan(
    plan: JoinPlan,
    *,
    permissions: Mapping[str, Sequence[str]],
    active_rel_ids: frozenset[str],
) -> list[str]:
    """Валидация плана кодом ДО ретраев (Task 1; deny-by-default).

    Правила (план S09): таблицы ⊆ scope прав, rel_id рёбер ⊆ активный снапшот
    реестра, ≤ MAX_JOIN_PLAN_TABLES таблиц, связность цепочки, ACL колонок
    соединения (ребро без доступа к ключам недоступно целиком — ревью-03 S2).
    Проекции/фильтры SELECT — не здесь (validate_sql — S11/V4).

    `permissions` — S00 контракт 3: `{table: [columns]}`, `"*"` = все колонки;
    таблицы нет в map — она не существует в мире агента. Строковое значение
    (CSV «a,b») — нарушение контракта: подстрочная проверка дала бы ложный
    доступ к ключу — функция бросает `ValueError` (fail-closed); нормализация
    строки — граница сервиса (`_normalize_permissions`). Возвращает список
    нарушений (человекочитаемых, для текста ретрая); пусто = валиден.
    """

    issues: list[str] = []

    # Контракт прав: значения — список колонок, НЕ строка. str — тоже
    # Sequence[str], подстрочная проверка `"id" in "customer_id,name"` дала бы
    # ложный доступ к ключу (ревью I4): fail-closed, нормализация — граница
    # сервиса (`_normalize_permissions`).
    for table, permitted in permissions.items():
        if isinstance(permitted, str):
            raise ValueError(
                f"контракт прав нарушен для {table}: колонки — список строк "
                f"(или '*'), получена строка {permitted!r} — строку «a,b» "
                "нормализуй на границе (agent_permissions.columns — CSV-текст)"
            )

    # --- Структурные правила ---
    if len(plan.tables) > MAX_JOIN_PLAN_TABLES:
        issues.append(
            f"план соединяет {len(plan.tables)} таблиц — лимит {MAX_JOIN_PLAN_TABLES}; "
            "такой вопрос — задача декомпозиции, а не джойн-план"
        )
    if len(set(plan.tables)) != len(plan.tables):
        dupes = sorted({t for t in plan.tables if plan.tables.count(t) > 1})
        issues.append(
            f"дубликаты таблиц в плане {dupes}: self-join требует алиасов — "
            "на этой стадии не поддерживается"
        )

    edge_tables: set[str] = set()
    for edge in plan.edges:
        edge_tables.update((edge.from_table, edge.to_table))
        if edge.rel_id not in active_rel_ids:
            issues.append(
                f"ребро {edge.rel_id} ({edge.from_table}→{edge.to_table}) — выдуманный "
                "джойн: его нет среди активных рёбер снапшота реестра"
            )
    dangling = sorted(set(plan.tables) - edge_tables)
    if dangling:
        issues.append(f"таблицы {dangling} заявлены в плане, но не задействованы ни в одном ребре")
    phantom = sorted(edge_tables - set(plan.tables))
    if phantom:
        issues.append(f"рёбра ссылаются на таблицы вне списка плана: {phantom}")

    # Связность: все таблицы плана — одна компонента (обход рёбер).
    if plan.tables and not _connected(plan.tables, plan.edges):
        issues.append("план не связный: часть рёбер не соединяет таблицы в одну цепочку")

    # --- Права (deny-by-default, S00 контракт 3) ---
    for table in plan.tables:
        if table not in permissions:
            issues.append(
                f"таблица {table} вне прав пользователя — она не существует в мире агента"
            )

    # --- ACL колонок соединения: ключ джойна ⊆ permitted columns обеих таблиц ---
    for edge in plan.edges:
        for table, columns in (
            (edge.from_table, edge.from_columns),
            (edge.to_table, edge.to_columns),
        ):
            permitted = permissions.get(table)
            if permitted is None:
                continue  # отсутствие таблицы уже зафиксировано выше
            missing = [c for c in columns if "*" not in permitted and c not in permitted]
            if missing:
                issues.append(
                    f"ребро {edge.rel_id}: ключ джойна {missing} на {table} вне прав колонок — "
                    "ребро без доступа к ключам недоступно целиком"
                )

    return issues


def _connected(tables: Sequence[str], edges: Sequence[JoinPlanEdge]) -> bool:
    """Все таблицы плана достижимы по рёбрам из первой (цепочка джойнов)."""

    adjacency: dict[str, set[str]] = {table: set() for table in tables}
    for edge in edges:
        if edge.from_table in adjacency and edge.to_table in adjacency:
            adjacency[edge.from_table].add(edge.to_table)
            adjacency[edge.to_table].add(edge.from_table)
    seen = {tables[0]}
    frontier = [tables[0]]
    while frontier:
        current = frontier.pop()
        for neighbour in adjacency[current]:
            if neighbour not in seen:
                seen.add(neighbour)
                frontier.append(neighbour)
    return seen == set(tables)

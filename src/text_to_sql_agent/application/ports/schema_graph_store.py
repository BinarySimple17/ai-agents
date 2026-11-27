"""Порт `SchemaGraphStore` — контракт графовой памяти связей (S00 стык; ADR-003 v2.1).

Граф — **производный снапшот реестра** `agent_relationships` (ADR-006 v1.3:
реестр — единственный источник рёбер; FK каталога — генератор кандидатов в
очередь владельцу, НЕ ребро). Хранилище (Neo4j) держит **только топологию** —
пары таблиц + `rel_id` (ADR-003); вся семантика (meaning, join_type,
usage_notes) читается из PostgreSQL по rel_id через `path_semantics` — в граф
не дублируется (иначе дубль дрейфует молча: reconciliation меряет только
топологию). Расхождение граф↔реестр ловят синк по счётчику `settings_version`
и reconciliation-тест «активные строки реестра = рёбра графа» (ADR-006 §4.3).

Права — deny-by-default (S00 контракт 3: `state.permissions` — единственный
источник для подграфа): `allowed_tables` передаёт caller (resolve_access →
state.permissions), пути через недоступные таблицы не возвращаются —
недоступное не существует в мире агента. Граф не ходит в настройки прав сам:
RBAC-источник один на прогон, пин-снапшот прав как у graph_version.

LLM пути не строит: `find_join_paths` — детерминированный код по снапшоту
(инвариант hw-01 §2.1); ранжирование кандидатов моделью — узел plan_joins
(S09, там модель наполняет, код решает).

Хранилище заменяемо за этим портом (D-015: Neo4j ↔ BFS по adjacency —
критерий ADR-003 §4); узлы FSM не меняются.

Ошибки стыка типизированы (прецедент `VectorStoreError`): `SchemaGraphError` —
сбой хранилища (Neo4j/PG); нарушение контракта вызова — ValueError.

DTO стыка — pydantic и живут здесь (прецедент: `vector_store.py`).
"""

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from text_to_sql_agent.domain.entities import RunContext


class SchemaGraphError(Exception):
    """Сбой графовой памяти (Neo4j/PG): человекочитаемое сообщение, не traceback."""


class JoinEdge(BaseModel):
    """Ребро пути в направлении обхода (from → to могут быть развёрнуты против
    направления связи реестра — джойн физически симметричен)."""

    model_config = ConfigDict(frozen=True)

    rel_id: str = Field(min_length=1)  # адресация в плане/валидаторе/аудите (ADR-006 §4.1)
    from_table: str = Field(min_length=1)
    to_table: str = Field(min_length=1)
    weight: float = Field(gt=0)  # вес реестра — ранжирование путей (финальное — S09)


class JoinPath(BaseModel):
    """Путь джойнов по активным рёбрам снапшота: цепочка рёбер + суммарный вес."""

    model_config = ConfigDict(frozen=True)

    edges: list[JoinEdge] = Field(min_length=1)
    total_weight: float = Field(gt=0)

    @property
    def tables(self) -> list[str]:
        """Таблицы пути по порядку обхода: from первой ноги + to каждой следующей."""
        return [self.edges[0].from_table, *(edge.to_table for edge in self.edges)]


class EdgeSemantics(BaseModel):
    """Семантика ребра из реестра (строка `agent_relationships`) — по rel_id.

    Ключевые поля (left/right_table/columns, join_operator, mandatory_predicates,
    cardinality, fanout_guard, temporal) — аддитивное расширение S09 (ruling Q2
    ARCH-GATE): JoinPlan несёт тип/направление/ключи джойна из ребра (Task 1),
    mandatory_predicates — skeleton-якоря S10, fanout_guard — инвариант
    plan_joins/validate_sql (ADR-006 §4.6). Значения с дефолтами — чтение
    порта не ломает потребителей S07.
    """

    model_config = ConfigDict(frozen=True)

    rel_id: str = Field(min_length=1)
    join_type: str = Field(min_length=1)  # inner | left — бизнес-дефолт, модель не решает
    meaning: str = Field(min_length=1)  # что связь значит бизнесу
    usage_notes: str | None = None  # ловушки; входят в ответ как допущения (feat-015)
    intent_tags: list[str] = Field(default_factory=list)  # выбор между параллельными рёбрами
    # --- С09 (ruling Q2): ключи/тип джойна и skeleton-якоря из того же ребра ---
    left_table: str = ""
    left_columns: tuple[str, ...] = ()
    right_table: str = ""
    right_columns: tuple[str, ...] = ()
    join_operator: str = "="
    mandatory_predicates: dict[str, Any] = Field(default_factory=dict)  # skeleton-якоря S10
    cardinality: str = ""  # 1:1 | 1:n | n:1 | n:m
    fanout_guard: bool = False  # агрегация поверх ребра требует предагрегации (ADR-004)
    temporal: dict[str, Any] | None = None  # «на дату заказа» vs «на сегодня»


class SyncReport(BaseModel):
    """Отчёт синка: размер загруженного снапшота + его ревизия (make sync-graph)."""

    model_config = ConfigDict(frozen=True)

    nodes: int = Field(ge=0)
    edges: int = Field(ge=0)
    graph_version: int = Field(ge=0)


@runtime_checkable
class SchemaGraphStore(Protocol):
    """Графовая память связей: синк снапшота реестра + поиск путей джойнов.

    `sync` — детерминированный ETL «активные строки реестра → рёбра графа»
    (ADR-006 §4.3), идемпотентен: повторный запуск даёт тот же граф.
    `graph_version` — ревизия снапшота (0 = снапшот ещё не загружен); пинится
    на прогон в state/кэш/аудит (resolve_access — S05/V4).
    `find_join_paths` — k взвешенных путей между парой таблиц по активным
    рёбрам; фильтр прав — `allowed_tables` (deny-by-default: путь, задевающий
    недоступную таблицу, не существует). Пары без связного пути → пустой
    список — джойн не выдумывается (behavior feat-007).
    `path_semantics` — семантика рёбер пути из реестра PG (вьюха active) по
    rel_id, в порядке входных edge_ids.
    """

    def sync(self) -> SyncReport: ...

    def graph_version(self) -> int: ...

    def find_join_paths(
        self,
        from_table: str,
        to_table: str,
        *,
        ctx: RunContext,
        allowed_tables: frozenset[str],
        k: int,
    ) -> list[JoinPath]: ...

    def path_semantics(self, edge_ids: list[str], *, ctx: RunContext) -> list[EdgeSemantics]: ...

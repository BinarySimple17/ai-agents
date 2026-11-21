"""Адаптер `SchemaGraphStore` на Neo4j — графовая память связей (S07, feat-007).

Два канала (ADR-003 v2.1: PG — истина, Neo4j — производный снапшот):
- PostgreSQL — DSN роли `agent_planner` (тот же пул «retrieval, настройки», c4-l2
  v1.8; гранты миграции 0002): вьюха `agent_relationships_active` для синка и
  `path_semantics` (ruling R1 canon-check: реестр — единственный источник рёбер,
  FK каталога — генератор кандидатов владельцу, НЕ ребро) и счётчик
  `settings_version.name='relationships'` для `graph_version`.
- Neo4j (bolt, пароль из Settings) — ТОЛЬКО топология: вершины `(:Table {name})`,
  рёбра `(:Table)-[:JOINS {rel_id, weight}]->(:Table)`, маркер снапшота
  `(:GraphMeta {graph_version})`. Направление ребра — направление связи реестра
  (left_table → right_table); `join_type`/`meaning`/`usage_notes` в граф НЕ
  дублируются (ruling R2, ADR-003:89 «пары узлов + edge_id»): они читаются из
  PG по rel_id (`path_semantics`), иначе дубль дрейфует молча — reconciliation
  меряет только топологию.

Синк — ПОЛНАЯ перезагрузка снапшота (`MATCH (n) DETACH DELETE` + загрузка),
идемпотентен. Почему без диффа: топология демо-масштаба — десятки рёбер
(ADR-003 §3), дифф добавил бы код, в котором заводится расхождение, без
выигрыша; контракт — «детерминированный ETL» (ADR-006 §4.3), потеря графа не
потеря данных (перестраивается из реестра).

Права — deny-by-default (S00 контракт 3): `allowed_tables` передаёт caller
(resolve_access → state.permissions — единственный источник для подграфа);
путь, задевающий таблицу вне набора, не возвращается. Пустой набор = пустой
результат (не ошибка): незнакомому пользователю пути не существуют.

Порядок путей — убывание суммарного `weight` реестра (вес — субъективная
предпочтительность владельца), тай-брейк: меньше хопов, затем лексикографика
rel_id — детерминизм между прогонами. Финальное ранжирование кандидатов —
моделью в plan_joins (S09); здесь LLM нет (инвариант hw-01 §2.1).

Сбой хранилища — типизированный `SchemaGraphError` (прецедент
VectorStoreError), не сырой psycopg/neo4j-эксепшен.
"""

from typing import Any

import psycopg
from psycopg.rows import dict_row

from text_to_sql_agent.application.ports.schema_graph_store import (
    EdgeSemantics,
    JoinEdge,
    JoinPath,
    SchemaGraphError,
    SyncReport,
)
from text_to_sql_agent.application.ports.sql_executor import CONNECT_TIMEOUT_S
from text_to_sql_agent.domain.entities import RunContext

_ERROR_MESSAGE_MAX_CHARS = 300  # сообщения стыка — не простыня psycopg/neo4j-хинтов

# Путь длиннее 3 хопов (> 4 таблиц) не может попасть в JoinPlan (лимит feat-009
# «не более 4 таблиц», hw-01 §4.2) — искать глубже бессмысленно и дороже.
MAX_JOIN_PATH_HOPS = 3

# Счётчик-триггер перестройки снапшота (ADR-006 §4.3; триггеры миграции 0001).
RELATIONSHIPS_COUNTER_NAME = "relationships"

# Активные строки реестра — единственный источник рёбер снапшота (ruling R1).
_ACTIVE_EDGES_SQL = """
SELECT edge_id, left_table, right_table, weight
FROM agent_relationships_active
"""

_COUNTER_SQL = """
SELECT version FROM settings_version WHERE name = %s
"""

# Семантика рёбер пути — из реестра по rel_id (вьюха active: путь построен по
# снапшоту активных рёбер, у не-active строк семантики в мире агента нет).
_SEMANTICS_SQL = """
SELECT edge_id, join_type, meaning, usage_notes, intent_tags
FROM agent_relationships_active
WHERE edge_id = ANY(%s)
"""


def _shorten(message: str) -> str:
    message = " ".join(message.split())
    if len(message) <= _ERROR_MESSAGE_MAX_CHARS:
        return message
    return message[: _ERROR_MESSAGE_MAX_CHARS - 1] + "…"


def _rank_paths(paths: list[JoinPath], k: int) -> list[JoinPath]:
    """Детерминированное ранжирование: убывание веса, затем меньше хопов,
    затем лексикографика rel_id (чистая функция — юнит-тесты без Neo4j)."""
    ordered = sorted(
        paths,
        key=lambda p: (-p.total_weight, len(p.edges), tuple(e.rel_id for e in p.edges)),
    )
    return ordered[:k]


class Neo4jGraphStore:
    """Реализация `SchemaGraphStore`: синк топологии + k взвешенных путей + семантика."""

    def __init__(
        self, neo4j_uri: str, neo4j_user: str, neo4j_password: str, planner_dsn: str
    ) -> None:
        # Ленивое подключение драйвера (без сети в конструкторе); пароль — строка
        # из Settings.get_secret_value() на границе composition, здесь не хранить
        # его в repr-безопасной форме некому — объект не логируется.
        from neo4j import GraphDatabase  # локальный импорт: тяжёлый SDK — только у адаптера

        self._driver = GraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_password))
        self._planner_dsn = planner_dsn

    def close(self) -> None:
        self._driver.close()

    def __enter__(self) -> "Neo4jGraphStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --- Синк: детерминированный ETL «активные строки реестра → рёбра графа» ---

    def sync(self) -> SyncReport:
        try:
            rows, graph_version = self._read_registry()
        except psycopg.Error as exc:
            raise SchemaGraphError(f"реестр связей недоступен: {_shorten(str(exc))}") from exc
        edges = [
            {
                "rel_id": row["edge_id"],
                "from_table": row["left_table"],
                "to_table": row["right_table"],
                "weight": float(row["weight"]),
            }
            for row in rows
        ]
        tables = sorted(
            {edge["from_table"] for edge in edges} | {edge["to_table"] for edge in edges}
        )
        try:
            self._load_snapshot(tables, edges, graph_version)
        except Exception as exc:
            # neo4j.exceptions в v5 не имеют общего предка с Neo4jError — ловим
            # широкий Exception вокруг execute_write (специфичный SDK-транспорт
            # уже обёрнут в ServiceUnavailable/AuthError)
            raise SchemaGraphError(
                f"загрузка снапшота в Neo4j не удалась: {_shorten(str(exc))}"
            ) from exc
        return SyncReport(nodes=len(tables), edges=len(edges), graph_version=graph_version)

    def _read_registry(self) -> tuple[list[dict[str, Any]], int]:
        """Активные строки реестра + счётчик 'relationships' (agent_planner, вьюха)."""
        with psycopg.connect(
            self._planner_dsn,
            row_factory=dict_row,
            connect_timeout=CONNECT_TIMEOUT_S,
        ) as conn:
            rows = conn.execute(_ACTIVE_EDGES_SQL).fetchall()
            counter = conn.execute(_COUNTER_SQL, (RELATIONSHIPS_COUNTER_NAME,)).fetchone()
        return rows, int(counter["version"]) if counter else 0

    def _load_snapshot(
        self, tables: list[str], edges: list[dict[str, Any]], graph_version: int
    ) -> None:
        # Полная перезагрузка одной транзакцией: wipe + вершины + рёбра + маркер
        # ревизии (см. докстринг модуля — почему без диффа). Каждый Result
        # потребляется (.consume()) — незавершённые стримы Neo4j 5 роняют
        # транзакцию с "open result" на закрытии.
        def _reload(tx: Any) -> None:
            tx.run("MATCH (n) DETACH DELETE n").consume()
            tx.run("UNWIND $tables AS t MERGE (:Table {name: t})", tables=tables).consume()
            tx.run(
                "UNWIND $edges AS e "
                "MATCH (a:Table {name: e.from_table}) "
                "MATCH (b:Table {name: e.to_table}) "
                "CREATE (a)-[:JOINS {rel_id: e.rel_id, weight: e.weight}]->(b)",
                edges=edges,
            ).consume()
            tx.run(
                "CREATE (:GraphMeta {graph_version: $gv, synced_at: datetime()})",
                gv=graph_version,
            ).consume()

        with self._driver.session() as session:
            session.execute_write(_reload)

    # --- Ревизия снапшота ---

    def graph_version(self) -> int:
        try:
            records, _, _ = self._driver.execute_query(
                "MATCH (m:GraphMeta) RETURN m.graph_version AS gv LIMIT 1",
                database_="neo4j",
            )
        except Exception as exc:
            raise SchemaGraphError(f"Neo4j недоступен: {_shorten(str(exc))}") from exc
        return int(records[0]["gv"]) if records else 0

    # --- Поиск путей джойнов (детерминированный код, без LLM) ---

    def find_join_paths(
        self,
        from_table: str,
        to_table: str,
        *,
        ctx: RunContext,
        allowed_tables: frozenset[str],
        k: int,
    ) -> list[JoinPath]:
        if from_table == to_table:
            raise ValueError(f"from_table == to_table ({from_table!r}) — путь джойнов не ищется")
        if k <= 0:
            raise ValueError(f"k должен быть положительным, получен {k}")
        # Deny-by-default: не-строковые/пустые наборы → пустой результат
        # (недоступное не существует в мире агента), а не ошибка вызова.
        if not allowed_tables:
            return []
        # Граница квантификатора — параметр не поддерживается (CypherSyntaxError
        # «Parameter maps cannot be used in MATCH patterns», проверено на
        # neo4j:5.26.0) — инлайн именованной константы модуля (не пользовательский
        # ввод, инъекции исключены).
        query = (
            f"MATCH p = (a:Table {{name: $from_table}})-[:JOINS*1..{MAX_JOIN_PATH_HOPS}]-"
            "(b:Table {name: $to_table}) "
            "WHERE all(n IN nodes(p) WHERE n.name IN $allowed) "
            # Простые пути (без повторов вершин): вхождение вершины в собственный префикс
            "AND all(idx IN range(0, size(nodes(p)) - 1) "
            "WHERE NOT nodes(p)[idx] IN nodes(p)[0..idx]) "
            "RETURN nodes(p) AS nodes, relationships(p) AS rels"
        )
        try:
            records, _, _ = self._driver.execute_query(
                query,
                from_table=from_table,
                to_table=to_table,
                allowed=list(allowed_tables),
                database_="neo4j",
            )
        except Exception as exc:
            raise SchemaGraphError(f"поиск путей в Neo4j не удался: {_shorten(str(exc))}") from exc
        paths: list[JoinPath] = []
        for record in records:
            path = self._build_path(list(record["nodes"]), list(record["rels"]))
            if path is not None:
                paths.append(path)
        return _rank_paths(paths, k)

    def _build_path(self, nodes: list[Any], rels: list[Any]) -> JoinPath | None:
        """Рёбра пути в порядке обхода; обход против направления связи
        разворачивает from/to (джойн физически симметричен)."""
        edges: list[JoinEdge] = []
        current = nodes[0]["name"]
        for rel in rels:
            start, end = rel.start_node["name"], rel.end_node["name"]
            if current not in (start, end):
                return None  # несвязный сегмент — не должен возникать, fail-closed
            next_table = end if current == start else start
            edges.append(
                JoinEdge(
                    rel_id=rel["rel_id"],
                    from_table=current,
                    to_table=next_table,
                    weight=float(rel["weight"]),
                )
            )
            current = next_table
        return JoinPath(edges=edges, total_weight=sum(edge.weight for edge in edges))

    # --- Семантика рёбер пути (из реестра PG, не из графа — ruling R2) ---

    def path_semantics(self, edge_ids: list[str], *, ctx: RunContext) -> list[EdgeSemantics]:
        if not edge_ids:
            return []
        try:
            with psycopg.connect(
                self._planner_dsn,
                row_factory=dict_row,
                connect_timeout=CONNECT_TIMEOUT_S,
            ) as conn:
                rows = conn.execute(_SEMANTICS_SQL, (list(edge_ids),)).fetchall()
        except psycopg.Error as exc:
            raise SchemaGraphError(f"семантика рёбер недоступна: {_shorten(str(exc))}") from exc
        by_rel_id = {row["edge_id"]: row for row in rows}
        missing = [rel_id for rel_id in edge_ids if rel_id not in by_rel_id]
        if missing:
            # Дрейф граф↔реестр: ребро есть в снапшоте, но не в active-вьюхе
            # (устарело после синка). Честный сигнал, не выдуманная семантика.
            raise SchemaGraphError(
                "рёбра отсутствуют в active-реестре (снапшот устарел — "
                f"перезапустите make sync-graph): {missing}"
            )
        return [
            EdgeSemantics(
                rel_id=row["edge_id"],
                join_type=row["join_type"],
                meaning=row["meaning"],
                usage_notes=row["usage_notes"],
                intent_tags=list(row["intent_tags"] or []),
            )
            for row in (by_rel_id[rel_id] for rel_id in edge_ids)
        ]

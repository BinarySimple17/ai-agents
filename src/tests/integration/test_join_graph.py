"""Integration-тесты графовой памяти связей (feat-007, S07) — верификация фичи.

behavior feat-007: для пары таблиц узел возвращает путь по реальным рёбрам или
null (пути нет — не выдумываем джойн); для каждого ребра пути — семантика
(join_type, meaning, usage_notes) из настроек. Reconciliation: активные строки
реестра = рёбра графа (ADR-006 §4.3, ruling R1 — без FK-слагаемого: реестр —
единственный источник рёбер); правка настроек → graph_version растёт → синк
меняет граф. Фильтр прав deny-by-default: пути через недоступные таблицы не
возвращаются (S00 контракт 3). Скипаются без инфры с понятной причиной (conftest).

Ловушка-1 сессии: пустой граф молча роняет тесты путей — модульный fixture
делает `sync()` до ассертов. Реестр в тестах не мутируется безвозвратно:
(e) правит weight демо-ребра и восстанавливает в finally (счётчик
settings_version монотонен по дизайну — инвариантов на его значение нет).
"""

import psycopg
import pytest
from psycopg.rows import dict_row

from text_to_sql_agent.application.ports.schema_graph_store import SchemaGraphError
from text_to_sql_agent.config.settings import load_settings
from text_to_sql_agent.domain.entities import RunContext
from text_to_sql_agent.infrastructure.graph.neo4j_graph_store import Neo4jGraphStore

# Демо-карты прав (миграция 0003): resolve_access (S05/V4) отдаёт их в
# state.permissions — здесь подставляются напрямую как источник подграфа.
ALLOWED_LOGIST = frozenset({"customers", "orders", "order_items", "campaigns", "products"})
ALLOWED_ANALYST = frozenset({"orders", "order_items", "products"})
ALLOWED_HR = frozenset({"employees", "bonuses"})

CTX = RunContext(trace_id="it-join-graph", user_id="u_logist")

_DEMO_EDGE = "e_campaigns_orders"
_DEMO_WEIGHT = 1.2  # сид 0003 — исходное значение для восстановления после (e)


@pytest.fixture(scope="module")
def settings(postgres_available, neo4j_available):
    """Доступность инфры — ДО load_settings(): без Docker модуль скипается
    с понятной причиной (conftest), а не падает на sync() (ревью S07 Critical-1;
    AGENTS: ./init.sh остаётся зелёным без Docker)."""
    return load_settings()


@pytest.fixture(scope="module")
def store(settings):
    """Адаптер на живой паре Neo4j + PG; граф синхронизирован до ассертов."""
    graph_store = Neo4jGraphStore(
        settings.neo4j_uri,
        settings.neo4j_user,
        settings.neo4j_password.get_secret_value(),
        planner_dsn=settings.agent_planner_dsn,
    )
    graph_store.sync()  # Ловушка-1: пустой граф молча роняет тесты путей
    yield graph_store
    graph_store.close()


@pytest.fixture(scope="module")
def neo4j_driver(settings):
    """Прямой драйвер для white-box чтения графа (reconciliation) — не через
    приватные поля адаптера."""
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
    )
    yield driver
    driver.close()


def _pg_service_dsn(settings) -> str:
    return settings.postgres_dsn.get_secret_value()


# --- (a) путь между связанными таблицами по реальным рёбрам ---


def test_path_between_linked_tables(store):
    paths = store.find_join_paths(
        "customers", "campaigns", ctx=CTX, allowed_tables=ALLOWED_LOGIST, k=5
    )
    assert len(paths) == 1  # мультиграфа между парой нет — путь один
    path = paths[0]
    assert path.tables == ["customers", "orders", "campaigns"]
    assert [edge.rel_id for edge in path.edges] == ["e_customers_orders", "e_campaigns_orders"]
    # вес пути = сумма весов реестра (1.0 + 1.2)
    assert path.total_weight == pytest.approx(2.2)


def test_path_respects_registry_direction(store):
    """Ребро реестра campaigns→orders в обходе orders→campaigns развёрнуто,
    но rel_id сохранён (валидатор (S11) сверяет джойны по edge_id)."""
    paths = store.find_join_paths(
        "orders", "campaigns", ctx=CTX, allowed_tables=ALLOWED_LOGIST, k=5
    )
    assert len(paths) == 1
    edge = paths[0].edges[0]
    assert edge.rel_id == "e_campaigns_orders"
    assert (edge.from_table, edge.to_table) == ("orders", "campaigns")


# --- (b) пути нет → пусто, джойн не выдумывается ---


def test_no_path_between_unlinked_tables(store):
    """customers и employees в разных доменах (нет активных рёбер) — пути нет."""
    assert (
        store.find_join_paths("customers", "employees", ctx=CTX, allowed_tables=ALLOWED_LOGIST, k=5)
        == []
    )


def test_no_path_to_isolated_table(store):
    assert (
        store.find_join_paths("customers", "bonuses", ctx=CTX, allowed_tables=ALLOWED_LOGIST, k=5)
        == []
    )


# --- (c) семантика каждого ребра пути из настроек ---


def test_semantics_of_path_edges(store):
    paths = store.find_join_paths(
        "customers", "campaigns", ctx=CTX, allowed_tables=ALLOWED_LOGIST, k=5
    )
    semantics = store.path_semantics([edge.rel_id for edge in paths[0].edges], ctx=CTX)
    by_rel = {s.rel_id: s for s in semantics}
    assert set(by_rel) == {"e_customers_orders", "e_campaigns_orders"}
    customers_orders = by_rel["e_customers_orders"]
    assert customers_orders.join_type == "inner"
    assert customers_orders.meaning == "клиент разместил заказ"
    campaigns_orders = by_rel["e_campaigns_orders"]
    assert campaigns_orders.join_type == "inner"
    # Допущение атрибуции ДЗ-04 (миграция 008): first-touch раскрыт в реестре
    assert "first-touch" in (campaigns_orders.usage_notes or "")
    assert campaigns_orders.intent_tags == ["attribution", "ads"]


def test_semantics_fail_closed_on_unknown_edge(store):
    """Ребро вне active-реестра — честный сигнал дрейфа, не пустая семантика."""
    with pytest.raises(SchemaGraphError, match="sync-graph"):
        store.path_semantics(["it-no-such-edge"], ctx=CTX)


# --- (d) reconciliation: активные строки реестра = рёбра графа (ADR-006 §4.3) ---


def test_reconciliation_registry_equals_graph(store, settings, neo4j_driver):
    with psycopg.connect(_pg_service_dsn(settings), row_factory=dict_row) as conn:
        registry_rows = conn.execute(
            "SELECT edge_id, left_table, right_table, weight FROM agent_relationships_active"
        ).fetchall()
    registry = {
        (row["edge_id"], row["left_table"], row["right_table"], float(row["weight"]))
        for row in registry_rows
    }
    records, _, _ = neo4j_driver.execute_query(
        "MATCH (a:Table)-[r:JOINS]->(b:Table) "
        "RETURN r.rel_id AS rel_id, a.name AS left_table, "
        "b.name AS right_table, r.weight AS weight",
        database_="neo4j",
    )
    graph = {
        (rec["rel_id"], rec["left_table"], rec["right_table"], float(rec["weight"]))
        for rec in records
    }
    assert registry == graph  # расхождение в любую сторону — красное
    # deprecated-ребро в графе отсутствует (вьюха active его не отдаёт)
    assert "e_orders_items_v0" not in graph
    # маркер ревизии на месте (0 = снапшот не загружен — Ловушка-1)
    assert store.graph_version() > 0


# --- (e) правка настроек → graph_version растёт → синк меняет граф ---


def test_registry_edit_bumps_graph_version(store, settings):
    counter_sql = "SELECT version FROM settings_version WHERE name = 'relationships'"
    with psycopg.connect(_pg_service_dsn(settings), row_factory=dict_row) as conn:
        counter_before = conn.execute(counter_sql).fetchone()["version"]
    version_before = store.graph_version()

    try:
        with psycopg.connect(_pg_service_dsn(settings), row_factory=dict_row) as conn:
            conn.execute(
                "UPDATE agent_relationships SET weight = 1.25 WHERE edge_id = %s",
                (_DEMO_EDGE,),
            )
        with psycopg.connect(_pg_service_dsn(settings), row_factory=dict_row) as conn:
            counter_after = conn.execute(counter_sql).fetchone()["version"]
        assert counter_after > counter_before  # триггер settings_version сработал

        report = store.sync()
        assert report.graph_version == counter_after  # ревизия снапшота = счётчику
        assert version_before == 0 or store.graph_version() > version_before

        paths = store.find_join_paths(
            "customers", "campaigns", ctx=CTX, allowed_tables=ALLOWED_LOGIST, k=5
        )
        assert paths[0].total_weight == pytest.approx(2.25)  # 1.0 + 1.25: граф поменялся
    finally:
        # Восстановление демо-значения: реестр остаётся в каноническом сиде
        with psycopg.connect(_pg_service_dsn(settings), row_factory=dict_row) as conn:
            conn.execute(
                "UPDATE agent_relationships SET weight = %s WHERE edge_id = %s",
                (_DEMO_WEIGHT, _DEMO_EDGE),
            )
        store.sync()
    paths = store.find_join_paths(
        "customers", "campaigns", ctx=CTX, allowed_tables=ALLOWED_LOGIST, k=5
    )
    assert paths[0].total_weight == pytest.approx(2.2)  # граф вернулся к сну 0003


# --- (f) фильтр прав: пути через недоступные таблицы не возвращаются ---


def test_rights_filter_hides_unavailable_tables(store):
    # analyst без прав на campaigns: путь orders→campaigns не существует в его мире
    assert (
        store.find_join_paths(
            "orders",
            "campaigns",
            ctx=RunContext(trace_id="it-join-graph", user_id="u_analyst"),
            allowed_tables=ALLOWED_ANALYST,
            k=5,
        )
        == []
    )
    # доступная пара того же пользователя ищется нормально
    paths = store.find_join_paths(
        "orders",
        "products",
        ctx=RunContext(trace_id="it-join-graph", user_id="u_analyst"),
        allowed_tables=ALLOWED_ANALYST,
        k=5,
    )
    assert len(paths) == 1 and paths[0].tables == ["orders", "order_items", "products"]


def test_rights_filter_deny_by_default(store):
    ctx_hr = RunContext(trace_id="it-join-graph", user_id="u_hr")
    # связная пара в своих правах — путь есть
    own_domain = store.find_join_paths(
        "employees", "bonuses", ctx=ctx_hr, allowed_tables=ALLOWED_HR, k=5
    )
    assert len(own_domain) == 1
    # та же СВЯЗНАЯ пара, но прав хватает только на employees: путь через
    # недоступный bonuses не возвращается (deny-by-default режет по узлам,
    # а не «нет связи в топологии» — ревью S07 Minor)
    assert (
        store.find_join_paths(
            "employees",
            "bonuses",
            ctx=ctx_hr,
            allowed_tables=frozenset({"employees"}),
            k=5,
        )
        == []
    )


def test_empty_allowed_tables_returns_no_paths(store):
    """Неизвестный пользователь (пустой scope) — deny-by-default, не ошибка."""
    assert (
        store.find_join_paths("customers", "orders", ctx=CTX, allowed_tables=frozenset(), k=5) == []
    )


def test_contract_violations_raise_value_error(store):
    with pytest.raises(ValueError, match="from_table"):
        store.find_join_paths("orders", "orders", ctx=CTX, allowed_tables=ALLOWED_LOGIST, k=1)
    with pytest.raises(ValueError, match="k"):
        store.find_join_paths("customers", "orders", ctx=CTX, allowed_tables=ALLOWED_LOGIST, k=0)

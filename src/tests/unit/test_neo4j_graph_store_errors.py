"""Юнит-тесты типизированных ошибок адаптера Neo4jGraphStore (S07 fix-волна-2).

Контракт порта: сбой хранилища — `SchemaGraphError`, не сырой SDK-эксепшен.
Регрессия ревью S07 (Important re-review): в neo4j 6.4.1 иерархия
`Neo4jError → GqlError`, но `DriverError → GqlError` и
`ServiceUnavailable → DriverError` — ВНЕ `Neo4jError`; `except (Neo4jError,
OSError)` пропускал ServiceUnavailable/SessionExpired наружу — самый частый
сценарий отказа («Neo4j не поднят») ломал контракт. Тест на fake-драйвере
(модульный атрибут GraphDatabase подменяется): на до-фиксовом коде падает
(сырой ServiceUnavailable), после расширения except — зелёный.

Ловушка-5 сессии не про это, но поведение согласовано: skip-guard
(test_join_graph_skip_guard.py) скипает ДО sync() — недоступную инфру он не
проверяет; здесь — именно отказ драйвера при живом вызове.
"""

from types import SimpleNamespace

import pytest
from neo4j.exceptions import ServiceUnavailable

from text_to_sql_agent.application.ports.schema_graph_store import SchemaGraphError
from text_to_sql_agent.domain.entities import RunContext
from text_to_sql_agent.infrastructure.graph import neo4j_graph_store
from text_to_sql_agent.infrastructure.graph.neo4j_graph_store import Neo4jGraphStore

CTX = RunContext(trace_id="ut-graph-errors", user_id="u_logist")


class _RaisingDriver:
    """Fake-драйвер: любой execute_query роняет транспортной ошибкой SDK."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def execute_query(self, *args: object, **kwargs: object) -> object:
        raise self._exc

    def session(self, **kwargs: object) -> object:
        raise AssertionError("session не должен вызываться в этих сценариях")

    def close(self) -> None:
        pass


def _store(monkeypatch: pytest.MonkeyPatch) -> Neo4jGraphStore:
    driver = _RaisingDriver(ServiceUnavailable("bolt down"))
    monkeypatch.setattr(
        neo4j_graph_store, "GraphDatabase", SimpleNamespace(driver=lambda *a, **kw: driver)
    )
    return Neo4jGraphStore("bolt://127.0.0.1:1", "neo4j", "pwd", planner_dsn="postgresql://x")


def test_graph_version_typed_error_on_service_unavailable(monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(SchemaGraphError, match="Neo4j недоступен"):
        _store(monkeypatch).graph_version()


def test_find_join_paths_typed_error_on_service_unavailable(monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(SchemaGraphError, match="поиск путей"):
        _store(monkeypatch).find_join_paths(
            "customers", "orders", ctx=CTX, allowed_tables=frozenset({"customers", "orders"}), k=5
        )


def test_sync_typed_error_on_service_unavailable(monkeypatch: pytest.MonkeyPatch):
    """PG здоров (реестр подменён), но Neo4j недоступен — синк тоже типизирован."""
    store = _store(monkeypatch)
    monkeypatch.setattr(store, "_read_registry", lambda: ([], 5))
    with pytest.raises(SchemaGraphError, match="загрузка снапшота"):
        store.sync()

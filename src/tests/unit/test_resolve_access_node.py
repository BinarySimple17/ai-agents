"""Юнит-тесты узла resolve_access и кэша прав (S05 Tasks 2–4, rulings R3/R5/R6).

Файл — test_resolve_access_node.py: basename не совпадает с integration
test_resolve_access.py (коллизия import-file-mismatch без __init__.py).

Сервис детерминирован и БЕЗ LLM (behavior feat-005); кэш валиден, пока ОБА
счётчика settings_version ('permissions' + 'user_roles') не выросли — сверка
только 'permissions' пропустила бы revoke роли (ruling R3). Узел fail-closed:
нет identity / unknown user_id / недоступность БД настроек — все три
FLAG_IDENTITY_FAILED → существующее ребро terminal_rejected (routing не трогаем).
"""

from typing import Any

import pytest

from text_to_sql_agent.application.ports.permission_store import (
    RIGHTS_VERSION_KEYS,
    RightsUnavailableError,
    UserRights,
)
from text_to_sql_agent.application.services.resolve_access_node import (
    IDENTITY_FAILED_FLAG,
    make_resolve_access_node,
)
from text_to_sql_agent.application.services.resolve_access_service import ResolveAccessService
from text_to_sql_agent.orchestration.routing import FLAG_IDENTITY_FAILED

_RIGHTS = UserRights(
    user_id="u_analyst",
    user_known=True,
    roles=("analyst",),
    tables={"orders": ("*",)},
    versions={"permissions": 2, "user_roles": 1},
)


class _FakeStore:
    """Скриптовый PermissionStore: счётчики вызовов, правки версий снаружи."""

    def __init__(
        self,
        *,
        rights: dict[str, UserRights] | None = None,
        broken: bool = False,
        broken_message: str | None = None,
    ):
        self.rights = rights if rights is not None else {}
        # broken = сбой fetch_rights (settings_versions жив — узел доходит до
        # чтения прав; rевью I1: деталь psycopg приходит из fetch-пути)
        self.broken = broken
        self.broken_message = broken_message or "таблицы прав недоступны"
        self.versions = {"permissions": 2, "user_roles": 1}
        self.versions_calls = 0
        self.fetch_calls = 0

    def settings_versions(self) -> dict[str, int]:
        self.versions_calls += 1
        return dict(self.versions)

    def fetch_rights(self, user_id: str) -> UserRights:
        self.fetch_calls += 1
        if self.broken:
            raise RightsUnavailableError(self.broken_message)
        rights = self.rights.get(user_id)
        if rights is None:
            return UserRights(
                user_id=user_id, user_known=False, roles=(), tables={}, versions=dict(self.versions)
            )
        return rights


def _unknown() -> UserRights:
    return UserRights(
        user_id="ghost",
        user_known=False,
        roles=(),
        tables={},
        versions={"permissions": 1, "user_roles": 1},
    )


class TestResolveAccessServiceCache:
    def test_first_resolve_fetches_and_caches(self) -> None:
        store = _FakeStore(rights={"u_analyst": _RIGHTS})
        service = ResolveAccessService(store)
        assert service.resolve("u_analyst").tables == {"orders": ("*",)}
        assert store.fetch_calls == 1

    def test_cache_hit_when_versions_unchanged(self) -> None:
        store = _FakeStore(rights={"u_analyst": _RIGHTS})
        service = ResolveAccessService(store)
        service.resolve("u_analyst")
        service.resolve("u_analyst")
        # Права читаются на каждый запрос — но ПОЛНОЕ чтение только на промахе
        assert store.versions_calls == 2
        assert store.fetch_calls == 1

    def test_permissions_bump_invalidates_cache(self) -> None:
        # Правка строк прав → 'permissions' вырос → перечитывание (behavior feat-005)
        store = _FakeStore(rights={"u_analyst": _RIGHTS})
        service = ResolveAccessService(store)
        service.resolve("u_analyst")
        store.versions["permissions"] += 1
        store.rights["u_analyst"] = UserRights(
            user_id="u_analyst",
            user_known=True,
            roles=("analyst",),
            tables={"orders": ("*",), "products": ("*",)},
            versions=dict(store.versions),
        )
        assert service.resolve("u_analyst").tables == {"orders": ("*",), "products": ("*",)}
        assert store.fetch_calls == 2

    def test_user_roles_bump_invalidates_cache(self) -> None:
        # Выдача/отзыв роли → 'user_roles' вырос → перечитывание (ruling R3:
        # кэш по одному счётчику пропустил бы revoke)
        store = _FakeStore(rights={"u_analyst": _RIGHTS})
        service = ResolveAccessService(store)
        service.resolve("u_analyst")
        store.versions["user_roles"] += 1
        service.resolve("u_analyst")
        assert store.fetch_calls == 2

    def test_unknown_user_cached_too(self) -> None:
        store = _FakeStore()
        service = ResolveAccessService(store)
        assert service.resolve("ghost").user_known is False
        assert service.resolve("ghost").user_known is False
        assert store.fetch_calls == 1

    def test_rights_unavailable_propagates(self) -> None:
        service = ResolveAccessService(_FakeStore(broken=True))
        with pytest.raises(RightsUnavailableError):
            service.resolve("u_analyst")

    def test_rights_version_keys_canon(self) -> None:
        # Оба счётчика — канон R3; состав не расползается молча
        assert RIGHTS_VERSION_KEYS == ("permissions", "user_roles")


class TestResolveAccessNode:
    def _node(self, store: _FakeStore | None = None) -> Any:
        if store is None:
            store = _FakeStore(rights={"u_analyst": _RIGHTS})
        return make_resolve_access_node(ResolveAccessService(store))

    def test_known_user_writes_permissions_map_and_clears_error(self) -> None:
        node = self._node()
        state = {"user_id": "u_analyst", "error": {"class": "stale"}}
        updated = node(state)
        assert updated["permissions"] == {"orders": ["*"]}
        assert updated["error"] is None
        assert updated["graph_version"] == 0  # снапшот не загружен — пин S07 (R6)
        assert updated["route_flags"][IDENTITY_FAILED_FLAG] is False

    def test_flag_name_matches_routing_constant(self) -> None:
        # application не импортирует orchestration — совпадение строк гвардит тест
        assert IDENTITY_FAILED_FLAG == FLAG_IDENTITY_FAILED

    def test_permissions_lists_not_tuples(self) -> None:
        # Контракт 3 S00: {tables: {table: [columns]}} — в state списки
        updated = self._node()({"user_id": "u_analyst"})
        assert all(isinstance(cols, list) for cols in updated["permissions"].values())

    def test_missing_identity_rejected(self) -> None:
        updated = self._node()({})
        assert updated["route_flags"][IDENTITY_FAILED_FLAG] is True
        assert updated["error"]["class"] == "identity_missing"

    def test_unknown_user_rejected(self) -> None:
        store = _FakeStore()
        updated = self._node(store)({"user_id": "ghost"})
        assert updated["route_flags"][IDENTITY_FAILED_FLAG] is True
        assert updated["error"]["class"] == "unknown_user"
        assert "permissions" not in updated  # карты для unknown нет — fail-closed

    def test_rights_unavailable_rejected_not_empty_map(self) -> None:
        # Недоступность БД настроек → отказ, НЕ «пустые права = всё разрешено»
        store = _FakeStore(broken=True)
        updated = self._node(store)({"user_id": "u_analyst"})
        assert updated["route_flags"][IDENTITY_FAILED_FLAG] is True
        assert updated["error"]["class"] == "rights_unavailable"

    def test_roles_without_permissions_is_legal_deny(self) -> None:
        # «Роли есть, прав нет» — легальный deny-по-умолчанию: пустая карта,
        # продолжение по графу (ruling R6), не rejected
        store = _FakeStore(
            rights={
                "u_bare": UserRights(
                    user_id="u_bare",
                    user_known=True,
                    roles=("analyst",),
                    tables={},
                    versions={"permissions": 1, "user_roles": 1},
                )
            }
        )
        updated = self._node(store)({"user_id": "u_bare"})
        assert updated["permissions"] == {}
        assert updated["route_flags"][IDENTITY_FAILED_FLAG] is False


class _RecordingTracer:
    """Фейковый Tracer: копит записи для проверок (прецедент юнит-тестов S03)."""

    def __init__(self) -> None:
        self.records: list[Any] = []

    def record(self, trace: Any) -> None:
        self.records.append(trace)


class TestRefusalPolicyS9:
    """Политика отказов S9 / ревью S05 Important-1: пользовательский отказ без
    деталей инфраструктуры; техническая деталь — только в операторский трейс."""

    _PSYCOSP_DETAIL = (
        'connection to server at "127.0.0.1", port 5432 failed: '
        'FATAL: password authentication failed for user "agent_planner"'
    )

    @staticmethod
    def _node(store: _FakeStore, tracer: _RecordingTracer | None = None) -> Any:
        return make_resolve_access_node(ResolveAccessService(store), tracer=tracer)

    def test_refusal_message_fixed_without_infra_details(self) -> None:
        # Регрессия: до фикса message содержал str(exc) — host/port/роль БД
        # psycopg утекали в stdout через CLI (S9/Task 4 плана S05)
        store = _FakeStore(broken=True, broken_message=self._PSYCOSP_DETAIL)
        updated = self._node(store)({"user_id": "u_analyst"})
        message = updated["error"]["message"]
        assert "127.0.0.1" not in message
        assert "agent_planner" not in message
        assert message == "права недоступны — запрос отклонён (fail-closed)"

    def test_infra_detail_goes_to_trace_not_state(self) -> None:
        store = _FakeStore(broken=True, broken_message=self._PSYCOSP_DETAIL)
        tracer = _RecordingTracer()
        self._node(store, tracer)({"user_id": "u_analyst"})
        assert [event.event for event in tracer.records] == ["resolve.rights_unavailable"]
        payload = tracer.records[0].payload
        assert "agent_planner" in payload["detail"]  # оператору — для диагностики
        assert payload["user_id"] == "u_analyst"

    def test_whitespace_identity_rejected_not_crash(self) -> None:
        # Ревью S05 Minor-2: whitespace-only user_id — fail-closed, не ValueError
        # из адаптера, проброшенный наружу графа
        updated = self._node(_FakeStore())({"user_id": "   "})
        assert updated["error"]["class"] == "identity_missing"

    def test_node_works_without_tracer(self) -> None:
        store = _FakeStore(broken=True)
        updated = self._node(store)({"user_id": "u_analyst"})  # tracer не передан
        assert updated["error"]["class"] == "rights_unavailable"

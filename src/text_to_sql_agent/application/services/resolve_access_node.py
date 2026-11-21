"""Узел-сервис FSM `resolve_access` (S05 Task 4) — наполнение заглушки S04.

Детерминированный первый узел конвейера: verified user_id кладёт в state точка
входа ДО graph.invoke (CLI вызывает `AuthProvider.verify`, ruling R4 ARCH-GATE
S05 — сырой user_id verify не обходит), узел строит карту прав
`{tables: {table: [columns]}}` (S00 контракт 3) + пишет `graph_version=0`
(снапшот не загружен — реальный пин ставит синк S07, ruling R6). БЕЗ LLM
(behavior feat-005): rejected-исход уходит ДО первого LLM-узла (clarify).

Fail-closed (ruling R6, все три случая — `FLAG_IDENTITY_FAILED` → существующее
ребро terminal_rejected, routing не трогается):
- нет identity в state (обход verify или дефект сборки) — `identity_missing`;
- unknown user_id (нет строк в agent_user_roles) — `unknown_user`;
- недоступность БД настроек (`RightsUnavailableError`) — `rights_unavailable`:
  отказ, НЕ «пустые права = всё разрешено» (S00 правило 8).

«Роли есть, прав нет» — легальный deny-по-умолчанию: пустая карта, продолжение
по графу (подграф/retrieval/валидатор в S09/S11 закроют всё сами).

Граница слоёв: имя флага дублируется константой — application не импортирует
orchestration (F0 = routing.py), ребро terminal_rejected читает
FLAG_IDENTITY_FAILED; совпадение имён проверяет тест.
"""

from collections.abc import Callable, Mapping
from typing import Any

from text_to_sql_agent.application.ports.permission_store import RightsUnavailableError
from text_to_sql_agent.application.services.resolve_access_service import ResolveAccessService

State = Mapping[str, Any]

# Имя флага — FLAG_IDENTITY_FAILED (orchestration/routing.py): приложение не
# импортирует orchestration, совпадение строк проверяет тест (test_graph)
IDENTITY_FAILED_FLAG = "identity_failed"

ERROR_KEY = "error"
PERMISSIONS_KEY = "permissions"
GRAPH_VERSION_KEY = "graph_version"
ROUTE_FLAGS_KEY = "route_flags"
USER_ID_KEY = "user_id"

# graph_version=0 = снапшот не загружен (ADR-006 §4.3); реальный пин — S07 (R6)
NO_SNAPSHOT_VERSION = 0

_UNKNOWN_USER_MESSAGE = "пользователь не найден в системе прав — запрос отклонён (fail-closed)"
_IDENTITY_MISSING_MESSAGE = "verified identity отсутствует в прогоне — сырой user_id не проходит"


def _reject(
    updated: dict[str, Any], flags: dict[str, Any], error_class: str, message: str
) -> dict[str, Any]:
    """Fail-closed-исход: FLAG_IDENTITY_FAILED → terminal_rejected (routing не тронут)."""
    flags[IDENTITY_FAILED_FLAG] = True
    updated[ROUTE_FLAGS_KEY] = flags
    updated[ERROR_KEY] = {"class": error_class, "message": message}
    updated[GRAPH_VERSION_KEY] = NO_SNAPSHOT_VERSION
    return updated


def make_resolve_access_node(
    service: ResolveAccessService,
) -> Callable[[State], dict[str, Any]]:
    """Фабрика узла: сервис прав → функция узла для биндера графа (S04-форма).

    Узел пишет флаг своей зоны НАПРЯУЮ (ruling R6: setdefault-семантика —
    механика заглушек S04, не обязательна для содержательного узла): успех —
    False, fail-closed — True, решение перерешивается на каждом входе.
    """

    def node(state: State) -> dict[str, Any]:
        updated = dict(state)
        flags = dict(updated.get(ROUTE_FLAGS_KEY) or {})
        user_id = updated.get(USER_ID_KEY) or ""
        if not user_id:
            return _reject(updated, flags, "identity_missing", _IDENTITY_MISSING_MESSAGE)
        try:
            rights = service.resolve(user_id)
        except RightsUnavailableError as exc:
            return _reject(
                updated,
                flags,
                "rights_unavailable",
                f"права недоступны — отказ вместо пустой карты: {exc}",
            )
        if not rights.user_known:
            return _reject(updated, flags, "unknown_user", _UNKNOWN_USER_MESSAGE)
        updated[PERMISSIONS_KEY] = {table: list(cols) for table, cols in rights.tables.items()}
        updated[GRAPH_VERSION_KEY] = NO_SNAPSHOT_VERSION
        updated[ERROR_KEY] = None  # успешный вход обязан чистить стейл-ошибку
        flags[IDENTITY_FAILED_FLAG] = False
        updated[ROUTE_FLAGS_KEY] = flags
        return updated

    return node

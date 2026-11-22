"""Адаптер `PermissionStore` — чтение прав из настроек PG под agent_planner (S05 Task 2).

Гранты — миграция 0002:80-83 (SELECT на agent_user_roles / agent_permissions /
settings_version; ruling R1 ARCH-GATE S05 — новых миграций не нужно). Соединение
на вызов (прецедент PgEdgeProposalStore): права читаются на КАЖДЫЙ запрос
(ADR-005 §4); кэш со сверкой версий — зона сервиса (resolve_access_service,
ruling R3), адаптер всегда отдаёт свежий снапшот одним соединением.

Нормализация колонок на границе (ruling R5, прецедент I4 S09): CSV-текст БД →
кортеж колонок, `'*'` литералом; повреждённая строка (пустая/пустые элементы
CSV) — `RightsUnavailableError` (отказ), не тихое сужение прав.
"""

import psycopg

from text_to_sql_agent.application.ports.permission_store import (
    RIGHTS_VERSION_KEYS,
    RightsUnavailableError,
    UserRights,
)

_CONNECT_TIMEOUT_S = 10  # slow-фейл узла лучше висячего соединения (S00 правило 9)

_VERSIONS_SQL = "SELECT name, version FROM settings_version WHERE name = ANY(%s)"
_ROLES_SQL = "SELECT role FROM agent_user_roles WHERE user_id = %s ORDER BY role"
# schema_name = 'public' — defense-in-depth (ревью S05 Minor-3): мир агента —
# публичная схема (ключи карты прав — голые table_name, как agent_permissions.table_name
# в retrieval-фильтре S06); строка прав непубличной схемы молча не попадает в карту —
# deny-by-default («недоступная таблица не существует»), не слияние в чужой ключ
_PERMISSIONS_SQL = (
    "SELECT table_name, columns FROM agent_permissions "
    "WHERE schema_name = 'public' AND role = ANY(%s) ORDER BY table_name"
)


def _shorten(message: str) -> str:
    return " ".join(str(message).split())


def _normalize_columns(raw: str, table_name: str) -> tuple[str, ...]:
    """CSV-текст БД → кортеж колонок; '*' литералом (ruling R5, прецедент I4 S09)."""
    stripped = raw.strip()
    if not stripped:
        raise RightsUnavailableError(
            f"agent_permissions.columns пуст для '{table_name}' — отказ, не угадывание"
        )
    columns: list[str] = []
    for entry in stripped.split(","):
        name = entry.strip()
        if not name:
            raise RightsUnavailableError(
                f"agent_permissions.columns повреждён для '{table_name}' — отказ, не сужение"
            )
        columns.append(name)
    if "*" in columns:  # '*' = вся таблица — сильнее любого списка колонок
        return ("*",)
    return tuple(dict.fromkeys(columns))  # дедупликация с сохранением порядка


def _merge_columns(acc: dict[str, tuple[str, ...]], table: str, cols: tuple[str, ...]) -> None:
    """Слияние прав одной таблицы по ролям: '*' сильнее списка, остальное — объединение."""
    existing = acc.get(table)
    if existing is None:
        acc[table] = cols
    elif "*" in existing or "*" in cols:
        acc[table] = ("*",)
    else:
        acc[table] = tuple(dict.fromkeys([*existing, *cols]))


def _read_versions(conn: psycopg.Connection) -> dict[str, int]:
    """Счётчики RIGHTS_VERSION_KEYS одним SELECT; отсутствующая строка = 0."""
    rows = conn.execute(_VERSIONS_SQL, (list(RIGHTS_VERSION_KEYS),)).fetchall()
    versions = {key: 0 for key in RIGHTS_VERSION_KEYS}
    for name, version in rows:
        if name in versions:
            versions[name] = int(version)
    return versions


class PgPermissionStore:
    """Права из PostgreSQL под agent_planner; соединение на вызов (права — на каждый запрос)."""

    def __init__(self, planner_dsn: str) -> None:
        self._planner_dsn = planner_dsn

    def settings_versions(self) -> dict[str, int]:
        try:
            with psycopg.connect(self._planner_dsn, connect_timeout=_CONNECT_TIMEOUT_S) as conn:
                return _read_versions(conn)
        except psycopg.Error as exc:
            raise RightsUnavailableError(
                f"счётчики settings_version недоступны: {_shorten(exc)}"
            ) from exc

    def fetch_rights(self, user_id: str) -> UserRights:
        """Роли + слияние прав по ролям + версии одним соединением (снапшот)."""
        if not user_id or not user_id.strip():
            raise ValueError("user_id обязателен — identity приходит из проверенного токена")
        try:
            with psycopg.connect(self._planner_dsn, connect_timeout=_CONNECT_TIMEOUT_S) as conn:
                versions = _read_versions(conn)
                roles = [row[0] for row in conn.execute(_ROLES_SQL, (user_id,)).fetchall()]
                tables: dict[str, tuple[str, ...]] = {}
                if roles:
                    rows = conn.execute(_PERMISSIONS_SQL, (roles,)).fetchall()
                    for table_name, raw_columns in rows:
                        _merge_columns(
                            tables, table_name, _normalize_columns(raw_columns, table_name)
                        )
        except psycopg.Error as exc:
            raise RightsUnavailableError(
                f"таблицы настроек прав недоступны: {_shorten(exc)}"
            ) from exc
        return UserRights(
            user_id=user_id,
            user_known=bool(roles),
            roles=tuple(roles),
            tables=tables,
            versions=versions,
        )

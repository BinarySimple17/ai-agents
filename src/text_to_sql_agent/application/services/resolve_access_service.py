"""Сервис узла resolve_access (S05 Task 2/3): user_id → карта прав из настроек.

Детерминированный код, БЕЗ LLM (behavior feat-005). Права — из таблиц настроек
через порт `PermissionStore` (ADR-005 §4: читаются на каждый запрос); кэш
per-user in-memory со сверкой ОБЕИХ версий `settings_version` ('permissions' +
'user_roles', ruling R3 ARCH-GATE S05) на каждый запрос: триггеры bump
инкрементируют счётчики при правке строк настроек → следующий запрос
перечитывает права (behavior feat-005: «правка строк настроек действует со
следующего запроса»). Кэш по одному счётчику — дыра revoke: выдача/отзыв роли
инкрементирует 'user_roles', а не 'permissions'.

In-memory per-user в MVP правомерен (ADR-005 §4: пользователей ≤ 10) — Valkey
только если доказано needed (границы скоупа S05).
"""

from text_to_sql_agent.application.ports.permission_store import PermissionStore, UserRights


class ResolveAccessService:
    """user_id → UserRights; кэш валиден, пока оба счётчика настроек не выросли."""

    def __init__(self, store: PermissionStore) -> None:
        self._store = store
        self._cache: dict[str, UserRights] = {}

    def resolve(self, user_id: str) -> UserRights:
        """Карта прав пользователя (кэш-хит при неизменных версиях настроек)."""
        # Сверка версий ДО кэша (ruling R3): оба счётчика — кэш устарел при правке
        # строк прав ИЛИ при выдаче/отзыве роли; отсутствие строки счётчика = 0
        versions = self._store.settings_versions()
        cached = self._cache.get(user_id)
        if cached is not None and all(
            cached.versions.get(key, 0) == value for key, value in versions.items()
        ):
            return cached
        rights = self._store.fetch_rights(user_id)
        self._cache[user_id] = rights
        return rights

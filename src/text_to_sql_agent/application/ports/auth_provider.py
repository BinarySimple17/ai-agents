"""Порт `AuthProvider` — проверка токена → verified identity (S00 таблица портов; ADR-005).

Аутентификация — ДО агента, агент — resource server (ADR-005): identity приходит
в прогон только из ПРОВЕРЕННОГО токена; голый ``user_id`` параметром — не identity
(аудит фиксирует проверенную личность, а не то, что прислал клиент). Точка
верификации — точка входа ДО прогона FSM (ruling R4 ARCH-GATE S05); узел
resolve_access fail-closed, если verified identity в state не пришла.

MVP — DevTokenAuth (dev-токены демо-пользователей из ``Settings``), production —
OIDCAuth (JWKS-верификация JWT, вне скоупа MVP; S00:88).

Fail-closed (S00 правило 8): пустой/неизвестный токен — типизированный отказ
`AuthenticationError`, дефолтного пользователя нет.
"""

from typing import Protocol, runtime_checkable


class AuthenticationError(Exception):
    """Токен не проверен (пустой/неизвестный): сообщение без деталей конфигурации."""


@runtime_checkable
class AuthProvider(Protocol):
    """Проверка токена — единственный законный источник identity (контракт 3 S00)."""

    def verify(self, token: str) -> str:
        """Проверить токен, вернуть verified user_id (identity).

        Пустой/неизвестный токен → `AuthenticationError` (fail-closed), не
        исключение-заглушка и не None: отказ аутентификации — валидный отказ
        запроса, а не неопределённость.
        """
        ...

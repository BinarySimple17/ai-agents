"""Адаптер `AuthProvider` MVP — DevTokenAuth (S05 Task 1; ADR-005:114-117).

Токен → user_id из списка демо-пользователей в ``Settings`` (``AGENT_DEV_TOKENS``:
секрет — в .env, в коде только чтение через Settings); никаких обращений к внешним
IdP. Production-реализация порта — OIDCAuth (вне скоупа MVP; S00:88).

Fail-closed: пустой/неизвестный токен — `AuthenticationError`, дефолтного
пользователя нет; пустая карта токенов — отклоняется любой токен.

Сравнение токенов — `secrets.compare_digest` (constant-time): сравнение через ==
даёт timing side-channel на подборе токена; байты, чтобы не-ASCII-токен не
поднимал TypeError от str-варианта сравнения.
"""

import secrets
from collections.abc import Mapping

from text_to_sql_agent.application.ports.auth_provider import AuthenticationError


class DevTokenAuth:
    """MVP-аутентификация: dev-токены демо-пользователей ({user_id: token})."""

    def __init__(self, tokens: Mapping[str, str]) -> None:
        self._tokens = dict(tokens)

    def verify(self, token: str) -> str:
        """Токен → user_id; неизвестный/пустой — `AuthenticationError` (fail-closed)."""
        if not token or not token.strip():
            raise AuthenticationError("токен не передан — запрос отклонён (fail-closed)")
        probe = token.encode("utf-8")
        for user_id, expected in self._tokens.items():
            if secrets.compare_digest(probe, expected.encode("utf-8")):
                return user_id
        raise AuthenticationError("токен не распознан — запрос отклонён (fail-closed)")

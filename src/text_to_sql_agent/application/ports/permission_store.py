"""Порт `PermissionStore` — чтение карты прав из таблиц настроек (S05; S00 стык 3).

Имя — ruling R2 ARCH-GATE S05: НЕ `AccessPolicy` (ADR-005 резервирует его под
будущий policy-движок рубежа 5 — RLS/region). Читает `agent_user_roles` →
`agent_permissions` + счётчики `settings_version` под ролью `agent_planner`
(SELECT-гранты — миграция 0002:80-83; ruling R1 ARCH-GATE S05 — новых миграций
не нужно).

Формат прав — S00 контракт 3: `{tables: {table: [columns]}}` — единственный
источник для подграфа (S09), retrieval-фильтра (S06) и AST-валидатора (S11);
ключ — имя таблицы (как `agent_permissions.table_name`; публичная схема — мир
агента, deny-by-default). Колонки в БД — CSV-текст: нормализация на границе
адаптера (ruling R5, прецедент I4 S09), `'*'` остаётся литералом `'*'`
(разворот в конкретные колонки — DDL-интроспекция, зона S11/S09).

Ошибки стыка типизированы (прецедент `EdgeProposalError`): `RightsUnavailableError`
— сбой чтения (соединение/БД/повреждённые данные) → отказ, НЕ «пустые права = всё
разрешено» (S00 правило 8, fail-closed); нарушение контракта вызова — ValueError.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

# Ключи счётчиков settings_version, от которых зависят права (ruling R3 ARCH-GATE
# S05): правка строк прав инкрементирует 'permissions', выдача/отзыв роли —
# 'user_roles' (триггеры миграции 0001:173-177). Сверять ОБА: кэш по одному
# счётчику пропустил бы revoke роли ('user_roles') или выданные права
# ('permissions') — ровно ради этого кейса ADR-005 §4 вводил счётчики.
RIGHTS_VERSION_KEYS: tuple[str, ...] = ("permissions", "user_roles")


class RightsUnavailableError(Exception):
    """Сбой чтения прав (соединение/БД/повреждённые данные): отказ, не «пустые права»."""


@dataclass(frozen=True, slots=True)
class UserRights:
    """Снапшот прав пользователя: роли + карта таблиц + версии настроек (кэш R3).

    `user_known=False` — user_id отсутствует в `agent_user_roles` (unknown →
    rejected-исход, решение принимает узел, ruling R6); «роли есть, прав нет» —
    `user_known=True` при пустой `tables` — легальный deny-по-умолчанию.
    """

    user_id: str
    user_known: bool
    roles: tuple[str, ...]
    # {table: (columns…)}, '*' литералом (R5); в state уходит списком (контракт 3)
    tables: Mapping[str, tuple[str, ...]]
    # {'permissions': n, 'user_roles': n} — сверка кэша на каждый запрос (R3)
    versions: Mapping[str, int]


@runtime_checkable
class PermissionStore(Protocol):
    """Чтение прав из таблиц настроек; соединение на вызов (права — на каждый запрос)."""

    def settings_versions(self) -> dict[str, int]:
        """Текущие счётчики `RIGHTS_VERSION_KEYS` (отсутствующая строка = 0)."""
        ...

    def fetch_rights(self, user_id: str) -> UserRights:
        """Роли + слияние прав по ролям + версии одним снапшотом (user_id непустой)."""
        ...

    def data_catalog(self) -> frozenset[str]:
        """Каталог мира агента: bare-имена таблиц `agent_data_sources` (SoT
        существования, BL-009). Различение исходов валидатора S11 (ADR-005 §4,
        plan правило 4): таблица вне каталога — «не существует» (hallucination,
        retry оправдан); в каталоге, но вне прав — «вне прав» (denied, degrade
        без retry). Грант SELECT agent_planner — миграция 0002:81; сбой чтения —
        `RightsUnavailableError` (fail-closed, не «пустой мир»)."""
        ...

    def columns_catalog(self, tables: frozenset[str]) -> dict[str, frozenset[str]]:
        """Фактические колонки запрошенных таблиц мира: `information_schema.columns`
        под `agent_planner` (разворот '*' — DDL-интроспекция, зона S11/S09 —
        предсказано в комментарии формата прав). Ключ — bare-имя таблицы,
        значения — нижний регистр (как в IR валидатора). Таблица без видимых
        планировщику колонок (GRANT-дрейф: есть в `agent_data_sources`/правах,
        но без GRANT SELECT) — ОТСУТСТВУЕТ в результате: вызывающий трактует
        отсутствие записи для '*'-таблицы как дефект стыка (fail-closed, ruling
        R1 S33 — не «пустая таблица», не «пустые права»). Сбой чтения —
        `RightsUnavailableError` (fail-closed)."""
        ...

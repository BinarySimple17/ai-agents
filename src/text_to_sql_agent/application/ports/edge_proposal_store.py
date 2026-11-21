"""Порт `EdgeProposalStore` — очередь заявок на рёбра (S09 Task 4; ADR-006 §4.4).

«Нет активного пути» — не только отказ, но и заявка владельцу: пара сущностей +
пример вопроса; подтвердит — следующий такой вопрос уже отвечается. Агент
пишет ТОЛЬКО в очередь заявок, не в реестр (ADR-006 §4.4); запись — код узла,
не LLM; append-only — UPDATE/DELETE у роли записи нет (правка очереди —
работа владельца через registry_writer, ревизия ADR-006 §4.2).

Канал записи — ruling Q1 ARCH-GATE S09: INSERT-грант для `agent_planner`
(миграция 0009; приватность «question_sample — чужие вопросы» из 0002 резалась
по SELECT, INSERT её не задевает; сессионного read-only у planner нет —
прецедент agent_trace не требуется). Заявка — settings-plane write кодом узла,
не пользовательский SQL: MUST «read-only agent_ro» не затрагивается.

Ошибки стыка типизированы (прецедент `VectorStoreError`): `EdgeProposalError` —
сбой записи (соединение/БД/права); нарушение контракта вызова — ValueError.

DTO стыка нет: заявка — 4 примитивных поля (KISS), DTO-обёртка не нужна.
"""

from typing import Protocol, runtime_checkable

from text_to_sql_agent.domain.entities import RunContext


class EdgeProposalError(Exception):
    """Сбой записи заявки (соединение/БД/права): человекочитаемое сообщение."""


@runtime_checkable
class EdgeProposalStore(Protocol):
    """Очередь заявок владельцу на недостающие рёбра (agent_edge_proposals).

    Запись append-only: без RETURNING (SELECT-гранта у роли нет), без UPDATE —
    частота `hits` считается владельцем как COUNT(*) строк (ruling Q1:
    `ON CONFLICT DO UPDATE` потребовал бы UPDATE-привилегию и размыл append-only).
    `question_sample` усекается адаптером до константы (приватность + размер).
    """

    def record_missing_edge(
        self,
        *,
        left_table: str,
        right_table: str,
        question_sample: str,
        ctx: RunContext,
    ) -> None:
        """INSERT заявки «нет активного пути между left/right». Пустой return — успех."""
        ...

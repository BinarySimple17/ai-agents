"""Порт `VectorStore` — контракт векторной памяти знаний (S00 стык; ADR-003 §4).

Поиск — **dense-only** на вехе V2 (решение владельца 2026-11-20, D-026 доп. 2):
cosine distance по эмбеддингу; механика качества (гибрид FTS+RRF, шумовой корпус,
реранкер) — веха V3 (BL-007), порт абстрагирует способ поиска.

Права — deny-by-default (ADR-005 «граница знаний = граница прав», NFR-4
ACL-leak = 0): фильтр прав живёт в самом запросе поиска (джойн с правами,
ADR-003 §4), не пост-фильтрация выдачи. Инвариант — по **всем таблицам,
упомянутым в документе** (`referenced_tables`), а не только по таблице-якорю:
кураторский few-shot «выручка по кампаниям» ссылается на orders И campaigns —
пользователь без прав на campaigns документ не видит (ревью S06 Critical-1).

Ошибки стыка типизированы (прецедент `SQLExecutionError`): `VectorStoreError` —
сбой хранилища (соединение/БД); нарушение контракта вызова (размерность,
параллельные списки) — ValueError.

DTO стыка — pydantic и живут здесь (прецедент: `sql_executor.py`).
"""

from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from text_to_sql_agent.domain.entities import RunContext

# Размерность эмбеддинга bge-m3 (model-card §1). Смена эмбеддера = переиндексация:
# другое пространство векторов — вся таблица пересобирается; model_version в
# документе фиксирует, каким эмбеддером он построен.
EMBEDDING_DIM = 1024


class DocKind(StrEnum):
    """Тип документа знаний — ровно 4 из feat-006 (ADR-003 §4; hw-plan §3)."""

    DDL = "ddl"  # DDL таблицы с комментариями колонок
    RELATIONSHIP = "relationship"  # связь таблиц с семантикой (meaning/usage_notes)
    GLOSSARY = "glossary"  # бизнес-термин → определение
    FEW_SHOT = "few_shot"  # кураторский пример «вопрос → SQL» (автонаполнение запрещено)


class VectorStoreError(Exception):
    """Сбой векторной памяти (соединение/БД): человекочитаемое сообщение, не traceback."""


class KnowledgeDocument(BaseModel):
    """Документ векторной памяти (строка agent_knowledge)."""

    model_config = ConfigDict(frozen=True)

    doc_kind: DocKind
    table_name: str | None = None  # якорь-таблица (null = общий документ); не фильтр
    # Таблицы, упомянутые в содержимом: права проверяются по ВСЕМ (NFR-4 ACL-leak=0,
    # ревью S06 Critical-1). Пусто = общий документ без табличных ссылок.
    referenced_tables: list[str] = Field(default_factory=list)
    content: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)  # ключ идемпотентного upsert
    score: float | None = None  # близость к запросу — пишет адаптер при поиске

    @model_validator(mode="after")
    def _anchor_is_declared(self) -> "KnowledgeDocument":
        # Defense-in-depth (ревью-2): якорь без объявления сделал бы документ
        # публичным при '{}' — таблица-якорь обязана быть в referenced_tables
        if self.table_name is not None and self.table_name not in self.referenced_tables:
            raise ValueError(
                f"table_name={self.table_name!r} не объявлена в referenced_tables "
                f"{self.referenced_tables} — пустой список сделал бы документ публичным"
            )
        return self


@runtime_checkable
class VectorStore(Protocol):
    """Векторная память: идемпотентная индексация + поиск с фильтром прав.

    `upsert` — параллельные списки (documents[i] ↔ embeddings[i]); запись
    идемпотентна по source_ref (повторная индексация обновляет, не дублирует).
    `search` — dense-поиск: top_k документов, у которых права ctx.user_id
    покрывают все referenced_tables.
    """

    def upsert(self, documents: list[KnowledgeDocument], embeddings: list[list[float]]) -> int: ...

    def search(
        self, query_embedding: list[float], *, ctx: RunContext, top_k: int
    ) -> list[KnowledgeDocument]: ...

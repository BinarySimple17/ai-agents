"""Порт `VectorStore` — контракт векторной памяти знаний (S00 стык; ADR-003 §4).

Поиск — **dense-only** на вехе V2 (решение владельца 2026-11-20, D-026 доп. 2):
cosine distance по эмбеддингу; механика качества (гибрид FTS+RRF, шумовой корпус,
реранкер) — веха V3 (BL-007), порт абстрагирует способ поиска.

Права — deny-by-default (ADR-005 «граница знаний = граница прав», NFR-4
ACL-leak = 0): фильтр прав живёт в самом запросе поиска (джойн с правами,
ADR-003 §4), не пост-фильтрация выдачи — документ недоступной таблицы не
участвует в поиске вообще.

DTO стыка — pydantic и живут здесь (прецедент: `sql_executor.py`).
"""

from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

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


class KnowledgeDocument(BaseModel):
    """Документ векторной памяти (строка agent_knowledge)."""

    model_config = ConfigDict(frozen=True)

    doc_kind: DocKind
    table_name: str | None = None  # null = общий документ (не привязан к таблице)
    content: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)  # ключ идемпотентного upsert
    score: float | None = None  # близость к запросу — пишет адаптер при поиске


@runtime_checkable
class VectorStore(Protocol):
    """Векторная память: идемпотентная индексация + поиск с фильтром прав.

    `upsert` — параллельные списки (documents[i] ↔ embeddings[i]); запись
    идемпотентна по source_ref (повторная индексация обновляет, не дублирует).
    `search` — dense-поиск: top_k документов, чьи таблицы разрешены ctx.user_id.
    """

    def upsert(
        self, documents: list[KnowledgeDocument], embeddings: list[list[float]]
    ) -> int: ...

    def search(
        self, query_embedding: list[float], *, ctx: RunContext, top_k: int
    ) -> list[KnowledgeDocument]: ...

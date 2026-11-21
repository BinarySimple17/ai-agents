"""Порт `VectorStore` — контракт векторной памяти знаний (S00 стык; ADR-003 §4).

Поиск — **гибридный** (S26, feat-021; канон D-026 с дополнениями, BL-007):
(A) dense — pgvector cosine; (B) ключевая нога — PostgreSQL FTS (ts_rank по
generated-колонке content_tsv) по тексту запроса. Слияние — взвешенный RRF
внутри адаптера; порт абстрагирует способ поиска (сигнатура несёт оба входа:
`query` — текст для FTS-ноги, `query_embedding` — вектор для dense; прецедент
расширения сигнатуры — N1/S06, синк строки порта в S00 тем же коммитом).

Права — deny-by-default (ADR-005 «граница знаний = граница прав», NFR-4
ACL-leak = 0): фильтр прав живёт в самом запросе поиска (джойн с правами,
ADR-003 §4), не пост-фильтрация выдачи. Инвариант — по **всем таблицам,
упомянутым в документе** (`referenced_tables`), а не только по таблице-якорю:
кураторский few-shot «выручка по кампаниям» ссылается на orders И campaigns —
пользователь без прав на campaigns документ не видит (ревью S06 Critical-1).

Запись — replace-семантика полной коллекции (BL-009 M3, ruling N1 ARCH-GATE
S26): upsert всех переданных документов + транзакционное удаление устаревших
source_ref переиндексируемых неймспейсов (префикс до ':') в одной транзакции.
Контент нормализуется при записи (снятие U+0301, ё→е — D-026 доп. 1(2)).

Ошибки стыка типизированы (прецедент `SQLExecutionError`): `VectorStoreError` —
сбой хранилища (соединение/БД); нарушение контракта вызова (размерность,
параллельные списки, пустой текст запроса) — ValueError.

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

# Префикс source_ref у RELATIONSHIP-документов индексатора (knowledge_sources):
# «rel:{edge_id}» — единственный маппинг «документ поиска → ребро реестра»;
# узел plan_joins читает им retrieval-подтверждение рёбер (ревью M2: константа
# в порту — источник истины для индексатора и потребителя, дубль дрейфует молча)
RELATIONSHIP_SOURCE_REF_PREFIX = "rel:"


class DocKind(StrEnum):
    """Тип документа знаний: 4 канонических из feat-006 (ADR-003 §4) + шумовой
    корпус-дистрактор feat-021 (S26; D-026: ранжируется вместе с реальными,
    в промпт не попадает — фильтр при сборке контекста)."""

    DDL = "ddl"  # DDL таблицы с комментариями колонок
    RELATIONSHIP = "relationship"  # связь таблиц с семантикой (meaning/usage_notes)
    GLOSSARY = "glossary"  # бизнес-термин → определение
    FEW_SHOT = "few_shot"  # кураторский пример «вопрос → SQL» (автонаполнение запрещено)
    NOISE = "noise"  # шумовой корпус-дистрактор (генератор, фиксированный сид)


class VectorStoreError(Exception):
    """Сбой векторной памяти (соединение/БД): человекочитаемое сообщение, не traceback."""


class KnowledgeDocument(BaseModel):
    """Документ векторной памяти (строка agent_knowledge)."""

    model_config = ConfigDict(frozen=True)

    doc_kind: DocKind
    table_name: str | None = None  # якорь-таблица (null = общий документ); не фильтр
    # Таблицы, упомянутые в содержимом: права проверяются по ВСЕМ (NFR-4 ACL-leak=0,
    # ревью S06 Critical-1). Пусто = общий документ без табличных ссылок. Шум
    # объявляет таблицы, которые упоминает (ruling N2 S26: guard универсален).
    referenced_tables: list[str] = Field(default_factory=list)
    content: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)  # ключ replace-идемпотентного upsert
    # Релевантность от адаптера при поиске: гибрид — RRF-оценка (порядка
    # 1/(RRF_K + rank), не cosine); consumers используют порядок, не шкалу
    score: float | None = None

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
    """Векторная память: replace-идемпотентная индексация + гибридный поиск с
    фильтром прав.

    `upsert` — параллельные списки (documents[i] ↔ embeddings[i]); replace-
    семантика полной коллекции: все переданные документы пишутся (update по
    source_ref), устаревшие source_ref переиндексируемых неймспейсов удаляются
    в той же транзакции (BL-009 M3). `search` — гибрид dense+FTS: top_k
    документов, у которых права ctx.user_id покрывают все referenced_tables.
    Диапазон выдачи: каждая нога отдаёт не больше MAX_LEG_DEPTH строк, поэтому
    top_k > 2×MAX_LEG_DEPTH молча недостижим (выдача короче запрошенного);
    канонные вызовы top_k ≤ 8 (S24) при глубине ноги 50 (m17 ревью S26).
    """

    def upsert(self, documents: list[KnowledgeDocument], embeddings: list[list[float]]) -> int: ...

    def search(
        self,
        query: str,
        query_embedding: list[float],
        *,
        ctx: RunContext,
        top_k: int,
    ) -> list[KnowledgeDocument]: ...

"""Индексация знаний в pgvector — точка запуска `make index-knowledge`.

Офлайн-операция куратора (S06 Task 6; feat-006/feat-021; hw-plan §3): собирает
4 канонических типа документов (ADR-003 §4, автонаполнение запрещено) + шумовой
корпус-дистрактор (S26 Task 4, D-026 — генератор с фиксированным сидом,
`noise:`-неймспейс), эмбеддирует (bge-m3) и пишет в agent_knowledge ОДНОЙ
replace-транзакцией (ruling N1 ARCH-GATE S26 / BL-009 M3: upsert всех +
удаление устаревших source_ref переиндексируемых неймспейсов). Запись — под
сервисным POSTGRES_DSN (ruling W1 ARCH-GATE S06: runtime-роли пишут не могут),
поиск агента — под agent_planner.

Список бизнес-таблиц — SoT каталог `agent_data_sources` (ADR-011; BL-009 M1):
новая таблица попадает в DDL-доки добавлением строки в каталог + CREATE TABLE
в миграцию, без правки кода. Повторный запуск обновляет содержимое и векторы;
после правки glossary.md/few_shot.md/миграций просто перезапустить.
"""

import sys
from pathlib import Path

from text_to_sql_agent.application.ports.vector_store import DocKind, KnowledgeDocument
from text_to_sql_agent.composition.container import Container, build_container
from text_to_sql_agent.infrastructure.memory.knowledge_sources import (
    business_tables_from_catalog,
    check_declared_references,
    ddl_documents,
    markdown_documents,
    relationship_documents,
)
from text_to_sql_agent.infrastructure.memory.noise_corpus import generate_noise_corpus

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION_0001 = _REPO_ROOT / "src" / "migrations" / "versions" / "20261118_0001_schema_v15.py"
_GLOSSARY_MD = _REPO_ROOT / "docs" / "glossary.md"
_FEWSHOT_MD = _REPO_ROOT / "docs" / "few_shot.md"

# Батч эмбеддинга: LM Studio принимает списки, но большой батч на одну просьбу
# рискует таймаутом шлюза (llm_request_timeout_s); 16 чанков — безопасный размер
_EMBED_BATCH = 16


def build_documents(container: Container) -> list[KnowledgeDocument]:
    """Канонические документы (ADR-003 §4) + шумовой корпус (S26, D-026).

    Порядок — кураторские знанием первыми, шум после (Task 4): неймспейсы одной
    replace-коллекции; шум участвует в ранжировании, в промпт не попадает.
    """
    model_version = container.settings.embedding_model
    service_dsn = container.settings.postgres_dsn.get_secret_value()
    # SoT бизнес-таблиц — каталог источников (BL-009 M1), не константа
    business_tables = business_tables_from_catalog(service_dsn)
    docs = ddl_documents(
        _MIGRATION_0001, model_version=model_version, business_tables=business_tables
    )
    docs += relationship_documents(service_dsn, model_version=model_version)
    docs += markdown_documents(
        _GLOSSARY_MD,
        doc_kind=DocKind.GLOSSARY,
        model_version=model_version,
        business_tables=business_tables,
    )
    docs += markdown_documents(
        _FEWSHOT_MD,
        doc_kind=DocKind.FEW_SHOT,
        model_version=model_version,
        business_tables=business_tables,
    )
    docs += generate_noise_corpus(business_tables, model_version=model_version)
    # guard прав (C1): содержимое ⊆ объявленные таблицы — универсален, шум
    # объявляет таблицы по упоминаниям (ruling N2 ARCH-GATE S26)
    check_declared_references(docs, business_tables=business_tables)
    return docs


def main() -> int:
    container = build_container(knowledge_writer=True)
    docs = build_documents(container)
    vectors: list[list[float]] = []
    for start in range(0, len(docs), _EMBED_BATCH):
        batch = docs[start : start + _EMBED_BATCH]
        vectors.extend(container.llm.embed([doc.content for doc in batch]))
    written = container.vector_store.upsert(docs, vectors)
    counts = {kind.value: sum(1 for d in docs if d.doc_kind == kind) for kind in DocKind}
    print(f"Проиндексировано {written} документов (модель {container.settings.embedding_model}):")
    for kind, count in counts.items():
        if count:
            print(f"  {kind}: {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

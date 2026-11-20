"""Индексация кураторских знаний в pgvector — точка запуска `make index-knowledge`.

Офлайн-операция куратора (S06 Task 6; feat-006; hw-plan §3): собирает 4 типа
документов (ADR-003 §4, автонаполнение запрещено), эмбеддирует (bge-m3) и
идемпотентно (source_ref) пишет в agent_knowledge. Запись — под сервисным
POSTGRES_DSN (ruling W1 ARCH-GATE S06: runtime-роли пишут не могут), поиск
агента — под agent_planner. Отчёт — сколько документов каждого типа.

Повторный запуск обновляет содержимое и векторы (ON CONFLICT DO UPDATE);
после правки glossary.md/few_shot.md/миграций просто перезапустить.
"""

import sys
from pathlib import Path

from text_to_sql_agent.application.ports.vector_store import DocKind, KnowledgeDocument
from text_to_sql_agent.composition.container import Container, build_container
from text_to_sql_agent.infrastructure.memory.knowledge_sources import (
    check_declared_references,
    ddl_documents,
    markdown_documents,
    relationship_documents,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION_0001 = _REPO_ROOT / "src" / "migrations" / "versions" / "20261118_0001_schema_v15.py"
_GLOSSARY_MD = _REPO_ROOT / "docs" / "glossary.md"
_FEWSHOT_MD = _REPO_ROOT / "docs" / "few_shot.md"

# Батч эмбеддинга: LM Studio принимает списки, но большой батч на одну просьбу
# рискует таймаутом шлюза (llm_request_timeout_s); 16 чанков — безопасный размер
_EMBED_BATCH = 16


def build_documents(container: Container) -> list[KnowledgeDocument]:
    """4 типа документов (ADR-003 §4): DDL, связи, глоссарий, few-shot."""
    model_version = container.settings.embedding_model
    service_dsn = container.settings.postgres_dsn.get_secret_value()
    docs = ddl_documents(_MIGRATION_0001, model_version=model_version)
    docs += relationship_documents(service_dsn, model_version=model_version)
    docs += markdown_documents(_GLOSSARY_MD, doc_kind=DocKind.GLOSSARY, model_version=model_version)
    docs += markdown_documents(_FEWSHOT_MD, doc_kind=DocKind.FEW_SHOT, model_version=model_version)
    check_declared_references(docs)  # guard прав: содержимое ⊆ объявленные таблицы (C1)
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
        print(f"  {kind}: {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

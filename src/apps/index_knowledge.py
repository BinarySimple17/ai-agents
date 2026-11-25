"""Индексация знаний в pgvector — точка запуска `make index-knowledge`.

Офлайн-операция куратора (S06 Task 6; feat-006/feat-021; hw-plan §3): собирает
4 канонических типа документов (ADR-003 §4, автонаполнение запрещено) + шумовой
корпус-дистрактор (S26 Task 4, D-026 — генератор с фиксированным сидом,
`noise:`-неймспейс), эмбеддирует (bge-m3) и пишет в agent_knowledge ОДНОЙ
replace-транзакцией (ruling N1 ARCH-GATE S26 / BL-009 M3: upsert всех +
удаление устаревших source_ref переиндексируемых неймспейсов). Запись — под
сервисным POSTGRES_DSN (ruling W1 ARCH-GATE S06: runtime-роли пишут не могут),
поиск агента — под agent_planner.

Сборка документов — `composition.knowledge_docs` (S30 Task 2, canon-check F1:
apps не импортирует infrastructure напрямую); здесь — только цикл эмбеддинга
и запись через container.

Список бизнес-таблиц — SoT каталог `agent_data_sources` (ADR-011; BL-009 M1):
новая таблица попадает в DDL-доки добавлением строки в каталог + CREATE TABLE
в миграцию, без правки кода. Повторный запуск обновляет содержимое и векторы;
после правки glossary.md/few_shot.md/миграций просто перезапустить.
"""

import sys

from text_to_sql_agent.application.ports.vector_store import DocKind
from text_to_sql_agent.composition.container import build_container
from text_to_sql_agent.composition.knowledge_docs import build_documents

# Батч эмбеддинга: LM Studio принимает списки, но большой батч на одну просьбу
# рискует таймаутом шлюза (llm_request_timeout_s); 16 чанков — безопасный размер
_EMBED_BATCH = 16


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

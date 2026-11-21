"""Синк графовой памяти — точка запуска `make sync-graph` (S07 Task 2; feat-007).

Офлайн-операция (как `make index-knowledge`, ruling N2 S06): пересобирает
снапшот реестра рёбер в Neo4j — «активные строки agent_relationships → вершины
и рёбра» (ruling R1 canon-check: реестр — единственный источник рёбер; FK
каталога — генератор кандидатов, НЕ ребро), маркирует ревизию graph_version
(ADR-006 §4.3). Чтение реестра — под ролью agent_planner (ruling BL-003 п.3),
запись — в Neo4j (не PG). Идемпотентен: повторный запуск даёт тот же граф.

Запускать после правки рёбер реестра (миграции/владелец) и ПЕРЕД
интеграционными тестами путей (Ловушка-1: пустой граф молча роняет тесты).
"""

import sys

from text_to_sql_agent.composition.container import build_container


def main() -> int:
    container = build_container()
    try:
        report = container.graph_store.sync()
    finally:
        container.graph_store.close()
    print(
        f"Граф синхронизирован: {report.nodes} вершин, {report.edges} рёбер, "
        f"graph_version={report.graph_version}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Живой прогон узла plan_joins — гибридный поиск ДЗ-04 (S09 Task 5, layer-3).

Mini-прогон через Container по образцу S06/S25 (не FSM — подключение узла в
маршрут — V4/S04): вопрос → retrieval-хиты (ребро + usage_notes) → путь по
графу → JoinPlan с допущениями. Демо-вопрос вехи: «клиенты, пришедшие из
рекламы» — semantic находит ребро orders↔campaigns и допущение first-touch,
граф даёт путь customers↔orders↔campaigns, план несёт допущение в результат.

Права: права пользователя читаются из таблиц настроек (миграция 0003) —
минимальный read-аналог resolve_access для standalone-прогона; FSM (V4/S05)
подменит полноценным узлом. Денай-по-дефолту: нет строк прав → пустой
подграф → исход «уточнение».

Запуск: `uv run python -m apps.plan_joins_demo "клиенты, пришедшие из рекламы" [--user u_logist]`.
Перед прогоном: make infra-up && make sync-graph && make index-knowledge.
"""

import argparse
import sys
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from text_to_sql_agent.application.services.plan_joins_service import PlanJoinsResult, PlanOutcome
from text_to_sql_agent.composition.container import build_container
from text_to_sql_agent.domain.entities import RunContext

# миграция 0003: logistics_viewer — customers/orders/order_items/campaigns/products
_DEFAULT_USER = "u_logist"


def _permissions(planner_dsn: str, user_id: str) -> dict[str, list[str]]:
    """Права пользователя {table: [columns]} из настроек (S00 контракт 3).

    Standalone-аналог resolve_access (S05/V4): read под agent_planner
    (SELECT на agent_permissions/agent_user_roles — миграция 0002).
    """
    with psycopg.connect(planner_dsn, row_factory=dict_row) as conn:
        rows = conn.execute(
            "SELECT p.table_name, p.columns FROM agent_permissions p "
            "JOIN agent_user_roles r ON r.role = p.role WHERE r.user_id = %s "
            "ORDER BY p.table_name",
            (user_id,),
        ).fetchall()
    permissions: dict[str, list[str]] = {}
    for row in rows:
        permissions.setdefault(row["table_name"], []).extend(row["columns"] or [])
    return permissions


def _print_result(question: str, user_id: str, result: PlanJoinsResult) -> None:
    print(f"Вопрос: «{question}» (пользователь {user_id})")
    if result.candidate_tables:
        hits = ", ".join(result.hit_edge_ids) if result.hit_edge_ids else "—"
        print(
            f"Retrieval: кандидаты-таблицы {result.candidate_tables}; "
            f"рёбра с релевантностью: {hits}"
        )
    if result.outcome is PlanOutcome.PLAN and result.plan is not None:
        plan = result.plan
        chain = " ↔ ".join(plan.tables)
        print(f"Исход: PLAN (graph_version={result.graph_version})")
        print(f"Путь: {chain}")
        for number, edge in enumerate(plan.edges, 1):
            print(
                f"  джойн {number}: {edge.from_table}({','.join(edge.from_columns)}) "
                f"{edge.join_operator} {edge.to_table}({','.join(edge.to_columns)}) "
                f"[{edge.rel_id}, {edge.join_type}]"
            )
        if plan.assumptions:
            print("Допущения (usage_notes рёбер):")
            for note in plan.assumptions:
                print(f"  - {note}")
        if plan.mandatory_predicates:
            print(f"Skeleton-якоря (mandatory_predicates): {plan.mandatory_predicates}")
    else:
        print(f"Исход: {result.outcome.value}")
        if result.error:
            print(f"Диагностика: {result.error}")
        if result.clarifying_question:
            print(f"Уточняющий вопрос: {result.clarifying_question}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Живой прогон узла plan_joins (ДЗ-04, гибридный поиск)"
    )
    parser.add_argument("question", help="бизнес-вопрос на естественном языке")
    parser.add_argument(
        "--user", default=_DEFAULT_USER, help=f"демо-пользователь (дефолт {_DEFAULT_USER})"
    )
    args = parser.parse_args()

    container = build_container()
    # Пер-запросный аккумулятор бюджета — с нуля (ruling Q7 ARCH-GATE S09:
    # шов эскалации считает остаток от капа запроса)
    container.llm.reset_request_budget()
    ctx = RunContext(trace_id=f"demo-plan-joins-{uuid4().hex[:8]}", user_id=args.user)
    try:
        permissions = _permissions(container.settings.agent_planner_dsn, args.user)
        if not permissions:
            print(f"Пользователь {args.user} не имеет прав — подграф пуст (deny-by-default)")
            sys.exit(2)
        result = container.plan_joins_service.plan(args.question, ctx=ctx, permissions=permissions)
        _print_result(args.question, args.user, result)
    finally:
        container.graph_store.close()


if __name__ == "__main__":
    main()

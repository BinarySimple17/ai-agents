"""Бенч retrieval: hit@5 dense-only vs гибрид — baseline для апгрейдов (S26 Task 6).

Канон D-026 (+доп. 2): смоук-метрика на 10–20 русских вопросах с известными
целевыми документами; фиксируется в отчёте сессии и model-card §1, с ней
сравниваются будущие апгрейды (реранкер — кандидат при недостаточном baseline).
Инвариант D-026 доп. 1(1): **гибрид ≥ dense** (α_dense=0.9 сделал гибрид ≥ dense
на корпусах-якорях) — если ниже, бенч завершается кодом 1 с разбором промахов
(стоп-сигнал: тюнинг α_dense по канону, реранкер — только решение владельца).

Методика: режим dense-only — тот же адаптер с α_dense=1.0 (вклад FTS-ноги
нулевой, порядок = чистому dense-ранжированию, hit@5 эквивалентен; контейнер
собирается build_container(rrf_alpha_dense=1.0) — сборка зависимостей только
в composition, I3 ревью S26); гибрид — канонное α_dense=0.9. top_k =
RETRIEVAL_TOP_K (8, S24): бенч меряет hit@5 по 8 выданным — целевые документы
в top-8, выдача не «удлиняется» ради метрики (Ловушки-7). Пользователь —
u_logist (самые широкие права сида: customers/campaigns/orders/order_items/
products): шумовой корпус объявляет таблицы по упоминаниям (ruling N2), цели
датасета лежат в этих таблицах; HR-цели (bonuses/employees) в датасет не
входят — они вне мира u_logist. Реальный шлюз (эмбеддер bge-m3) — ручная
Layer-3 операция, в тестах не вызывается (S00 правило 7).
"""

import sys

from apps.cli import RETRIEVAL_TOP_K  # S24: 8 документов в выдаче — единый источник
from text_to_sql_agent.composition.container import build_container
from text_to_sql_agent.domain.entities import RunContext

BENCH_USER_ID = "u_logist"  # самые широкие права сида 0003 (см. докстринг)
HIT_K = 5  # метрика hit@5: цель в первых HIT_K позициях выдачи (канон D-026)

# Датасет: (вопрос, целевые source_ref) — цели видны BENCH_USER_ID; вопрос
# словами пользователя, не дословно заголовком документа (иначе dense всегда
# попадает — тривиальный замер, D-026)
_DATASET: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Что такое выручка и как её правильно считать?", ("glossary:выручка",)),
    ("Теряются ли заказы, не попавшие ни в одну кампанию?", ("glossary:заказ вне кампании",)),
    ("Как правильно фильтровать клиентов из России?", ("glossary:клиент из РФ",)),
    ("Какие значения страны встречаются в данных клиентов?", ("glossary:справочник стран",)),
    ("Какие бывают статусы у заказов?", ("glossary:справочник статусов заказа",)),
    ("Из чего состоит позиция заказа?", ("glossary:позиция заказа",)),
    ("Сколько всего заказов в базе?", ("few_shot:всего-заказов",)),
    ("Сколько заказов у клиентов из РФ?", ("few_shot:заказы-из-РФ",)),
    ("Покажи выручку по рекламным кампаниям", ("few_shot:выручка-по-кампаниям",)),
    ("Кто самые активные клиенты по числу заказов?", ("few_shot:топ-клиентов",)),
    ("Как устроена таблица заказов на уровне БД?", ("ddl:orders",)),
    ("Какая структура у таблицы клиентов?", ("ddl:customers",)),
    ("Как связаны заказы и клиенты?", ("rel:e_customers_orders",)),
    ("Как товары попадают в заказы?", ("rel:e_orders_items", "rel:e_products_items")),
)


def _hit_rank(docs: list, targets: tuple[str, ...]) -> int | None:
    """Позиция (1-based) первой цели в top-HIT_K выдачи; None — промах hit@5."""
    for rank, doc in enumerate(docs[:HIT_K], start=1):
        if doc.source_ref in targets:
            return rank
    return None


def main() -> int:
    container = build_container()  # гибрид: канонное α_dense=0.9 (D-026)
    ctx = RunContext(trace_id="bench-retrieval", user_id=BENCH_USER_ID)
    # dense-only: тот же контур с α=1.0 — сборка ТОЛЬКО через composition (I3 ревью)
    container_dense = build_container(rrf_alpha_dense=1.0)
    modes = ("dense", "hybrid")
    hits: dict[str, int] = {mode: 0 for mode in modes}
    misses: dict[str, list[str]] = {mode: [] for mode in modes}
    print(
        f"Бенч retrieval: hit@5 на {len(_DATASET)} вопросах (top_k={RETRIEVAL_TOP_K}, "
        f"пользователь {BENCH_USER_ID}, корпус = кураторский + шумовой)"
    )
    print(f"{'вопрос':<52} {'dense':>6} {'гибрид':>7}")
    for question, targets in _DATASET:
        [vector] = container.llm.embed([question])
        ranks: dict[str, int | None] = {}
        for mode, store in (
            ("dense", container_dense.vector_store),
            ("hybrid", container.vector_store),
        ):
            docs = store.search(question, vector, ctx=ctx, top_k=RETRIEVAL_TOP_K)
            rank = _hit_rank(docs, targets)
            ranks[mode] = rank
            if rank is not None:
                hits[mode] += 1
            else:
                misses[mode].append(question)
        print(f"{question:<52} {str(ranks['dense']):>6} {str(ranks['hybrid']):>7}")
    total = len(_DATASET)
    print(f"\nИтог hit@{HIT_K}: dense {hits['dense']}/{total}, гибрид {hits['hybrid']}/{total}")
    if hits["hybrid"] < hits["dense"]:
        print("\nСТОП: hit@5(гибрид) < hit@5(dense) — инвариант D-026 нарушен.")
        print("Разбор промахов гибрида:")
        for question in misses["hybrid"]:
            print(f"  - {question}")
        print("Тюнинг α_dense по канону D-026; реранкер — только решение владельца.")
        return 1
    for question in misses["hybrid"]:
        print(f"промах (оба режима): {question}")
    print("Инвариант D-026 выполняется: гибрид ≥ dense — baseline записан в model-card §1.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""CLI — FSM-прогон (ДЗ-05, feat-004): «вопрос → FSM → исход + контракт ответа».

Тул-контур ДЗ-02/03 (run_sql, merged-flow, `--plain`) удалён — BL-048/D-041
(S39): единственный продуктовый контур — state machine на LangGraph
(``_run_fsm``). Флаг ``--fsm`` сохранён для совместимости артефактов hw-05
(дефолт теперь тоже FSM); identity — проверенный токен (--token или DEV_TOKEN
из .env, S31) — verify внутри ``_run_fsm`` ДО первого LLM-вызова.

stdout — маршрут и исход (D-022); служебные этапы — в stderr через
ConsoleTracer (``--quiet`` глушит stderr, не ответ). Уточняющий вопрос
(interrupt — S08/feat-008) печатается и ждёт ответ со stdin; resume тем же
thread_id продолжает ТОТ ЖЕ прогон (кап раундов уточнения — код).

Retrieval-механика промпта (``format_retrieval_block``, ``RETRIEVAL_*``)
сохранена здесь как утилита сборки контекста знаний — потребители вне CLI:
``apps/bench_retrieval`` (RETRIEVAL_TOP_K) и ``integration/test_hybrid_memory``
(format_retrieval_block). Промпт тул-контура и его карта демо-схемы удалены
вместе с контуром (BL-048): FSM наполняет узлы из pgvector с фильтром прав.
"""

import argparse
import sys
import uuid

from text_to_sql_agent.application.ports.auth_provider import AuthenticationError
from text_to_sql_agent.application.ports.vector_store import DocKind, KnowledgeDocument
from text_to_sql_agent.composition.container import Container, build_container

# --- Контекст знаний (S06, hw-plan §3; потребители вне CLI) ---
# Топ-8 документов в контекст поиска. «Ранжирование топ-5» в каноне (ADR-003/006,
# FR-2) — про пути графа в plan_joins, НЕ про retrieval: прежний комментарий
# мицитировал ADR. Находка S24 #1(а): при k=5 на корпусе из 21 документа вопросы
# о 3-табличной цепочке (customers+orders+campaigns) теряли DDL-звенья — для
# «...из Кзахстана... всего» ddl:orders оказался 7-м, и модель честно ответила
# «таблицы нет» (deny-by-default отработал на неполном контексте). Структура
# такого вопроса требует DDL всех таблиц цепочки + ребро + few-shot + глоссарий
# — 7–8 документов. Блок по-прежнему ограничен капом RETRIEVAL_CONTEXT_MAX_CHARS
# (≈2К токенов, резка по границе документа), поэтому рост k не раздувает контекст.
RETRIEVAL_TOP_K = 8
# Бюджет retrieval-блока ≈2К токенов (hw-plan §3): кап по символам — 3.5 симв/токен
# по токенизатору bge-m3; для генеративной модели это верхняя оценка (кириллица
# у qwen дорожe, ≈2–3К токенов), т.е. гарантия «не раздуть контекст», не точный
# счётчик. Блок режется по границе документа (чанк посередине теряет якорь)
RETRIEVAL_CONTEXT_TOKENS = 2000
_CHARS_PER_TOKEN = 3.5  # эмпирическая оценка для кириллицы (токенизатор bge-m3)
RETRIEVAL_CONTEXT_MAX_CHARS = int(RETRIEVAL_CONTEXT_TOKENS * _CHARS_PER_TOKEN)
RETRIEVAL_SEPARATOR = "\n---\n"
RETRIEVAL_BLOCK_HEADER = (
    "Контекст знаний (документы, разрешённые твоим правам; DDL, связи таблиц, "
    "глоссарий, примеры «вопрос → SQL»):"
)
RETRIEVAL_EMPTY_BLOCK = "\nКонтекст знаний пуст: релевантных разрешённых документов нет."

# Кап раундов уточнения в CLI interrupt-цикле (S08): классификатор даёт РОВНО
# ОДИН вопрос за визит; повторные паузы возможны только через равных кандидатов
# планера (S10) — кап = per-node предохранителю ≤3 (S00 стык 1)
_FSM_CLARIFY_ROUNDS = 3


def format_retrieval_block(
    docs: list[KnowledgeDocument], *, max_chars: int = RETRIEVAL_CONTEXT_MAX_CHARS
) -> str:
    """Retrieval-блок системного промпта: заголовок + чанки через ---, кап ≈2К токенов.

    Кап — по границе документа: чанк, разрезанный посередине, теряет якорь-
    заголовок («[таблица: orders] …») и смысл (ACCURACY §6.2). Документы уже
    отфильтрованы по правам в SQL поиска — блок не пост-фильтруется. Шумовой
    корпус (doc_kind='noise', D-026) участвует в ранжировании, но знанием не
    является — фильтр при сборке контекста (отдельный слой от поиска, S26).
    """
    chunks: list[str] = []
    total = len(RETRIEVAL_BLOCK_HEADER)
    for doc in docs:
        if doc.doc_kind is DocKind.NOISE:
            continue  # дистрактор ранжируется вместе с реальными, в промпт не попадает
        if total + len(RETRIEVAL_SEPARATOR) + len(doc.content) > max_chars:
            break  # бюджет ≈2К токенов исчерпан — остальные документы не влезают целиком
        chunks.append(doc.content)
        total += len(doc.content) + len(RETRIEVAL_SEPARATOR)
    if not chunks:
        return RETRIEVAL_EMPTY_BLOCK
    return "\n" + RETRIEVAL_BLOCK_HEADER + "\n" + RETRIEVAL_SEPARATOR.join(chunks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Text-to-SQL CLI: FSM-прогон (LangGraph). Тул-контур ДЗ-02/03 "
            "удалён (BL-048/D-041) — единственный контур исполнения FSM"
        )
    )
    parser.add_argument("question", nargs="+", help="бизнес-вопрос")
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="глушить этапы работы агента в stderr (ответ остаётся на stdout)",
    )
    parser.add_argument(
        "--fsm",
        action="store_true",
        help="FSM-прогон (S04/S05/S08): единственный контур после удаления "
        "тул-контура (BL-048/D-041); флаг сохранён для совместимости артефактов "
        "hw-05 — дефолт тоже FSM",
    )
    parser.add_argument(
        "--token",
        default="",
        help="dev-токен identity (S31: обязателен для любого SQL-контура); "
        "без него берётся DEV_TOKEN из .env; verify не обходится (ADR-005, "
        "ruling R4 S05)",
    )
    args = parser.parse_args(argv)
    question = " ".join(args.question)
    container = build_container(console_trace=not args.quiet)
    token = args.token or container.settings.dev_token.get_secret_value()
    try:
        return _run_fsm(container, question, token=token)
    finally:
        # Графовый драйвер создаётся при сборке контейнера (bolt, пул сокетов) —
        # FSM читает пути через порты (plan_joins), закрываем аккуратно
        container.graph_store.close()
        # Checkpointer (S08): пул redis-соединений сессий interrupt; фейк в
        # тестах может не иметь ни поля, ни close() — гигиена по наличию
        checkpointer_close = getattr(getattr(container, "checkpointer", None), "close", None)
        if callable(checkpointer_close):
            checkpointer_close()


def _print_final_answer(result: object) -> None:
    """Финальный payload исхода (S15, FSMResult.answer): ответ + контракт feat-015.

    done — текст + SQL + допущения/usage_notes + флаги + стоимость; degrade —
    текст отказа + причина. Печатается только содержимое final_answer — S9-
    инварианты текста гарантированы узлом (санитайзер/шаблоны), CLI ничего не
    переформулирует. ASCII-вывод (ревью S04 M3): без «→» в строках печати.
    """
    answer = getattr(result, "answer", None) or {}
    text = answer.get("text")
    if text:
        print("Ответ: " + str(text))
    sql = answer.get("sql")
    if sql:
        print("SQL: " + str(sql))
    notes = [*(answer.get("assumptions") or []), *(answer.get("usage_notes") or [])]
    for note in notes:
        print("Допущение: " + str(note))
    flags = [
        label
        for label, value in (
            ("усечение", answer.get("truncated")),
            ("approximate", answer.get("approximate")),
            ("неполный", answer.get("incomplete")),
        )
        if value
    ]
    if flags:
        print("Флаги: " + ", ".join(flags))
    cost = answer.get("cost_rub")
    if cost is not None:
        print("Стоимость: " + format(float(cost), ".4f") + " руб.")


def _run_fsm(container: Container, question: str, *, token: str) -> int:
    """FSM-прогон (S04/S05/S08): verify токена → порядок обхода узлов + исход.

    Identity — только из проверенного токена (ADR-005, ruling R4 ARCH-GATE S05):
    AuthProvider.verify ДО graph.invoke, verified user_id кладётся в state;
    отказ аутентификации — вежливый отказ без LLM-вызова и деталей конфигурации
    (S9-политика). Порядок обхода — из state.visited_nodes (ruling R3), исход —
    различимый payload FSMResult.outcome (state-agent v1.5). Уточняющий вопрос
    (interrupt — S08/feat-008): печатается и ждёт ответ со stdin; resume тем же
    thread_id продолжает ТОТ ЖЕ прогон (S00 стык 1, ruling R6).
    """
    try:
        user_id = container.auth_provider.verify(token)
    except AuthenticationError as exc:
        print("Исход: rejected")
        print("Ошибка: " + str(exc))
        return 1
    thread_id = uuid.uuid4().hex  # сессия checkpointer'а: run и resume — один id
    result = container.answer_question.run(question, user_id=user_id, thread_id=thread_id)
    for _ in range(_FSM_CLARIFY_ROUNDS):
        if not result.paused:
            break
        print("Уточнение: " + (result.clarify_question or "(уточняющий вопрос)"))
        try:
            answer = input("Ответ: ")
        except (EOFError, KeyboardInterrupt):
            # Ревью S08 Minor-6a: закрытый stdin/Ctrl+C при ожидании ответа —
            # валидный отказ презентационного слоя, не traceback (S00-8)
            print("Исход: clarification")
            print("Ошибка: ответ на уточняющий вопрос не получен — сессия истечёт по TTL")
            return 0
        result = container.answer_question.resume(answer, thread_id=thread_id, question=question)
    else:
        # Защита презентационного слоя (FSM сам ограничен recursion_limit):
        # кап раундов уточнения исчерпан — честный fail, не бесконечный ввод
        print("Исход: fail")
        print("Ошибка: превышено число раундов уточнения (" + str(_FSM_CLARIFY_ROUNDS) + ")")
        return 1
    # ASCII-стрелка: при перенаправлении stdout в файл ru-Windows (cp1251)
    # « → » дал бы UnicodeEncodeError (ревью S04 M3)
    print("Маршрут: " + " -> ".join(result.visited_nodes))
    print("Исход: " + str(result.outcome))
    _print_final_answer(result)
    answer = getattr(result, "answer", None) or {}
    error = result.state.get("error")
    if error and not answer.get("text"):
        # Сообщение отказа — фиксированный текст узла (S9): деталей настройки
        # (host/роль БД) здесь нет, они — только в операторском трейсе (ревью I1).
        # A7/S33: печать только для исходов БЕЗ текста ответа — сырая ошибка БД
        # в stdout не попадает ни через message (фиксированный), ни вторым
        # каналом поверх содержательного отказа
        print("Ошибка: " + str(error.get("message")))
    # Отказы — единообразный non-zero для скриптов (ревью S05 Minor-4 + re-review
    # Minor-2): rejected (отказ на входе) и fail (технический сбой прогона —
    # recursion_limit/missing_outcome); содержательные исходы с ответом
    # (done/degrade/budget_stop/clarification/…) — 0
    return 1 if result.outcome in ("rejected", "fail") else 0


if __name__ == "__main__":
    sys.exit(main())

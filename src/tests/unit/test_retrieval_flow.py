"""Юнит-тесты retrieval-контура CLI ДЗ-03 (S06 Task 7; стрим — S19) — шлюз/хранилище
ТОЛЬКО моки (S00 правило 7).

Проверяются: сборка retrieval-блока (кап ≈2К токенов по границе документа),
передача ctx/top_k в поиск, деградация по правилу 8 (недоступность эмбеддера/
хранилища, пустой/битый ответ — валидный текстовый исход, не traceback).
Ответ модель отдаёт стримом — контракт контура S19 (ruling W1 ARCH-GATE: один
текстовый вызов, дельты → printer). Поведение фильтра прав на живых данных —
integration/test_vector_memory.py.
"""

from apps.cli import (
    DEGRADED_ANSWER,
    LLM_DEGRADED_TEMPLATE,
    RETRIEVAL_CONTEXT_MAX_CHARS,
    RETRIEVAL_SYSTEM_PROMPT,
    RETRIEVAL_TOP_K,
    format_retrieval_block,
    run_retrieval_flow,
)
from tests.unit.fakes import UNIT_CTX
from text_to_sql_agent.application.ports.llm_provider import (
    LLMProviderError,
    LLMRequestError,
    StreamChunk,
    StreamFinal,
    Usage,
)
from text_to_sql_agent.application.ports.vector_store import (
    DocKind,
    KnowledgeDocument,
    VectorStoreError,
)

_QUERY_VECTOR = [0.1] * 1024


class FakeEmbedLLM:
    """Двойник LLMProvider: скриптованный embed + complete_stream (S00 правило 7).

    Ответ отдаётся одним чанком (разбиение на дельты — предмет test_streaming.py);
    printer флоу не передаётся — проверяется текст исхода, не печать.
    """

    def __init__(
        self,
        answer: str = "Ответ по контексту",
        *,
        embed_error: Exception | None = None,
        stream_error: Exception | None = None,
        finish_reason: str | None = "stop",
    ) -> None:
        self._answer = answer
        self._embed_error = embed_error
        self._stream_error = stream_error
        self._finish_reason = finish_reason
        self.stream_messages: list[list[dict]] = []

    def embed(self, texts):
        if self._embed_error is not None:
            raise self._embed_error
        return [_QUERY_VECTOR for _ in texts]

    def complete_stream(self, role, messages, tools=None):
        self.stream_messages.append(messages)
        if self._stream_error is not None:
            raise self._stream_error
        if self._answer:
            yield StreamChunk(text_delta=self._answer)
        yield StreamFinal(
            usage=Usage(tokens_in=10, tokens_out=5),
            finish_reason=self._finish_reason,
            model="fake",
        )


class FakeStore:
    def __init__(self, docs: list[KnowledgeDocument], error: Exception | None = None) -> None:
        self._docs = docs
        self._error = error
        self.calls: list[dict] = []

    def search(self, query_embedding, *, ctx, top_k):
        self.calls.append({"ctx": ctx, "top_k": top_k, "vector": query_embedding})
        if self._error is not None:
            raise self._error
        return self._docs


def _doc(content: str, ref: str, table: str | None = "orders") -> KnowledgeDocument:
    return KnowledgeDocument(
        doc_kind=DocKind.DDL,
        table_name=table,
        referenced_tables=[table] if table else [],  # якорь обязан быть объявлен (C1)
        content=content,
        model_version="test-embedder",
        source_ref=ref,
    )


def test_retrieval_flow_answers_with_context() -> None:
    docs = [_doc("[таблица: orders] DDL", "ddl:orders")]
    llm, store = FakeEmbedLLM("42 заказа"), FakeStore(docs)
    answer = run_retrieval_flow("Сколько заказов?", llm=llm, vector_store=store, ctx=UNIT_CTX)
    assert answer == "42 заказа"
    # Вопрос уходит в эмбеддер, вектор + ctx + top_k — в поиск с фильтром прав
    assert store.calls[0]["ctx"] == UNIT_CTX
    assert store.calls[0]["top_k"] == RETRIEVAL_TOP_K
    assert store.calls[0]["vector"] == _QUERY_VECTOR
    # Retrieval-блок — в системном сообщении, вопрос — в user
    system = llm.stream_messages[0][0]["content"]
    assert "[таблица: orders] DDL" in system
    assert llm.stream_messages[0][1]["content"] == "Сколько заказов?"


def test_retrieval_top_k_pins_eight() -> None:
    # S24 находка #1(а): при k=5 вопросы о 3-табличной цепочке
    # (customers+orders+campaigns) теряли DDL-звенья — для «...из Кзахстана...
    # всего» ddl:orders был 7-м, и модель отказывалась. Топ-8 покрывает
    # структуру (покрытие документов цепочки на живом корпусе — интеграционная
    # зона test_vector_memory/BL-007); контекст по-прежнему ограничен капом
    # по символам (≈2К токенов). Регресс: падает на до-фиксовом k=5.
    assert RETRIEVAL_TOP_K == 8


def test_retrieval_block_caps_on_document_boundary() -> None:
    # Дефолтный бюджет ≈7000 символов: первый документ 4000 влезает, второй — нет
    first, second = _doc("A" * 4000, "ref-1"), _doc("B" * 4000, "ref-2")
    block = format_retrieval_block([first, second])
    assert "A" * 4000 in block
    assert "B" not in block  # второй документ целиком не влезает — не режется посередине
    assert len(block) <= RETRIEVAL_CONTEXT_MAX_CHARS


def test_retrieval_block_budget_default_is_about_2k_tokens() -> None:
    # Константа бюджета hw-plan §3: ≈2К токенов × 3.5 симв/токен
    assert RETRIEVAL_CONTEXT_MAX_CHARS == 7000


def test_retrieval_block_empty_without_docs() -> None:
    block = format_retrieval_block([])
    assert "Контекст знаний пуст" in block


def test_retrieval_flow_degrades_on_embed_failure() -> None:
    llm = FakeEmbedLLM(embed_error=LLMProviderError("шлюз недоступен"))
    answer = run_retrieval_flow("вопрос", llm=llm, vector_store=FakeStore([]), ctx=UNIT_CTX)
    assert answer == LLM_DEGRADED_TEMPLATE.format(error="шлюз недоступен")


def test_retrieval_flow_degrades_on_store_failure() -> None:
    store = FakeStore([], error=VectorStoreError("векторная память недоступна"))
    answer = run_retrieval_flow("вопрос", llm=FakeEmbedLLM(), vector_store=store, ctx=UNIT_CTX)
    assert "Не удалось получить ответ" in answer


def test_retrieval_flow_degrades_on_stream_error() -> None:
    # Сбой стрима ДО первой напечатанной дельты — деградация-ответ (ruling W3)
    llm = FakeEmbedLLM(stream_error=LLMRequestError("шлюз не ответил после 4 попыток"))
    answer = run_retrieval_flow("вопрос", llm=llm, vector_store=FakeStore([]), ctx=UNIT_CTX)
    assert answer.startswith("Не удалось получить ответ")


def test_retrieval_flow_degrades_on_empty_answer() -> None:
    llm, store = FakeEmbedLLM(answer=""), FakeStore([])  # пустой текст — не ответ
    answer = run_retrieval_flow("вопрос", llm=llm, vector_store=store, ctx=UNIT_CTX)
    assert answer == DEGRADED_ANSWER


def test_retrieval_flow_degrades_on_malformed_tool_tag() -> None:
    llm = FakeEmbedLLM(answer="<tool_call>{...}</tool_call>")
    answer = run_retrieval_flow("вопрос", llm=llm, vector_store=FakeStore([]), ctx=UNIT_CTX)
    assert answer == DEGRADED_ANSWER


def test_retrieval_system_prompt_marks_context_as_data() -> None:
    # security §3 S6: retrieval-контекст (RAG/few-shot/usage_notes) маркируется
    # как данные, не инструкции — в системном промпте (наблюдение S13 о параличе
    # reasoning-модели при придирке рядом с самими данными)
    llm, store = FakeEmbedLLM(), FakeStore([_doc("[таблица: orders] DDL", "ddl:orders")])
    run_retrieval_flow("вопрос", llm=llm, vector_store=store, ctx=UNIT_CTX)
    system = llm.stream_messages[0][0]["content"]
    assert "ДАННЫЕ" in system and "не инструкции" in system


def test_retrieval_system_prompt_forbids_fabricated_results() -> None:
    # S24 находка #1(б): модель retrieval-контура не имеет канала исполнения,
    # но выдала «Результат: 1» как выполненный запрос — контракт промпта
    # обязан запрещать выдуманные результаты. Отдельная оговорка отделяет
    # отсутствие строк данных (норма) от отсутствия схемы (честное «не хватает»):
    # прежнее «если данных для ответа в контексте нет — скажи честно» читалось
    # малой моделью как «нет строк → отказ» (отказы в прогонах КЗ, S24 #1).
    # Формулировка «не существует в мире» (ревью S24 Important-2) отвергнута:
    # top-k-выдача ≠ весь индекс — промах recall не должен превращаться
    # в уверенное утверждение об отсутствии таблицы; deny-by-default остаётся
    # рубрижной границей (права, фильтр в SQL поиска), не recall-границей
    assert "SQL не выполняется" in RETRIEVAL_SYSTEM_PROMPT
    assert "не выдумывай числа" in RETRIEVAL_SYSTEM_PROMPT
    assert "не повод отказываться отвечать" in RETRIEVAL_SYSTEM_PROMPT
    # промах recall → честная неопределённость, а не ложное «таблицы нет»
    assert "не выдумывай схему" in RETRIEVAL_SYSTEM_PROMPT
    assert "каких знаний не хватает" in RETRIEVAL_SYSTEM_PROMPT

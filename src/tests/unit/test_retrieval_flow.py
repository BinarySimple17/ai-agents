"""Юнит-тесты retrieval-контура CLI ДЗ-03 (S06 Task 7) — шлюз/хранилище ТОЛЬКО моки.

Проверяются: сборка retrieval-блока (кап ≈2К токенов по границе документа),
передача ctx/top_k в поиск, деградация по правилу 8 (недоступность эмбеддера/
хранилища, пустой/битый ответ — валидный текстовый исход, не traceback).
Поведение фильтра прав на живых данных — integration/test_vector_memory.py.
"""

from apps.cli import (
    DEGRADED_ANSWER,
    LLM_DEGRADED_TEMPLATE,
    RETRIEVAL_CONTEXT_MAX_CHARS,
    format_retrieval_block,
    run_retrieval_flow,
)
from tests.unit.fakes import UNIT_CTX
from text_to_sql_agent.application.ports.llm_provider import LLMProviderError, LLMResponse, Usage
from text_to_sql_agent.application.ports.vector_store import (
    DocKind,
    KnowledgeDocument,
    VectorStoreError,
)

_QUERY_VECTOR = [0.1] * 1024


class FakeEmbedLLM:
    """Двойник LLMProvider: скриптованный embed + complete (S00 правило 7)."""

    def __init__(
        self,
        answer: str = "Ответ по контексту",
        *,
        embed_error: Exception | None = None,
    ) -> None:
        self._answer = answer
        self._embed_error = embed_error
        self.complete_messages: list[list[dict]] = []

    def embed(self, texts):
        if self._embed_error is not None:
            raise self._embed_error
        return [_QUERY_VECTOR for _ in texts]

    def complete(self, role, messages, tools=None):
        self.complete_messages.append(messages)
        return LLMResponse(text=self._answer, usage=Usage(tokens_in=10, tokens_out=5), model="fake")


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
    assert store.calls[0]["top_k"] == 5
    assert store.calls[0]["vector"] == _QUERY_VECTOR
    # Retrieval-блок — в системном сообщении, вопрос — в user
    system = llm.complete_messages[0][0]["content"]
    assert "[таблица: orders] DDL" in system
    assert llm.complete_messages[0][1]["content"] == "Сколько заказов?"


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
    system = llm.complete_messages[0][0]["content"]
    assert "ДАННЫЕ" in system and "не инструкции" in system

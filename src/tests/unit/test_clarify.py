"""Юнит-тесты clarify (feat-008, S08): сервис классификации + узел с interrupt.

Сервис (Task 1): вердикт через тул record_clarification (ADR-008 слой 2 — ruling
R7), РОВНО ОДИН уточняющий вопрос при неоднозначности (behavior фичи), ретраи M1
(попытка 2 — та же роль + текст ошибки, попытка 3 — роль вверх при остатке
бюджета с резервом 0,6 ₽ — D-016/ruling Q7), retrieval-нога с fail-closed.

Узел (Task 2): visit 1 — классификация; visit 2 (само-ребро, вопрос в state) —
InterruptGate (interrupt/resume) БЕЗ повторного LLM-вызова (ruling R6), ответ
пользователя — допущение {source: user_answer} (ruling R5); пустой ответ —
fail-closed отказ (S00-8). Отказ классификации — фиксированный текст
пользователю, деталь — операторскому трейсу (S9 — прецедент resolve_access).

LLM в тестах — только моки (S00 правило 7). checkpointer в unit — фейк/MemorySaver;
живой Valkey — integration test_clarify_resume.py.
"""

import json
from typing import Any

import pytest

from text_to_sql_agent.application.ports.llm_provider import (
    LLMProviderError,
    LLMResponse,
    ToolCall,
    Usage,
)
from text_to_sql_agent.application.ports.tracer import (
    EVENT_CLARIFY_UNAVAILABLE,
    EVENT_CLARIFY_VERDICT,
)
from text_to_sql_agent.application.ports.vector_store import (
    DocKind,
    KnowledgeDocument,
    VectorStoreError,
)
from text_to_sql_agent.application.services.clarify_node import (
    CLARIFICATION_NEEDED_FLAG,
    make_clarify_node,
)
from text_to_sql_agent.application.services.clarify_service import (
    ClarifyOutcome,
    ClarifyService,
    ClarifyVerdict,
)
from text_to_sql_agent.domain.entities import Assumption, RunContext
from text_to_sql_agent.orchestration.routing import FLAG_CLARIFICATION_NEEDED

_CTX = RunContext(trace_id="t-clarify", user_id="u_analyst")


def _tool_response(payload: dict[str, Any]) -> LLMResponse:
    return LLMResponse(
        tool_calls=[
            ToolCall(id="call-1", name="record_clarification", arguments=json.dumps(payload))
        ],
        usage=Usage(tokens_in=10, tokens_out=5),
        model="cheap",
    )


class _ScriptedLLM:
    """Скриптовый шлюз: ответы по очереди; пишет роли/сообщения для проверок M1."""

    def __init__(self, responses: list[LLMResponse], embed_fail: bool = False) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.embed_fail = embed_fail

    def complete(self, role: str, messages: Any, *, tools: Any = None) -> LLMResponse:
        self.calls.append({"role": role, "messages": list(messages), "tools": tools})
        if not self.responses:
            raise AssertionError("скриптовый шлюз исчерпан — вызовов больше, чем ответов")
        return self.responses.pop(0)

    def embed(self, texts: Any) -> list[list[float]]:
        if self.embed_fail:
            raise LLMProviderError("эмбеддер недоступен")
        return [[0.01] * 1024 for _ in texts]

    def reset_request_budget(self) -> None:  # порт, сервис не зовёт — на прогоне
        ...


class _FakeVectorStore:
    """Фейковый VectorStore: выдача или сбой (fail-closed retrieval-ноги)."""

    def __init__(self, docs: list[KnowledgeDocument] | None = None, broken: bool = False) -> None:
        self.docs = docs or []
        self.broken = broken

    def upsert(self, documents: Any, embeddings: Any) -> int:  # pragma: no cover
        raise AssertionError("clarify не пишет в память знаний")

    def search(self, query: str, query_embedding: Any, *, ctx: Any, top_k: int) -> list[Any]:
        if self.broken:
            raise VectorStoreError("pgvector недоступен")
        return self.docs[:top_k]


class _RecordingGate:
    """Фейковый InterruptGate: копит вопросы, возвращает заготовленный ответ."""

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.asked: list[str] = []

    def ask(self, question: str) -> str:
        self.asked.append(question)
        return self.answer


class _RecordingTracer:
    def __init__(self) -> None:
        self.records: list[Any] = []

    def record(self, trace: Any) -> None:
        self.records.append(trace)


def _doc(
    kind: DocKind = DocKind.DDL, content: str = "таблица orders — заказы"
) -> KnowledgeDocument:
    return KnowledgeDocument(
        doc_kind=kind,
        table_name="orders" if kind == DocKind.DDL else None,
        referenced_tables=["orders"] if kind == DocKind.DDL else [],
        content=content,
        model_version="embed-1",
        source_ref=f"test:{kind.value}",
    )


class TestClarifyVerdictSchema:
    def test_unambiguous_without_question_ok(self) -> None:
        verdict = ClarifyVerdict.model_validate(
            {"ambiguity": "unambiguous", "assumptions": ["период — текущий год"]}
        )
        assert verdict.clarifying_question is None

    def test_unambiguous_with_question_rejected(self) -> None:
        with pytest.raises(Exception, match="не требует уточняющего"):
            ClarifyVerdict.model_validate(
                {"ambiguity": "unambiguous", "clarifying_question": "какой период?"}
            )

    @pytest.mark.parametrize("ambiguity", ["ambiguous", "insufficient_data"])
    def test_ambiguous_requires_one_question(self, ambiguity: str) -> None:
        with pytest.raises(Exception, match="требует непустой clarifying_question"):
            ClarifyVerdict.model_validate({"ambiguity": ambiguity})
        verdict = ClarifyVerdict.model_validate(
            {"ambiguity": ambiguity, "clarifying_question": "какой период?"}
        )
        assert verdict.clarifying_question == "какой период?"

    def test_empty_assumption_rejected(self) -> None:
        with pytest.raises(Exception, match="не может быть пустым"):
            ClarifyVerdict.model_validate({"ambiguity": "unambiguous", "assumptions": ["  "]})


class TestClarifyService:
    def _service(
        self,
        llm: _ScriptedLLM,
        vector_store: _FakeVectorStore | None = None,
        budget_remaining: Any = None,
    ) -> ClarifyService:
        return ClarifyService(
            llm=llm,
            vector_store=vector_store or _FakeVectorStore(docs=[_doc()]),
            budget_remaining_roubles=budget_remaining,
        )

    def test_unambiguous_writes_classifier_assumptions(self) -> None:
        llm = _ScriptedLLM(
            [_tool_response({"ambiguity": "unambiguous", "assumptions": ["период — 2026 год"]})]
        )
        result = self._service(llm).clarify("Сколько заказов?", ctx=_CTX)
        assert result.outcome == ClarifyOutcome.UNAMBIGUOUS
        assert result.question is None
        assert result.assumptions == [Assumption(source="classifier", text="период — 2026 год")]
        assert result.llm_calls == 1
        assert llm.calls[0]["role"] == "cheap"  # матрица ADR-007: attempt 1 — cheap

    def test_ambiguous_returns_exactly_one_question(self) -> None:
        llm = _ScriptedLLM(
            [
                _tool_response(
                    {
                        "ambiguity": "ambiguous",
                        "clarifying_question": "Заказы за какой период?",
                    }
                )
            ]
        )
        result = self._service(llm).clarify("Заказы", ctx=_CTX)
        assert result.outcome == ClarifyOutcome.CLARIFY
        assert result.question == "Заказы за какой период?"
        assert result.llm_calls == 1

    def test_insufficient_data_is_clarify_outcome(self) -> None:
        llm = _ScriptedLLM(
            [
                _tool_response(
                    {
                        "ambiguity": "insufficient_data",
                        "clarifying_question": "Уточните сущность",
                    }
                )
            ]
        )
        result = self._service(llm).clarify("Что-то", ctx=_CTX)
        assert result.outcome == ClarifyOutcome.CLARIFY
        assert result.question == "Уточните сущность"

    def test_retrieval_fail_is_honest_fail_without_llm_classification(self) -> None:
        # Fail-closed retrieval-ноги (прецедент S09): классификация без контекста —
        # ложные вердикты; LLM-классификация не вызывается вовсе
        llm = _ScriptedLLM([_tool_response({"ambiguity": "unambiguous"})])
        store = _FakeVectorStore(broken=True)
        result = self._service(llm, store).clarify("Сколько заказов?", ctx=_CTX)
        assert result.outcome == ClarifyOutcome.FAIL
        assert "retrieval недоступен" in (result.error or "")
        assert not llm.calls  # complete не вызван — embed упал раньше
        assert result.llm_calls == 0

    def test_invalid_output_retries_same_role_with_error_text(self) -> None:
        # Попытка 1 — выход без тула → попытка 2 — ТА ЖЕ роль + текст ошибки
        # (D-016: в контекст — последний выход + ошибка, не цепочка попыток)
        llm = _ScriptedLLM(
            [
                LLMResponse(text="думаю о вопросе...", usage=Usage(), model="cheap"),
                _tool_response({"ambiguity": "unambiguous"}),
            ]
        )
        result = self._service(llm).clarify("Сколько заказов?", ctx=_CTX)
        assert result.outcome == ClarifyOutcome.UNAMBIGUOUS
        assert result.llm_calls == 2
        assert llm.calls[0]["role"] == "cheap"
        assert llm.calls[1]["role"] == "cheap"
        retry_messages = llm.calls[1]["messages"]
        joined = json.dumps(retry_messages, ensure_ascii=False)
        assert "record_clarification" in joined or "не прошёл валидацию" in joined
        assert "думаю о вопросе..." in joined  # последний выход в контексте
        assert "без вызова тула" in joined  # текст ошибки в контексте

    def test_reasoning_only_text_fails_parse_and_retries(self) -> None:
        # Ловушка 3 S08 (D-021): reasoning-only текст — парсер обязан упасть
        llm = _ScriptedLLM(
            [
                LLMResponse(text="</think>размышление", usage=Usage(), model="cheap"),
                _tool_response({"ambiguity": "unambiguous"}),
            ]
        )
        result = self._service(llm).clarify("Сколько?", ctx=_CTX)
        assert result.outcome == ClarifyOutcome.UNAMBIGUOUS  # ретрай спас
        assert result.llm_calls == 2

    def test_escalation_attempt_three_uses_mid_role(self) -> None:
        # Попытка 3 — роль вверх (cheap→mid, D-016) при достатке бюджета с резервом
        llm = _ScriptedLLM(
            [
                LLMResponse(text="битый выход 1", usage=Usage(), model="cheap"),
                LLMResponse(text="битый выход 2", usage=Usage(), model="cheap"),
                _tool_response({"ambiguity": "unambiguous"}),
            ]
        )
        service = self._service(llm, budget_remaining=lambda: 1.5)
        result = service.clarify("Сколько?", ctx=_CTX)
        assert result.outcome == ClarifyOutcome.UNAMBIGUOUS
        assert result.llm_calls == 3
        assert llm.calls[2]["role"] == "mid"  # эскалация по D-016

    def test_escalation_rejected_when_budget_below_reserve(self) -> None:
        # Резерв 0,6 ₽ (ADR-007 §4.3): остаток не покрывает — эскалация отклонена,
        # честный FAIL после двух попыток
        llm = _ScriptedLLM(
            [
                LLMResponse(text="битый 1", usage=Usage(), model="cheap"),
                LLMResponse(text="битый 2", usage=Usage(), model="cheap"),
                _tool_response({"ambiguity": "unambiguous"}),  # не должен уйти
            ]
        )
        result = self._service(llm, budget_remaining=lambda: 0.5).clarify("Сколько?", ctx=_CTX)
        assert result.outcome == ClarifyOutcome.FAIL
        assert "резерв" in (result.error or "")
        assert result.llm_calls == 2
        assert len(llm.calls) == 2

    def test_escalation_without_budget_seam_proceeds_unchecked(self) -> None:
        # Шов бюджета не подключён (None) — эскалация без проверки (ruling Q7:
        # документированный fallback до S15 BudgetGuard; прецедент plan_joins —
        # тот же контекст, единая семантика шва)
        llm = _ScriptedLLM(
            [
                LLMResponse(text="битый 1", usage=Usage(), model="cheap"),
                LLMResponse(text="битый 2", usage=Usage(), model="cheap"),
                _tool_response({"ambiguity": "unambiguous"}),
            ]
        )
        result = self._service(llm, budget_remaining=None).clarify("Сколько?", ctx=_CTX)
        assert result.outcome == ClarifyOutcome.UNAMBIGUOUS
        assert result.llm_calls == 3
        assert llm.calls[2]["role"] == "mid"

    def test_provider_error_is_honest_fail(self) -> None:
        llm = _ScriptedLLM([])
        llm.responses.append(LLMResponse(text="x", usage=Usage(), model="cheap"))

        # Переопределяем complete на бросок после первого же вызова
        def raise_after_first(role: Any, messages: Any, *, tools: Any = None) -> LLMResponse:
            raise LLMProviderError("breaker открыт")

        llm.complete = raise_after_first  # type: ignore[method-assign]
        result = self._service(llm).clarify("Сколько?", ctx=_CTX)
        assert result.outcome == ClarifyOutcome.FAIL
        assert "шлюз недоступен" in (result.error or "")
        assert result.llm_calls == 1  # вызов попытан — учёт честный

    def test_all_three_attempts_exhausted_is_fail(self) -> None:
        llm = _ScriptedLLM(
            [
                LLMResponse(text="битый 1", usage=Usage(), model="cheap"),
                LLMResponse(text="битый 2", usage=Usage(), model="cheap"),
                LLMResponse(text="битый 3", usage=Usage(), model="mid"),
            ]
        )
        result = self._service(llm, budget_remaining=lambda: 2.0).clarify("Сколько?", ctx=_CTX)
        assert result.outcome == ClarifyOutcome.FAIL
        assert "3 попытки (M1)" in (result.error or "")

    def test_noise_documents_never_reach_prompt(self) -> None:
        # S26: дистракторы ранжируются, но в промпт не попадают
        docs = [_doc(DocKind.NOISE, "шумовой корпус-дистрактор"), _doc()]
        llm = _ScriptedLLM([_tool_response({"ambiguity": "unambiguous", "assumptions": []})])
        self._service(llm, _FakeVectorStore(docs=docs)).clarify("Сколько?", ctx=_CTX)
        prompt = llm.calls[0]["messages"][1]["content"]
        assert "дистрактор" not in prompt
        assert "orders" in prompt


class TestClarifyNode:
    def _service_result(self, outcome: ClarifyOutcome, **kwargs: Any) -> Any:
        from text_to_sql_agent.application.services.clarify_service import ClarifyResult

        return ClarifyResult(outcome=outcome, **kwargs)

    def _scripted_service(self, result: Any) -> Any:
        class _Scripted:
            def __init__(self) -> None:
                self.called = 0

            def clarify(self, question: str, *, ctx: Any, permissions: Any) -> Any:
                self.called += 1
                return result

        return _Scripted()

    def test_flag_name_matches_routing_constant(self) -> None:
        # application не импортирует orchestration — совпадение строк гвардит тест
        assert CLARIFICATION_NEEDED_FLAG == FLAG_CLARIFICATION_NEEDED

    def test_unambiguous_visit_writes_assumptions_and_clears_error(self) -> None:
        service = self._scripted_service(
            self._service_result(
                ClarifyOutcome.UNAMBIGUOUS,
                assumptions=[Assumption(source="classifier", text="период — 2026")],
                llm_calls=1,
            )
        )
        node = make_clarify_node(service, _RecordingGate("ответ"))
        updated = node(
            {
                "question": "Сколько заказов?",
                "user_id": "u_analyst",
                "trace_id": "t-1",
                "error": {"class": "stale"},
                "budget_used": {"llm_calls": 0, "rub": 0.0},
            }
        )
        assert [a.text for a in updated["assumptions"]] == ["период — 2026"]
        assert all(a.source == "classifier" for a in updated["assumptions"])
        assert updated["route_flags"][CLARIFICATION_NEEDED_FLAG] is False
        assert updated["error"] is None
        assert updated["budget_used"]["llm_calls"] == 1  # учёт фактического расхода
        assert updated["rewritten_question"] is None  # переписывание — вне скоупа S08

    def test_ambiguous_visit_writes_question_but_no_interrupt_yet(self) -> None:
        # Visit 1 только фиксирует вопрос в state; interrupt — visit 2 по
        # само-ребру (routing читает clarify_question) — ruling R1/R2
        service = self._scripted_service(
            self._service_result(ClarifyOutcome.CLARIFY, question="За какой период?", llm_calls=1)
        )
        gate = _RecordingGate("ответ")
        node = make_clarify_node(service, gate)
        updated = node({"question": "Заказы", "user_id": "u_analyst", "trace_id": "t-1"})
        assert updated["clarify_question"] == "За какой период?"
        assert updated["route_flags"][CLARIFICATION_NEEDED_FLAG] is True
        assert gate.asked == []  # interrupt в этом визите ещё не задан

    def test_interrupt_visit_uses_gate_without_llm_and_writes_user_assumption(self) -> None:
        # Visit 2: вопрос в state → gate.ask → допущение user_answer; сервис НЕ
        # вызывается (ruling R6: без повторного LLM-вызова)
        service = self._scripted_service(self._service_result(ClarifyOutcome.UNAMBIGUOUS))
        gate = _RecordingGate("за 2026 год")
        node = make_clarify_node(service, gate)
        updated = node(
            {
                "user_id": "u_analyst",
                "clarify_question": "За какой период?",
                "route_flags": {CLARIFICATION_NEEDED_FLAG: True},
                "assumptions": [Assumption(source="classifier", text="выручка по заказам")],
            }
        )
        assert gate.asked == ["За какой период?"]
        assert service.called == 0
        assert [a.source for a in updated["assumptions"]] == ["classifier", "user_answer"]
        assert updated["assumptions"][1].text == "за 2026 год"
        assert updated["clarify_question"] is None
        assert updated["route_flags"][CLARIFICATION_NEEDED_FLAG] is False
        assert updated["error"] is None

    def test_empty_answer_is_fail_closed_rejection(self) -> None:
        # S00-8 + fail-closed: пустой/whitespace ответ — отказ, не молчаливое
        # продолжение
        for answer in ("", "   "):
            service = self._scripted_service(self._service_result(ClarifyOutcome.UNAMBIGUOUS))
            node = make_clarify_node(service, _RecordingGate(answer))
            updated = node({"user_id": "u_analyst", "clarify_question": "Уточните"})
            assert updated["clarify_question"] is None
            assert updated["route_flags"][CLARIFICATION_NEEDED_FLAG] is True
            assert updated["error"]["class"] == "clarify_empty_answer"
            assert "assumptions" not in updated

    def test_fail_outcome_fixed_message_detail_to_trace(self) -> None:
        # S9-политика: пользовательский текст без деталей инфраструктуры; деталь
        # сбоя — только операторский трейс (прецедент resolve.rights_unavailable)
        service = self._scripted_service(
            self._service_result(
                ClarifyOutcome.FAIL, error="retrieval недоступен: 127.0.0.1:5432 отказ"
            )
        )
        tracer = _RecordingTracer()
        node = make_clarify_node(service, _RecordingGate("ответ"), tracer=tracer)
        updated = node({"question": "Сколько?", "user_id": "u_analyst", "trace_id": "t-1"})
        message = updated["error"]["message"]
        assert (
            message == "не удалось оценить однозначность вопроса — переформулируйте его подробнее"
        )
        assert "127.0.0.1" not in message
        assert updated["route_flags"][CLARIFICATION_NEEDED_FLAG] is True
        assert updated["clarify_question"] is None  # флаг БЕЗ вопроса → терминал clarification
        events = [t.event for t in tracer.records]
        assert EVENT_CLARIFY_UNAVAILABLE in events
        detail_payload = next(
            t.payload for t in tracer.records if t.event == EVENT_CLARIFY_UNAVAILABLE
        )
        assert "127.0.0.1" in detail_payload["detail"]  # оператору — для диагностики

    def test_missing_identity_is_fail_closed_without_service_call(self) -> None:
        # Defense-in-depth: resolve_access отклоняет пустую identity раньше —
        # clarify с пустым user_id означает подмену каркаса; не RunContext-crash
        service = self._scripted_service(self._service_result(ClarifyOutcome.UNAMBIGUOUS))
        node = make_clarify_node(service, _RecordingGate("ответ"))
        updated = node({"question": "Сколько?", "user_id": "  "})
        assert updated["error"]["class"] == "identity_missing"
        assert service.called == 0

    def test_tracer_records_verdict_event_not_interrupt(self) -> None:
        # clarify.verdict — зона узла; clarify.interrupt — зона раннера (resume-
        # визит ре-исполняет узел с начала: узел не отличает паузу от продолжения)
        tracer = _RecordingTracer()
        service = self._scripted_service(
            self._service_result(ClarifyOutcome.CLARIFY, question="Уточните", llm_calls=1)
        )
        node = make_clarify_node(service, _RecordingGate("ответ"), tracer=tracer)
        node({"question": "Заказы", "user_id": "u_analyst", "trace_id": "t-1"})
        node(
            {
                "user_id": "u_analyst",
                "clarify_question": "Уточните",
                "route_flags": {CLARIFICATION_NEEDED_FLAG: True},
            }
        )
        assert service.called == 1
        events = [t.event for t in tracer.records]
        assert events == [EVENT_CLARIFY_VERDICT]

    def test_node_works_without_tracer(self) -> None:
        service = self._scripted_service(self._service_result(ClarifyOutcome.FAIL, error="сбой"))
        node = make_clarify_node(service, _RecordingGate("ответ"))  # tracer не передан
        updated = node({"question": "Сколько?", "user_id": "u_analyst", "trace_id": "t-1"})
        assert updated["error"]["class"] == "clarify_unavailable"

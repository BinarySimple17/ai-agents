"""Юнит-тесты финального контура FSM (feat-015, S15): compact_result /
interpret_result / degrade + BudgetGuard.

LLM в тестах — только моки (S00 правило 7): скриптованный двойник шлюза
(тул-выходы/сбои) + RecordingTracer — прецедент test_diagnose (S14).
Бюджетный шов и снапшот ₽ — инъекции (rulings R4/R6 ARCH-GATE S15).
"""

import json

import pytest

from tests.unit.fakes import RecordingTracer
from text_to_sql_agent.application.ports.llm_provider import (
    BudgetExceededError,
    LLMProviderError,
    LLMResponse,
    ToolCall,
    Usage,
)
from text_to_sql_agent.application.ports.tracer import (
    EVENT_INTERPRET_UNAVAILABLE,
    EVENT_INTERPRET_VERDICT,
)
from text_to_sql_agent.application.services.compact_result_node import (
    make_compact_result_node,
)
from text_to_sql_agent.application.services.interpret_result_node import (
    FINAL_ANSWER_KEY,
    make_interpret_result_node,
)
from text_to_sql_agent.application.services.interpret_result_service import (
    InterpretOutcome,
    InterpretResult,
    InterpretService,
)
from text_to_sql_agent.domain.result_compact import (
    MAX_COMPACT_ROWS,
    ResultCompact,
    compact_rows,
)

State = dict


# --- Task 1: compact_result — код (VO ResultCompact + узел) ---


def _rows(count: int, value: int = 1) -> list[dict]:
    """count строк одной числовой колонки (детерминированные значения)."""
    return [{"amount": i * value, "region": f"r{i % 3}"} for i in range(count)]


class TestResultCompactVO:
    """VO ResultCompact — поля по ER (ruling R2 ARCH-GATE S15)."""

    def test_sample_truncated_to_max_rows(self) -> None:
        compact = compact_rows(_rows(100), truncated=False, approximate=False)
        assert compact.row_count == 100
        assert len(compact.sample) == MAX_COMPACT_ROWS == 20
        assert compact.sample[0] == {"amount": 0, "region": "r0"}

    def test_small_result_keeps_all_rows(self) -> None:
        rows = _rows(5)
        compact = compact_rows(rows, truncated=False, approximate=False)
        assert compact.row_count == 5
        assert compact.sample == rows

    def test_aggregates_numeric_column(self) -> None:
        rows = [{"amount": 10}, {"amount": 20}, {"amount": 30}]
        compact = compact_rows(rows, truncated=False, approximate=False)
        agg = compact.aggregates["amount"]
        assert agg.sum == 60
        assert agg.avg == 20
        assert agg.min == 10
        assert agg.max == 30

    def test_aggregates_skip_non_numeric_columns(self) -> None:
        rows = [{"amount": 1, "region": "r"}, {"amount": 2, "region": "r"}]
        compact = compact_rows(rows, truncated=False, approximate=False)
        assert "amount" in compact.aggregates
        assert "region" not in compact.aggregates

    def test_bool_is_not_numeric(self) -> None:
        # bool — подкласс int в Python: без явного исключения «активные» считались бы
        rows = [{"active": True}, {"active": False}]
        compact = compact_rows(rows, truncated=False, approximate=False)
        assert "active" not in compact.aggregates

    def test_aggregates_skip_none_values(self) -> None:
        rows = [{"amount": None}, {"amount": 4}, {"amount": 6}]
        compact = compact_rows(rows, truncated=False, approximate=False)
        assert compact.aggregates["amount"].sum == 10

    def test_all_none_column_has_no_aggregate(self) -> None:
        rows = [{"amount": None}, {"amount": None}]
        compact = compact_rows(rows, truncated=False, approximate=False)
        assert "amount" not in compact.aggregates

    def test_mixed_type_column_has_no_aggregate(self) -> None:
        # «сумма» по смешанным типам — ложь в payload: колонка пропускается
        rows = [{"amount": 1}, {"amount": "2"}]
        compact = compact_rows(rows, truncated=False, approximate=False)
        assert "amount" not in compact.aggregates

    def test_empty_rows(self) -> None:
        compact = compact_rows([], truncated=False, approximate=False)
        assert compact.row_count == 0
        assert compact.sample == []
        assert compact.aggregates == {}

    def test_flags_are_explicit_fields(self) -> None:
        # truncated — зеркало state.truncated (исполнитель LIMIT), approximate —
        # T2-флаг (False до выпускной) — ruling R2 ARCH-GATE S15
        compact = compact_rows([], truncated=True, approximate=True)
        assert compact.truncated is True
        assert compact.approximate is True

    def test_sample_cannot_exceed_limit(self) -> None:
        # fail-closed VO: сэмпл больше бюджета промпта интерпретатора невозможен
        with pytest.raises(ValueError, match="бюджета промпта"):
            ResultCompact(row_count=3, sample=_rows(MAX_COMPACT_ROWS + 1))

    def test_aggregate_deterministic_column_order(self) -> None:
        rows = [{"b": 1, "a": 2}]
        compact = compact_rows(rows, truncated=False, approximate=False)
        assert list(compact.aggregates) == sorted(["a", "b"])


class TestCompactResultNode:
    """Узел compact_result: state.rows → state.compact (владение — карта AgentState)."""

    def test_writes_compact_from_rows(self) -> None:
        node = make_compact_result_node()
        updated = node({"rows": _rows(100), "row_count": 100, "truncated": False})
        compact = updated["compact"]
        assert compact["row_count"] == 100
        assert len(compact["sample"]) == MAX_COMPACT_ROWS

    def test_mirrors_truncation_flags(self) -> None:
        node = make_compact_result_node()
        updated = node({"rows": [], "truncated": True, "approximate": True})
        assert updated["compact"]["truncated"] is True
        assert updated["compact"]["approximate"] is True

    def test_none_rows_is_empty_compact(self) -> None:
        # T2-approximate путь доходит до compact без исполнения (S12, D-19):
        # честный пустой компакт с approximate-флагом, не дефект
        node = make_compact_result_node()
        updated = node({"rows": None, "approximate": True})
        assert updated["compact"]["row_count"] == 0

    def test_no_llm_calls_charged(self) -> None:
        # компакт — код-узел (hw-01 §3.4): бюджет прогона не платит
        node = make_compact_result_node()
        updated = node({"rows": _rows(1)})
        assert "budget_used" not in updated


# --- Task 2: interpret_result — финальный текст (strong, ADR-008) ---

_ANSWER_TOOL = "report_answer"


class ScriptedLLM:
    """Двойник LLMProvider: очередь ответов/сбоев + запись вызовов (роль/сообщения)."""

    def __init__(self, script: list[LLMResponse | Exception]) -> None:
        self.script = list(script)
        self.calls: list[tuple[str, object, object]] = []

    def complete(self, role, messages, *, tools=None):
        self.calls.append((role, messages, tools))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def embed(self, texts):
        return [[0.0]] * len(texts)

    def reset_request_budget(self) -> None:
        return None


def _tool_answer(text: str, *, finish_reason: str | None = "stop") -> LLMResponse:
    return LLMResponse(
        tool_calls=[ToolCall(id="1", name=_ANSWER_TOOL, arguments=json.dumps({"text": text}))],
        usage=Usage(tokens_in=100, tokens_out=50),
        model="glm-5.3-prime",
        finish_reason=finish_reason,
    )


def _compact_dict() -> dict:
    return compact_rows(
        [{"amount": 10}, {"amount": 20}], truncated=False, approximate=False
    ).model_dump()


class TestInterpretService:
    """interpret_result: strong, тул report_answer, M1 ≤ 3 без эскалации (вершина)."""

    def _service(
        self, script: list[LLMResponse | Exception]
    ) -> tuple[InterpretService, ScriptedLLM]:
        llm = ScriptedLLM(script)
        return InterpretService(llm=llm), llm

    def _interpret(self, service: InterpretService):
        return service.interpret(
            question="Сколько заказов?",
            sql="SELECT count(*) FROM orders LIMIT 1000",
            compact=_compact_dict(),
            assumptions=["считаем все заказы"],
            usage_notes=["заказы соединяются с клиентами по customer_id"],
            cost_rub=0.42,
            truncated=False,
            approximate=False,
            incomplete=False,
        )

    def test_answer_via_tool(self) -> None:
        service, _ = self._service([_tool_answer("Всего 2 заказа.")])
        result = self._interpret(service)
        assert result.outcome is InterpretOutcome.ANSWER
        assert result.text == "Всего 2 заказа."
        assert result.llm_calls == 1
        assert result.incomplete is False

    def test_role_is_strong_on_every_attempt(self) -> None:
        # interpret — вершина каскада: ретраи M1 остаются strong, эскалации нет
        service, llm = self._service(
            [LLMResponse(text="голый текст без тула", model="m"), _tool_answer("ок")]
        )
        result = self._interpret(service)
        assert result.outcome is InterpretOutcome.ANSWER
        assert result.llm_calls == 2
        assert [role for role, _, _ in llm.calls] == ["strong", "strong"]

    def test_retry_context_carries_parse_error(self) -> None:
        service, llm = self._service(
            [LLMResponse(text="голый текст без тула", model="m"), _tool_answer("ок")]
        )
        self._interpret(service)
        retry_messages = llm.calls[1][1]
        joined = json.dumps(retry_messages, ensure_ascii=False)
        assert "Ошибка предыдущей попытки" in joined

    def test_m1_exhaustion_is_incomplete(self) -> None:
        bare = LLMResponse(text="не тул", model="m")
        service, _ = self._service([bare, bare, bare])
        result = self._interpret(service)
        assert result.outcome is InterpretOutcome.INCOMPLETE
        assert result.llm_calls == 3
        assert result.text is None  # шаблон честного отказа собирает узел

    def test_empty_answer_text_rejected(self) -> None:
        # пустой текст в туле — бессодержательный ответ: ретрай M1 (валидатор VO)
        empty = LLMResponse(
            tool_calls=[ToolCall(id="1", name=_ANSWER_TOOL, arguments=json.dumps({"text": "   "}))],
            model="m",
        )
        service, _ = self._service([empty, empty, empty])
        result = self._interpret(service)
        assert result.outcome is InterpretOutcome.INCOMPLETE
        assert result.llm_calls == 3

    def test_llm_unavailable_is_incomplete(self) -> None:
        service, _ = self._service([LLMProviderError("шлюз не ответил")])
        result = self._interpret(service)
        assert result.outcome is InterpretOutcome.INCOMPLETE
        assert result.llm_calls == 0

    def test_budget_exceeded_is_incomplete(self) -> None:
        # последняя миля (strong) не пробивает кап: исключение адаптера ДО вызова
        service, _ = self._service([BudgetExceededError("кап 2 ₽ исчерпан")])
        result = self._interpret(service)
        assert result.outcome is InterpretOutcome.INCOMPLETE
        assert result.llm_calls == 0

    def test_finish_reason_length_marks_incomplete(self) -> None:
        # S19-хвост: «length» с текстом — тихая усечённость шлюза → incomplete
        service, _ = self._service([_tool_answer("частичный ответ", finish_reason="length")])
        result = self._interpret(service)
        assert result.outcome is InterpretOutcome.ANSWER
        assert result.incomplete is True

    def test_prompt_contains_compact_not_raw_rows(self) -> None:
        service, llm = self._service([_tool_answer("ок")])
        rows = [{"amount": i} for i in range(100)]
        compact = compact_rows(rows, truncated=False, approximate=False).model_dump()
        service.interpret(
            question="вопрос",
            sql="SELECT ...",
            compact=compact,
            assumptions=["допущение про период"],
            usage_notes=["замечание связи"],
            cost_rub=0.5,
            truncated=True,
            approximate=False,
            incomplete=False,
        )
        prompt = json.dumps(llm.calls[0][1], ensure_ascii=False)
        assert "row_count" in prompt and "допущение про период" in prompt
        assert "замечание связи" in prompt
        assert "Стоимость" in prompt
        # сырье за пределами компакта в промпт не попадает (behavior feat-015)
        assert '{"amount": 99}' not in prompt


# --- Узел interpret_result: glue сервис ↔ state (final_answer, R6 ₽-снапшот) ---


class _StaticGuard:
    """Фейк BudgetGuard: фиксированный снапшот расхода прогона."""

    def spent_rub(self) -> float:
        return 0.42

    def remaining_rub(self) -> float:
        return 1.58

    def reset(self) -> None:
        return None


class _ScriptedInterpretService:
    """Двойник сервиса: заготовленный InterpretResult + запись kwargs узла."""

    def __init__(self, result: "InterpretResult") -> None:
        self.result = result
        self.kwargs: dict | None = None

    def interpret(self, **kwargs):
        self.kwargs = kwargs
        return self.result


def _answer_result(text: str = "Всего 2 заказа.", **fields) -> "InterpretResult":
    return InterpretResult(outcome=InterpretOutcome.ANSWER, text=text, llm_calls=1, **fields)


class TestInterpretNode:
    def _state(self) -> dict:
        return {
            "question": "Сколько заказов?",
            "sql": "SELECT count(*) FROM orders LIMIT 1000",
            "compact": _compact_dict(),
            "assumptions": [{"source": "classifier", "text": "считаем все заказы"}],
            "join_plan": {"tables": ["orders"], "assumptions": ["замечание связи"]},
            "truncated": False,
            "approximate": False,
            "incomplete": False,
            "budget_used": {"llm_calls": 2, "rub": 0.1},
            "user_id": "u1",
        }

    def test_success_writes_final_answer_contract(self) -> None:
        service = _ScriptedInterpretService(_answer_result())
        node = make_interpret_result_node(service, budget_guard=_StaticGuard())
        updated = node(self._state())
        answer = updated[FINAL_ANSWER_KEY]
        assert answer["text"] == "Всего 2 заказа."
        assert answer["sql"] == "SELECT count(*) FROM orders LIMIT 1000"
        assert answer["assumptions"] == ["считаем все заказы"]
        assert answer["usage_notes"] == ["замечание связи"]
        assert answer["row_count"] == 2
        assert answer["truncated"] is False
        assert answer["approximate"] is False
        assert answer["incomplete"] is False
        assert answer["cost_rub"] == 0.42
        # компакт и остальные поля не тронуты (карта владения)
        assert "compact" not in updated

    def test_budget_accounting_r6(self) -> None:
        service = _ScriptedInterpretService(_answer_result())
        node = make_interpret_result_node(service, budget_guard=_StaticGuard())
        updated = node(self._state())
        budget = updated["budget_used"]
        assert budget["llm_calls"] == 3  # 2 + 1 вызов интерпретатора
        assert budget["rub"] == 0.42  # снапшот факта прогона (R6)

    def test_no_guard_cost_is_none_honest(self) -> None:
        # без guard'а снапшот недоступен: cost_rub=None честно (0 ₽ не маскируется),
        # rub в budget_used не появляется (узлы до S15 его не писали)
        state = self._state()
        state["budget_used"] = {"llm_calls": 2}
        service = _ScriptedInterpretService(_answer_result())
        node = make_interpret_result_node(service)
        updated = node(state)
        assert updated[FINAL_ANSWER_KEY]["cost_rub"] is None
        assert "rub" not in updated["budget_used"]

    def test_incomplete_outcome_sets_flag_and_template(self) -> None:
        service = _ScriptedInterpretService(
            InterpretResult(outcome=InterpretOutcome.INCOMPLETE, error="шлюз недоступен")
        )
        tracer = RecordingTracer()
        node = make_interpret_result_node(service, budget_guard=_StaticGuard(), tracer=tracer)
        updated = node(self._state())
        assert updated["incomplete"] is True
        answer = updated[FINAL_ANSWER_KEY]
        assert answer["incomplete"] is True
        assert "2" in answer["text"]  # честно: данные получены (row_count в тексте)
        assert "шлюз" not in answer["text"]  # S9: детали инфраструктуры — не пользователю
        assert EVENT_INTERPRET_UNAVAILABLE in tracer.names()

    def test_finish_reason_length_marks_state(self) -> None:
        service = _ScriptedInterpretService(_answer_result(text="частичный", incomplete=True))
        node = make_interpret_result_node(service, budget_guard=_StaticGuard())
        updated = node(self._state())
        assert updated["incomplete"] is True
        assert updated[FINAL_ANSWER_KEY]["incomplete"] is True

    def test_verdict_event_hides_answer_text(self) -> None:
        # R7: payload вердикта — факты, текст ответа в JSONL не дублируется
        service = _ScriptedInterpretService(_answer_result(text="СЕКРЕТНЫЙ-ТЕКСТ"))
        tracer = RecordingTracer()
        node = make_interpret_result_node(service, budget_guard=_StaticGuard(), tracer=tracer)
        node(self._state())
        verdict = next(t for t in tracer.events if t.event == EVENT_INTERPRET_VERDICT)
        assert "СЕКРЕТНЫЙ-ТЕКСТ" not in json.dumps(verdict.payload, ensure_ascii=False)
        assert verdict.payload["row_count"] == 2
        assert verdict.payload["cost_rub"] == 0.42

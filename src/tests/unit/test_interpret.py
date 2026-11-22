"""Юнит-тесты финального контура FSM (feat-015, S15): compact_result /
interpret_result / degrade + BudgetGuard.

LLM в тестах — только моки (S00 правило 7): скриптованный двойник шлюза
(тул-выходы/сбои) + RecordingTracer — прецедент test_diagnose (S14).
Бюджетный шов и снапшот ₽ — инъекции (rulings R4/R6 ARCH-GATE S15).
"""

import json
from collections.abc import Callable

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


def _tool_refusal(text: str) -> LLMResponse:
    return LLMResponse(
        tool_calls=[ToolCall(id="1", name=_REFUSAL_TOOL, arguments=json.dumps({"text": text}))],
        usage=Usage(tokens_in=80, tokens_out=40),
        model="deepseek-v4.1-flash",
        finish_reason="stop",
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
        assert verdict.payload["cost_rub"] == 0.42  # _StaticGuard: снапшот после вызова

    def test_cost_snapshot_after_llm_call(self) -> None:
        # I1 fix-волны (регрессия: падала на до-фиксовом коде): payload и трейс —
        # снапшот ПОСЛЕ вызова strong-модели (стоимость самого дорогого вызова
        # входит в пользовательский контракт), промпт — снапшот ДО (стоимость
        # на момент объяснения); guard растёт между вызовом сервиса и снапшотом
        class _GrowingGuard:
            def __init__(self) -> None:
                self.spent = 0.1

            def spent_rub(self) -> float:
                return self.spent

            def remaining_rub(self) -> float:
                return 2.0 - self.spent

            def reset(self) -> None:
                return None

        guard = _GrowingGuard()

        class _ChargingService:
            def __init__(self) -> None:
                self.prompt_cost: float | None = None

            def interpret(self, **kwargs):
                from text_to_sql_agent.application.services.interpret_result_service import (
                    InterpretOutcome,
                    InterpretResult,
                )

                self.prompt_cost = kwargs["cost_rub"]
                guard.spent = 0.7  # strong-вызов «стоил» и учтён аккумулятором
                return InterpretResult(outcome=InterpretOutcome.ANSWER, text="ок", llm_calls=1)

        service = _ChargingService()
        node = make_interpret_result_node(service, budget_guard=guard)
        updated = node(self._state())
        assert service.prompt_cost == pytest.approx(0.1)  # промпт — до вызова
        answer = updated[FINAL_ANSWER_KEY]
        assert answer["cost_rub"] == pytest.approx(0.7)  # payload — факт после вызова
        assert updated["budget_used"]["rub"] == pytest.approx(0.7)  # единый снапшот


# --- Task 3: degrade — отказ бизнес-понятиями (mid, санитайзер R1, вердикт R8) ---

from text_to_sql_agent.application.ports.tracer import (  # noqa: E402
    EVENT_DEGRADE_UNAVAILABLE,
    EVENT_DEGRADE_VERDICT,
)
from text_to_sql_agent.application.services.degrade_node import (  # noqa: E402
    make_degrade_node,
)
from text_to_sql_agent.application.services.degrade_service import (  # noqa: E402
    DegradeService,
)
from text_to_sql_agent.domain.degradation import (  # noqa: E402
    DegradationCase,
    DegradationCause,
    fallback_text,
)

_REFUSAL_TOOL = "report_refusal"


def _rights_case() -> DegradationCase:
    return DegradationCase(
        cause=DegradationCause.RIGHTS,
        situation="вопрос затрагивает данные, недоступные вашей роли",
        available_topics=("orders", "customers"),
        forbidden_topics=("bonuses", "salaries"),
    )


class TestDegradationCase:
    """Домен отказа: типизированный вердикт-причина (R8) + код-шаблоны (R1)."""

    def test_fallback_rights_lists_available_only(self) -> None:
        text = fallback_text(_rights_case())
        assert "orders" in text and "customers" in text
        assert "bonuses" not in text and "salaries" not in text

    def test_fallback_no_existence_markers(self) -> None:
        # S9: шаблон не подтверждает существование недоступных данных — гвард
        # теми же стемами, что санитайзер (M2 re-review: будущая правка шаблона
        # со словом «наличие/содержит» не пройдёт молча)
        text = fallback_text(_rights_case()).lower()
        for marker in (
            "существ",
            "присутству",
            "содерж",
            "имеютс",
            "наличи",
            "есть строки",
            "есть данные",
        ):
            assert marker not in text

    def test_fallback_no_path_mentions_reformulation(self) -> None:
        case = DegradationCase(
            cause=DegradationCause.NO_PATH,
            situation="данные вопроса не связаны в базе",
            available_topics=("orders",),
            forbidden_topics=(),
        )
        assert "order" in fallback_text(case)

    def test_fallback_technical_without_details(self) -> None:
        # S9: детали инфраструктуры — не пользователю (Ловушка 4)
        case = DegradationCase(
            cause=DegradationCause.TECHNICAL,
            situation="запрос не подготовлен",
            available_topics=(),
            forbidden_topics=(),
        )
        text = fallback_text(case)
        assert "технич" in text  # честная категория, без деталей (S9)
        assert "база данных" not in text.lower()


class TestDegradeSanitizer:
    """Санитайзер — runtime-гарантия S9 (ruling R1): LLM-текст не доверенный."""

    def _sanitize(self, text: str, case: DegradationCase | None = None):
        from text_to_sql_agent.application.services.degrade_service import sanitize_refusal

        return sanitize_refusal(text, case or _rights_case())

    def test_forbidden_table_name_rejected(self) -> None:
        violation = self._sanitize("Таблица bonuses вам недоступна.")
        assert violation is not None and "bonuses" in violation

    def test_forbidden_name_case_insensitive(self) -> None:
        assert self._sanitize("Данные BONUSES скрыты.") is not None

    def test_existence_confirmation_rejected(self) -> None:
        for text in (
            "В bonuses существуют записи, но они скрыты.",
            "В базе есть строки с премиями.",
            "Таблица содержит данные, недоступные вам.",
            "Премии присутствуют в системе.",
            "В системе содержатся данные о премиях.",  # M1 fix-волны: стем «содерж»
            "У нас имеются записи о бонусах.",  # M1 fix-волны: стем «имеютс»
        ):
            assert self._sanitize(text) is not None, text

    def test_clean_refusal_passes(self) -> None:
        assert (
            self._sanitize("Часть данных по этому вопросу недоступна. Могу показать заказы.")
            is None
        )

    def test_own_topics_allowed(self) -> None:
        # «своё упоминать можно» (ADR-005:78): доступные темы — не нарушение
        assert self._sanitize("Доступны заказы (orders) и клиенты (customers).") is None


class TestDegradeService:
    """Формулировка отказа: mid, одна попытка без эскалации/ретрая (R1)."""

    def _service(self, script: list[LLMResponse | Exception]) -> tuple[DegradeService, ScriptedLLM]:
        llm = ScriptedLLM(script)
        return DegradeService(llm=llm), llm

    def _formulate(self, service: DegradeService):
        return service.formulate(case=_rights_case())

    def test_refusal_via_tool(self) -> None:
        service, _ = self._service([_tool_refusal("Часть данных недоступна.")])
        result = self._formulate(service)
        assert result.text == "Часть данных недоступна."
        assert result.used_llm is True
        assert result.llm_calls == 1
        assert result.error is None

    def test_role_is_mid_single_attempt(self) -> None:
        service, llm = self._service([LLMResponse(text="не тул", model="m")])
        result = self._formulate(service)
        assert result.used_llm is True
        assert [role for role, _, _ in llm.calls] == ["mid"]  # одна попытка, без ретрая

    def test_parse_failure_falls_back_to_template(self) -> None:
        service, _ = self._service([LLMResponse(text="не тул", model="m")])
        result = self._formulate(service)
        assert result.text == fallback_text(_rights_case())
        assert result.error is not None

    def test_sanitizer_violation_falls_back_to_template(self) -> None:
        # модель назвала чужую таблицу — шаблон, не текст модели (runtime-гарантия)
        service, _ = self._service([_tool_refusal("Таблица bonuses скрыта от вас.")])
        result = self._formulate(service)
        assert result.text == fallback_text(_rights_case())
        assert "bonuses" in (result.error or "")

    def test_llm_unavailable_falls_back_to_template(self) -> None:
        # NFR-8: недоступность шлюза — деградация шаблоном (0 вызовов)
        service, _ = self._service([LLMProviderError("breaker открыт")])
        result = self._formulate(service)
        assert result.text == fallback_text(_rights_case())
        assert result.used_llm is False
        assert result.llm_calls == 0

    def test_budget_exceeded_falls_back_to_template(self) -> None:
        service, _ = self._service([BudgetExceededError("кап")])
        result = self._formulate(service)
        assert result.text == fallback_text(_rights_case())

    def test_prompt_has_no_forbidden_topics(self) -> None:
        # R8: сырые «чужие» имена в промпт не идут — модель не может их «спросить»
        service, llm = self._service([_tool_refusal("ок")])  # тул сервиса — report_refusal (M3)
        self._formulate(service)
        prompt = json.dumps(llm.calls[0][1], ensure_ascii=False)
        assert "bonuses" not in prompt and "salaries" not in prompt
        assert "orders" in prompt  # доступные темы — можно


class TestDegradeNode:
    """Узел degrade: вердикт-причина из кодовых каналов state (R8) + final_answer."""

    def _state(self, **extra) -> dict:
        state = {
            "question": "Покажи премии Анны",
            "permissions": {"orders": ["*"], "customers": ["*"]},
            "join_plan": {"tables": ["orders", "bonuses"], "assumptions": []},
            "validation": {"outcome": "ok", "issues": []},
            "route_flags": {},
            "budget_used": {"llm_calls": 3, "rub": 0.5},
            "truncated": False,
            "approximate": False,
            "incomplete": False,
            "user_id": "anna",
        }
        state.update(extra)
        return state

    def _service(self, text: str = "Часть данных недоступна."):
        return _ScriptedDegradeService(text)

    def test_rights_cause_from_validation_denied(self) -> None:
        service = self._service()
        node = make_degrade_node(service, budget_guard=_StaticGuard())
        updated = node(self._state(validation={"outcome": "denied", "issues": []}))
        case = service.kwargs["case"]
        assert case.cause is DegradationCause.RIGHTS
        assert set(case.available_topics) == {"orders", "customers"}
        assert case.forbidden_topics == ("bonuses",)  # план − права
        answer = updated[FINAL_ANSWER_KEY]
        assert answer["cause"] == "rights"
        assert answer["sql"] is None  # SQL недоступной выборки не показывается (S9)

    def test_no_path_cause_priority_over_generator_flag(self) -> None:
        # порядок причин: дефект → права → нет пути → генератор → техника
        service = self._service()
        node = make_degrade_node(service)
        node(self._state(route_flags={"plan_joins_no_path": True, "generate_sql_failed": True}))
        assert service.kwargs["case"].cause is DegradationCause.NO_PATH

    def test_execute_defect_is_top_priority(self) -> None:
        service = self._service()
        node = make_degrade_node(service)
        node(self._state(route_flags={"execute_defect": True, "plan_joins_no_path": True}))
        assert service.kwargs["case"].cause is DegradationCause.TECHNICAL

    def test_generator_cause_from_flag(self) -> None:
        service = self._service()
        node = make_degrade_node(service)
        node(self._state(route_flags={"generate_sql_failed": True}))
        assert service.kwargs["case"].cause is DegradationCause.GENERATOR

    def test_default_cause_is_technical(self) -> None:
        service = self._service()
        node = make_degrade_node(service)
        node(self._state())  # futile-разбор diagnose без флагов — техника
        assert service.kwargs["case"].cause is DegradationCause.TECHNICAL

    def test_writes_final_answer_and_budget(self) -> None:
        service = self._service()
        node = make_degrade_node(service, budget_guard=_StaticGuard())
        updated = node(self._state(validation={"outcome": "denied", "issues": []}))
        answer = updated[FINAL_ANSWER_KEY]
        assert answer["text"] == "Часть данных недоступна."
        assert answer["cause"] == "rights"
        assert answer["cost_rub"] == 0.42
        assert updated["budget_used"]["llm_calls"] == 4  # 3 + 1 вызов формулировки
        assert updated["budget_used"]["rub"] == 0.42

    def test_events(self) -> None:
        tracer = RecordingTracer()
        node = make_degrade_node(self._service(), tracer=tracer)
        node(self._state(validation={"outcome": "denied", "issues": []}))
        verdict = next(t for t in tracer.events if t.event == EVENT_DEGRADE_VERDICT)
        assert verdict.payload["cause"] == "rights"
        assert verdict.payload["has_variant"] is True

    def test_flag_literals_parity_with_routing(self) -> None:
        # I2 fix-волны (прецедент test_generate_sql): degrade_node дублирует имена
        # флагов routing литералами (application не импортирует orchestration) —
        # совпадение строк гвардит тест, переименование в routing не пройдёт
        # молча (иначе причина отказа классифицируется неверно: NO_PATH → TECHNICAL)
        from text_to_sql_agent.application.services.degrade_node import (
            _DENIED_OUTCOME,
            _EXECUTE_DEFECT_FLAG,
            _GENERATOR_FAILED_FLAG,
            _NO_PATH_FLAG,
        )
        from text_to_sql_agent.domain.sql_validation import ValidationOutcome
        from text_to_sql_agent.orchestration.routing import (
            FLAG_EXECUTE_DEFECT,
            FLAG_GENERATE_SQL_FAILED,
            FLAG_PLAN_JOINS_NO_PATH,
        )

        assert _EXECUTE_DEFECT_FLAG == FLAG_EXECUTE_DEFECT
        assert _NO_PATH_FLAG == FLAG_PLAN_JOINS_NO_PATH
        assert _GENERATOR_FAILED_FLAG == FLAG_GENERATE_SQL_FAILED
        assert _DENIED_OUTCOME == ValidationOutcome.DENIED.value

    def test_llm_unavailable_still_answers_by_template(self) -> None:
        # NFR-8: degrade не падает — шаблон без LLM, событие unavailable
        class _BrokenService:
            def formulate(self, *, case):
                from text_to_sql_agent.application.services.degrade_service import DegradeResult

                return DegradeResult(
                    text=fallback_text(case), used_llm=False, llm_calls=0, error="breaker"
                )

        tracer = RecordingTracer()
        node = make_degrade_node(_BrokenService(), tracer=tracer)
        updated = node(self._state(validation={"outcome": "denied", "issues": []}))
        assert FINAL_ANSWER_KEY in updated
        assert EVENT_DEGRADE_UNAVAILABLE in tracer.names()


class _ScriptedDegradeService:
    """Двойник DegradeService: фиксированный текст + запись kwargs узла."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.kwargs: dict | None = None

    def formulate(self, *, case):
        from text_to_sql_agent.application.services.degrade_service import DegradeResult

        self.kwargs = {"case": case}
        return DegradeResult(text=self.text, used_llm=True, llm_calls=1, error=None)


# --- R3: incomplete при budget_stop (терминальная карта + exception-fallback) ---


class TestBudgetStopIncomplete:
    def test_terminal_budget_stop_carries_incomplete(self) -> None:
        from text_to_sql_agent.orchestration.graph import make_terminal

        updated = make_terminal("budget_stop")({"compact": {"row_count": 5}})
        assert updated["outcome"] == "budget_stop"
        assert updated["incomplete"] is True  # частичное не выдаётся за полное

    def test_other_terminals_do_not_set_incomplete(self) -> None:
        from text_to_sql_agent.orchestration.graph import make_terminal

        updated = make_terminal("done")({})
        assert "incomplete" not in updated

    def test_answer_question_budget_fallback_sets_incomplete(self) -> None:
        # exception-fallback (BudgetExceededError мимо графа) — тот же флаг (R3)
        from text_to_sql_agent.application.use_cases.answer_question import AnswerQuestion

        class _Runner:
            def run(self, state, *, thread_id=None):
                raise BudgetExceededError("кап исчерпан")

        class _LLM:
            def reset_request_budget(self):
                return None

        result = AnswerQuestion(runner=_Runner(), llm=_LLM()).run("вопрос", user_id="u1")
        assert result.outcome == "budget_stop"
        assert result.state["incomplete"] is True


# --- Task 4: BudgetGuard — реализация над аккумулятором S03 (R4) ---


class TestTokenBudgetGuard:
    def _guard(self, spent: float = 0.0):
        from text_to_sql_agent.infrastructure.token_budget import TokenBudgetGuard

        holder = {"spent": spent, "resets": 0}
        guard = TokenBudgetGuard(
            cap_rub=2.0,
            spent_getter=lambda: holder["spent"],
            resetter=lambda: holder.__setitem__("resets", holder["resets"] + 1),
        )
        return guard, holder

    def test_satisfies_port(self) -> None:
        from text_to_sql_agent.application.ports.budget_guard import BudgetGuard

        guard, _ = self._guard()
        assert isinstance(guard, BudgetGuard)  # runtime_checkable Protocol

    def test_spent_mirrors_accumulator(self) -> None:
        guard, holder = self._guard(spent=0.42)
        assert guard.spent_rub() == pytest.approx(0.42)

    def test_remaining_is_cap_minus_spent(self) -> None:
        guard, holder = self._guard(spent=0.5)
        assert guard.remaining_rub() == pytest.approx(1.5)

    def test_remaining_floored_at_zero(self) -> None:
        # перерасход (округления usage) не даёт отрицательный остаток эскалациям
        guard, _ = self._guard(spent=2.5)
        assert guard.remaining_rub() == 0.0

    def test_reset_delegates_to_entry_point(self) -> None:
        guard, holder = self._guard()
        guard.reset()
        assert holder["resets"] == 1

    def test_seam_callable_compatibility(self) -> None:
        # шов ruling Q7: сервисы принимают Callable[[], float] — bound method
        # порта подставляется без изменения сигнатур (BL-015)
        guard, holder = self._guard(spent=0.25)
        seam: Callable[[], float] = guard.remaining_rub
        assert seam() == pytest.approx(1.75)

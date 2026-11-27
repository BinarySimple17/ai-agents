"""Юнит-тесты diagnose_error (feat-014, S14 Task 1/3): сервис + узел FSM.

LLM в тестах — только моки (S00 правило 7): скриптованный двойник шлюза
(тул-выходы/сбои) + RecordingTracer. Бюджетный шов эскалации — инъекция
(ruling Q7 ARCH-GATE S09, прецедент generate_sql).
"""

import json

import pytest

from tests.unit.fakes import RecordingTracer
from text_to_sql_agent.application.ports.llm_provider import (
    LLMProviderError,
    LLMResponse,
    ToolCall,
    Usage,
)
from text_to_sql_agent.application.ports.permission_store import RightsUnavailableError
from text_to_sql_agent.application.services.diagnose_error_node import (
    RETRY_GENERATE_SQL_FLAG,
    RETRY_TARGET_FLAG,
    RETRY_TARGET_GENERATE_SQL,
    make_diagnose_error_node,
)
from text_to_sql_agent.application.services.diagnose_error_service import (
    DiagnoseClass,
    DiagnoseService,
)
from text_to_sql_agent.application.services.generate_sql_node import (
    make_generate_sql_node,
)
from text_to_sql_agent.application.services.generate_sql_service import (
    GenerateSqlOutcome,
    GenerateSqlResult,
)

_DIAG_TOOL = "report_diagnosis"


class ScriptedLLM:
    """Двойник LLMProvider: очередь ответов/сбоев + счётчик вызовов по ролям."""

    def __init__(self, script: list[LLMResponse | Exception]) -> None:
        self.script = list(script)
        self.calls: list[tuple[str, object]] = []

    def complete(self, role, messages, *, tools=None):
        self.calls.append((role, messages))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def embed(self, texts):
        return [[0.0]] * len(texts)

    def reset_request_budget(self) -> None:
        return None

    @property
    def exhausted(self) -> bool:
        return not self.script


def _tool_response(error_class: str, hint: str = "исправь условие фильтра") -> LLMResponse:
    arguments = json.dumps({"error_class": error_class, "regeneration_hint": hint})
    return LLMResponse(
        tool_calls=[ToolCall(id="t1", name=_DIAG_TOOL, arguments=arguments)],
        usage=Usage(tokens_in=10, tokens_out=5),
        model="qwen-fake",
    )


def _base_state(**overrides):
    state = {
        "user_id": "u1",
        "trace_id": "tr-1",
        "error": {"class": "timeout", "message": "выполнение превысило лимит времени"},
        "sql": "SELECT count(*) FROM orders JOIN customers ON orders.customer_id = customers.id",
        "validation": None,
        "retry_count": 0,
        "route_flags": {},
        "budget_used": {"llm_calls": 0, "rub": 0.0},
        "diagnosis": None,
    }
    state.update(overrides)
    return state


# --- Сервис: детерминированная пред-классфикация (без LLM) ---


@pytest.mark.parametrize(
    ("raw_class", "expected"),
    [
        ("permission_denied", DiagnoseClass.PERMISSIONS),
        ("read_only", DiagnoseClass.PERMISSIONS),
        ("acl_denied", DiagnoseClass.PERMISSIONS),
        ("connection", DiagnoseClass.CONNECTION),
    ],
)
def test_futile_classes_skip_llm(raw_class: str, expected: DiagnoseClass) -> None:
    """Права/инфраструктура — повтор бессмыслен: ретрай LLM не тратится (ADR-005, S00-8)."""
    llm = ScriptedLLM([])
    service = DiagnoseService(llm=llm)
    result = service.diagnose(error_class=raw_class, error_message="отказ")
    assert result.verdict is not None
    assert result.verdict.error_class is expected
    assert result.verdict.retry_futile is True
    assert result.llm_calls == 0
    assert llm.calls == []


def test_unknown_class_falls_back_to_service_llm() -> None:
    llm = ScriptedLLM([_tool_response("syntax")])
    service = DiagnoseService(llm=llm)
    result = service.diagnose(error_class="unknown", error_message="что-то сломалось")
    assert result.verdict is not None
    assert result.verdict.error_class is DiagnoseClass.SYNTAX
    assert result.verdict.retry_futile is False


# --- Сервис: LLM-разбор через тул (ADR-008) ---


def test_llm_diagnosis_parses_tool_output() -> None:
    # S33: LLM-контур — прочий syntax/timeout/empty; validate_hallucination
    # ушёл в код-подсказку (semantics без LLM) — тест см. test_semantics_hint_*
    llm = ScriptedLLM([_tool_response("timeout", "ужать выборку периодом")])
    service = DiagnoseService(llm=llm)
    result = service.diagnose(
        error_class="timeout",
        error_message="запрос превысил лимит времени",
        sql="SELECT * FROM orders",
    )
    assert result.verdict is not None
    assert result.verdict.error_class is DiagnoseClass.TIMEOUT
    assert result.verdict.retry_futile is False  # A3: вердикт модели ретрабелен по построению
    assert result.llm_calls == 1
    # Контекст ретрая (D-016): последний SQL + ошибка в промпте
    role, messages = llm.calls[0]
    assert role == "cheap"
    prompt = messages[-1]["content"]
    assert "SELECT * FROM orders" in prompt
    assert "превысил лимит времени" in prompt


def test_semantics_hint_built_from_db_detail_and_catalog_no_llm() -> None:
    """S33 A4: «объекта нет» с уровня БД → класс semantics, LLM не вызывается
    (шпион), подсказка кода несёт фактические колонки (ловушка 2: модель не
    видела реальных имён — при temperature 0 она повторяла бы тот же SQL)."""
    llm = ScriptedLLM([])  # шпион: ни одного вызова быть не должно
    service = DiagnoseService(
        llm=llm,
        columns_catalog=lambda tables: {"orders": ("order_id", "customer_id", "total")},
    )
    result = service.diagnose(
        error_class="semantics",
        error_message="по этому вопросу данных не нашлось — проверим условия выборки и повторим",
        error_detail="column orders.client_id does not exist",
        plan_tables=frozenset({"orders", "customers"}),
        sql="SELECT orders.client_id FROM orders",
    )
    assert result.verdict is not None
    assert result.verdict.error_class is DiagnoseClass.SEMANTICS
    assert result.verdict.retry_futile is False  # регенерация оправдана
    assert result.llm_calls == 0
    assert llm.calls == []  # LLM не тратится (аудит A4)
    hint = result.verdict.regeneration_hint
    assert "client_id" in hint  # сырой текст БД — фактическое имя
    assert "customer_id" in hint  # доступные колонки из каталога (мир пользователя)


def test_semantics_hint_from_validator_issues_only() -> None:
    """validate_hallucination: факты валидатора уже несут реальные имена —
    каталог не читается, LLM не тратится (контур 2, S33)."""
    llm = ScriptedLLM([])
    catalog_calls: list[frozenset[str]] = []

    def _catalog(tables: frozenset[str]) -> dict[str, frozenset[str]]:
        catalog_calls.append(tables)
        return {}

    service = DiagnoseService(llm=llm, columns_catalog=_catalog)
    result = service.diagnose(
        error_class="validate_hallucination",
        error_message="часть данных по этому вопросу вам недоступна — повторите позже",
        validation_issues=(
            "колонка client_id отсутствует в мире агента — у orders доступные: id, total",
        ),
    )
    assert result.verdict is not None
    assert result.verdict.error_class is DiagnoseClass.SEMANTICS
    assert result.llm_calls == 0
    assert catalog_calls == []  # факты валидатора достаточны — шов не читается
    assert "client_id" in result.verdict.regeneration_hint


def test_semantics_hint_survives_catalog_failure() -> None:
    """Сбой шва каталога не роняет разбор: подсказка без раздела «доступные
    колонки», факты из detail остаются (честный fallback, R2 S33)."""
    llm = ScriptedLLM([])

    def boom(tables: frozenset[str]) -> dict[str, frozenset[str]]:
        raise RightsUnavailableError("db down")

    service = DiagnoseService(llm=llm, columns_catalog=boom)
    result = service.diagnose(
        error_class="semantics",
        error_message="фиксированный текст",
        error_detail="column orders.client_id does not exist",
        plan_tables=frozenset({"orders"}),
    )
    assert result.verdict is not None
    hint = result.verdict.regeneration_hint
    assert "client_id" in hint
    assert "доступные колонки" not in hint


def test_probe_context_flows_into_prompt() -> None:
    """Факты COUNT-пробы (error["probe"], ревью Important-1) — в контексте разбора."""
    llm = ScriptedLLM([_tool_response("empty", "проверь условия джойна и фильтры")])
    service = DiagnoseService(llm=llm)
    result = service.diagnose(
        error_class="empty",
        error_message="по этому вопросу данных не нашлось — проверим условия выборки и повторим",
        sql="SELECT orders.order_id FROM orders JOIN order_items ON ...",
        probe_context="проба COUNT по таблицам плана — orders: 400, order_items: 1000",
    )
    assert result.verdict is not None
    assert result.verdict.error_class is DiagnoseClass.EMPTY
    _, messages = llm.calls[0]
    prompt = messages[-1]["content"]
    assert "orders: 400" in prompt  # факты пробы видны диагносту
    assert "проба COUNT" in prompt


def test_db_detail_flows_into_prompt_for_llm_classes() -> None:
    """S33: сырой текст БД (error["detail"]) — в промпте LLM-классов: модель
    видит фактические имена (фиксированный message их больше не несёт)."""
    llm = ScriptedLLM([_tool_response("timeout", "ужать")])
    service = DiagnoseService(llm=llm)
    result = service.diagnose(
        error_class="timeout",
        error_message="запрос оказался слишком тяжёлым — попробуйте сузить вопрос",
        error_detail="canceling statement due to statement timeout",
    )
    assert result.verdict is not None
    _, messages = llm.calls[0]
    prompt = messages[-1]["content"]
    assert "canceling statement due to statement timeout" in prompt


def test_retry_futile_is_code_decision_not_model() -> None:
    """Регрессия A3 (падает на до-фиксовом коде): модель возвращает класс
    «permissions» — до фикса ретрай снимался выходом модели (retry_futile=True
    от класса LLM); после — класс вне enum тула отклоняется приёмкой (M1),
    futile-вердикт может дать только детерминированная карта по классу ошибки.
    """

    class FutileTool:
        def complete(self, role, messages, *, tools=None):
            return _tool_response("permissions", "вне прав")

        def embed(self, texts):
            return [[0.0]]

        def reset_request_budget(self) -> None:
            return None

    service = DiagnoseService(llm=FutileTool())
    result = service.diagnose(error_class="timeout", error_message="таймаут")
    # Верdict модели с классом permissions НЕ принимается: M1 исчерпан — честный
    # «разбор не удался», ретрай не назначается (деградация — валидный исход),
    # а не futile-вердикт от поля модели
    assert result.verdict is None
    assert result.llm_calls == 3  # все попытки M1 ушли на отклонённые выходы


@pytest.mark.parametrize(
    "bad_response",
    [
        LLMResponse(text="просто текст без тула", model="qwen-fake"),  # reasoning-only (D-021)
        _tool_response("not_a_class"),  # класс вне enum
        _tool_response("syntax", "   "),  # пустая подсказка
    ],
)
def test_invalid_llm_output_retries_m1(bad_response: LLMResponse) -> None:
    """Невалидный выход → ретрай M1: попытка 2 — та же роль + текст ошибки (D-016)."""
    llm = ScriptedLLM([bad_response, _tool_response("syntax", "убери лишнюю скобку")])
    service = DiagnoseService(llm=llm)
    result = service.diagnose(error_class="syntax", error_message="битый SQL")
    assert result.verdict is not None
    assert result.verdict.error_class is DiagnoseClass.SYNTAX
    assert result.llm_calls == 2
    assert len(llm.calls) == 2
    _, second_messages = llm.calls[1]
    assert "Ошибка предыдущей попытки" in second_messages[-1]["content"]


def test_m1_exhaustion_is_honest_unavailable() -> None:
    """Исчерпание M1 на невалидных выходах — разбор не удался, вердикта нет (S00-8)."""
    llm = ScriptedLLM([LLMResponse(text="мусор", model="m")] * 3)
    service = DiagnoseService(llm=llm)
    result = service.diagnose(error_class="syntax", error_message="битый SQL")
    assert result.verdict is None
    assert result.error is not None
    assert result.llm_calls == 3


def test_escalation_attempt3_checks_budget_seam() -> None:
    """Попытка 3 — уровень вверх (cheap→mid) только при остатке выше резерва 0,6 ₽."""

    class RoleSpy(ScriptedLLM):
        def __init__(self, script) -> None:
            super().__init__(script)
            self.seen_roles: list[str] = []

        def complete(self, role, messages, *, tools=None):
            self.seen_roles.append(role)
            return super().complete(role, messages, tools=tools)

    llm = RoleSpy(
        [
            LLMResponse(text="мусор", model="m"),
            LLMResponse(text="мусор", model="m"),
            _tool_response("timeout", "эскалация сработала"),  # S33: enum тула без semantics
        ]
    )
    service = DiagnoseService(llm=llm, budget_remaining_roubles=lambda: 1.5)
    result = service.diagnose(error_class="syntax", error_message="битый SQL")
    assert result.verdict is not None
    assert llm.seen_roles == ["cheap", "cheap", "mid"]


def test_escalation_rejected_when_budget_below_reserve() -> None:
    """Резерв 0,6 ₽ не покрывается — честный отказ без эскалации (ADR-007 §4.3)."""
    llm = ScriptedLLM(
        [
            LLMResponse(text="мусор", model="m"),
            LLMResponse(text="мусор", model="m"),
        ]
    )
    service = DiagnoseService(llm=llm, budget_remaining_roubles=lambda: 0.4)
    result = service.diagnose(error_class="syntax", error_message="битый SQL")
    assert result.verdict is None
    assert len(llm.calls) == 2  # попытка 3 не тратится


def test_llm_unavailable_is_not_crash() -> None:
    llm = ScriptedLLM([LLMProviderError("шлюз недоступен")])
    service = DiagnoseService(llm=llm)
    result = service.diagnose(error_class="timeout", error_message="таймаут")
    assert result.verdict is None
    assert "шлюз недоступен" in (result.error or "")


# --- Узел FSM: флаги ретрая, diagnosis в state, трейс ---


def test_node_sets_retry_flag_for_retryable_class() -> None:
    service = DiagnoseService(llm=ScriptedLLM([_tool_response("timeout", "ужать выборку")]))
    tracer = RecordingTracer()
    node = make_diagnose_error_node(service, tracer=tracer)
    updated = node(_base_state())
    assert updated["route_flags"]["retry_generate_sql"] is True
    assert updated["diagnosis"]["error_class"] == "timeout"
    assert "ужать выборку" in updated["diagnosis"]["hint"]
    assert updated["diagnosis"]["retry_futile"] is False
    assert updated["budget_used"]["llm_calls"] == 1
    assert "diagnose.verdict" in tracer.names()


def test_node_no_retry_flag_when_futile() -> None:
    service = DiagnoseService(llm=ScriptedLLM([]))
    node = make_diagnose_error_node(service)
    updated = node(_base_state(error={"class": "permission_denied", "message": "RLS"}))
    assert "retry_generate_sql" not in updated["route_flags"]
    assert updated["diagnosis"]["retry_futile"] is True
    assert updated["budget_used"]["llm_calls"] == 0


def test_node_no_retry_flag_when_attempts_exhausted() -> None:
    """Fuse ≤ 3 на узел: ретрай-визит 3 исчерпан — флага нет → degrade (routing)."""
    service = DiagnoseService(llm=ScriptedLLM([_tool_response("timeout", "подсказка")]))
    node = make_diagnose_error_node(service)
    updated = node(
        _base_state(
            retry_count=2,
            route_flags={"retry_target": "generate_sql"},  # гейт уже дважды провёл ретрай
        )
    )
    assert "retry_generate_sql" not in updated["route_flags"]


def test_node_retry_target_counter_reset_via_other_target() -> None:
    """Чужой счётчик (ретраился другой узел) не ограничивает генератор — попытка 2."""
    service = DiagnoseService(llm=ScriptedLLM([_tool_response("timeout", "подсказка")]))
    node = make_diagnose_error_node(service)
    updated = node(
        _base_state(
            retry_count=2,
            route_flags={"retry_target": "plan_joins"},
        )
    )
    assert updated["route_flags"]["retry_generate_sql"] is True


def test_node_unavailable_does_not_set_retry_flag() -> None:
    """Разбор не удался (шлюз недоступен) — ретрай не назначается: degrade (S00-8)."""
    service = DiagnoseService(llm=ScriptedLLM([LLMProviderError("шлюз недоступен")]))
    tracer = RecordingTracer()
    node = make_diagnose_error_node(service, tracer=tracer)
    updated = node(_base_state())
    assert "retry_generate_sql" not in updated["route_flags"]
    assert "diagnose.unavailable" in tracer.names()


def test_node_missing_error_is_fail_closed_defect() -> None:
    """diagnose_error без state.error — подмена каркаса: без флага ретрая (degrade-ветка)."""
    service = DiagnoseService(llm=ScriptedLLM([]))
    tracer = RecordingTracer()
    node = make_diagnose_error_node(service, tracer=tracer)
    updated = node(_base_state(error=None))
    assert "retry_generate_sql" not in updated["route_flags"]
    assert "diagnose.unavailable" in tracer.names()


def test_node_missing_identity_is_fail_closed() -> None:
    service = DiagnoseService(llm=ScriptedLLM([]))
    node = make_diagnose_error_node(service)
    updated = node(_base_state(user_id=""))
    assert "retry_generate_sql" not in updated["route_flags"]


def test_node_probe_empty_class_diagnosed_with_hint() -> None:
    """Проба (0 строк, потерянный джойн): класс empty — ретрай с подсказкой (state-agent v1.5)."""
    service = DiagnoseService(llm=ScriptedLLM([_tool_response("empty", "проверь условие джойна")]))
    node = make_diagnose_error_node(service)
    updated = node(
        _base_state(
            error={"class": "empty", "message": "проба: строки есть во всех таблицах, джойн пуст"},
            validation=None,
        )
    )
    assert updated["route_flags"]["retry_generate_sql"] is True
    assert updated["diagnosis"]["error_class"] == "empty"


# --- Parity флагов: application не импортирует orchestration (прецедент S11) ---


def test_flag_names_parity_with_routing() -> None:
    from text_to_sql_agent.orchestration.routing import (  # noqa: PLC0415 — только тест
        FLAG_RETRY_GENERATE_SQL,
        FLAG_RETRY_TARGET,
    )

    assert RETRY_GENERATE_SQL_FLAG == FLAG_RETRY_GENERATE_SQL
    assert RETRY_TARGET_FLAG == FLAG_RETRY_TARGET
    assert RETRY_TARGET_GENERATE_SQL == "generate_sql"


# --- Подсказка доходит до retry-контекста generate_sql (Task 1) ---


class RecordingGenerateService:
    """Двойник генератора: фиксирует kwargs вызова generate()."""

    def __init__(self) -> None:
        self.kwargs: dict | None = None

    def generate(self, **kwargs):
        self.kwargs = kwargs
        return GenerateSqlResult(outcome=GenerateSqlOutcome.SQL, sql="SELECT 1", llm_calls=1)


def test_hint_flows_into_generate_retry_context() -> None:
    """D-016: в контекст ретрая — последний SQL + разбор ошибки (raw + подсказка)."""
    gen_service = RecordingGenerateService()
    node = make_generate_sql_node(gen_service)
    state = {
        "user_id": "u1",
        "question": "Сколько заказов?",
        "skeleton": {
            "tables": ("orders", "customers"),
            "from_table": "orders",
            "join_sections": ("JOIN customers ON orders.customer_id = customers.id",),
            "mandatory_predicates": (),
            "skeleton_hash": "h",
        },
        "assumptions": [],
        "permissions": {"orders": ["id"], "customers": ["id"]},
        "sql": "SELECT bad FROM orders",
        "error": {"class": "timeout", "message": "выполнение превысило лимит времени"},
        "diagnosis": {
            "error_class": "timeout",
            "hint": "ужать выборку периодом",
            "retry_futile": False,
        },
        "retry_count": 1,
        "route_flags": {"retry_target": "generate_sql"},
        "budget_used": {"llm_calls": 0, "rub": 0.0},
        "trace_id": "tr-1",
    }
    node(state)
    assert gen_service.kwargs is not None
    assert gen_service.kwargs["last_sql"] == "SELECT bad FROM orders"
    last_error = gen_service.kwargs["last_error"]
    assert last_error is not None
    assert "выполнение превысило лимит времени" in last_error
    assert "ужать выборку периодом" in last_error


def test_hint_ignored_outside_fsm_retry() -> None:
    """Затерявшаяся diagnosis без активного ретрая не превращается в контекст (прецедент S10)."""
    gen_service = RecordingGenerateService()
    node = make_generate_sql_node(gen_service)
    state = {
        "user_id": "u1",
        "question": "Сколько заказов?",
        "skeleton": {
            "tables": ("orders", "customers"),
            "from_table": "orders",
            "join_sections": ("JOIN customers ON orders.customer_id = customers.id",),
            "mandatory_predicates": (),
            "skeleton_hash": "h",
        },
        "assumptions": [],
        "permissions": {"orders": ["id"], "customers": ["id"]},
        "sql": "SELECT bad FROM orders",
        "error": {"class": "timeout", "message": "ошибка"},
        "diagnosis": {"error_class": "timeout", "hint": "подсказка", "retry_futile": False},
        "retry_count": 0,
        "route_flags": {},
        "budget_used": {"llm_calls": 0, "rub": 0.0},
        "trace_id": "tr-1",
    }
    node(state)
    assert gen_service.kwargs is not None
    assert gen_service.kwargs["last_sql"] is None
    assert gen_service.kwargs["last_error"] is None

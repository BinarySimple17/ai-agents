"""Unit-тесты узла plan_joins (feat-009, S09 Task 1–3) — на фейках, LLM не вызывается.

Task 1 — валидатор domain/join_plan.py: выдуманный джойн (rel_id вне снапшота)
отсекается — тест-инвариант hw-01 §2.1 «LLM не управляет валидацией» (критерий
приёмки); права deny-by-default; ACL колонок соединения; ≤ 4 таблиц; связность.
Task 2 — гибрид на фейках портов: retrieval-нога, графовая, pre-rank по
retrieval-хитам (intent-фильтр кодом), ранжирование тула (ADR-008).
Task 3 — ретраи M1 (D-016): попытка 2 — та же роль + текст ошибки, попытка 3 —
роль вверх при остатке бюджета с резервом 0.6 ₽ (ruling Q7), иначе честный
отказ; исходы вердиктов (нет пути / уточнение / fail) — без выдуманных планов.
Q2 — маппинг left/right реестра ↔ направление обхода (ruling ARCH-GATE S09).
"""

import json
from collections.abc import Callable, Sequence
from typing import Any

import pytest

from text_to_sql_agent.application.ports.edge_proposal_store import EdgeProposalError
from text_to_sql_agent.application.ports.llm_provider import (
    LLMRequestError,
    LLMResponse,
    Role,
    ToolCall,
    Usage,
)
from text_to_sql_agent.application.ports.schema_graph_store import (
    EdgeSemantics,
    JoinEdge,
    JoinPath,
    SchemaGraphError,
)
from text_to_sql_agent.application.ports.vector_store import (
    DocKind,
    KnowledgeDocument,
    VectorStoreError,
)
from text_to_sql_agent.application.services.plan_joins_service import (
    PathRanking,
    PlanJoinsService,
    PlanOutcome,
    _columns_for_direction,
    _parse_ranking,
)
from text_to_sql_agent.domain.entities import RunContext
from text_to_sql_agent.domain.join_plan import (
    MAX_JOIN_PLAN_TABLES,
    JoinPlan,
    JoinPlanEdge,
    validate_join_plan,
)

CTX = RunContext(trace_id="unit-plan-joins", user_id="u_logist")
PERMISSIONS_ALL = {
    "customers": ["*"],
    "orders": ["*"],
    "campaigns": ["*"],
    "order_items": ["*"],
    "products": ["*"],
}
_QUESTION = "клиенты, пришедшие из рекламы"

_FIRST_TOUCH_NOTE = (
    "«пришли из рекламы» = first-touch: первый заказ клиента из ads-кампании; "
    "any-touch завышает цифру; campaign_id надёжен с 2026-03"
)

# Семантика демо-рёбер (миграции 0003/0008, ADR-006 §4.1) — extended-поля Q2.
_REGISTRY_SEM: dict[str, EdgeSemantics] = {
    "e_customers_orders": EdgeSemantics(
        rel_id="e_customers_orders",
        join_type="inner",
        meaning="клиент разместил заказ",
        usage_notes="customers.created_at — критерий «новый клиент» (по created_at)",
        intent_tags=["buyer", "client"],
        left_table="customers",
        left_columns=("customer_id",),
        right_table="orders",
        right_columns=("customer_id",),
        cardinality="1:n",
    ),
    "e_campaigns_orders": EdgeSemantics(
        rel_id="e_campaigns_orders",
        join_type="inner",
        meaning="заказ атрибутирован кампании",
        usage_notes=_FIRST_TOUCH_NOTE,
        intent_tags=["attribution", "ads"],
        left_table="campaigns",
        left_columns=("campaign_id",),
        right_table="orders",
        right_columns=("campaign_id",),
        cardinality="n:1",
    ),
    "e_orders_items": EdgeSemantics(
        rel_id="e_orders_items",
        join_type="inner",
        meaning="заказ раскрывается в позиции",
        usage_notes="агрегация поверх ребра только с предагрегацией в CTE (fanout_guard, ADR-004)",
        intent_tags=["items", "basket"],
        left_table="orders",
        left_columns=("order_id",),
        right_table="order_items",
        right_columns=("order_id",),
        cardinality="1:n",
        fanout_guard=True,
    ),
    "e_products_items": EdgeSemantics(
        rel_id="e_products_items",
        join_type="inner",
        meaning="товар продаётся в позициях заказов",
        usage_notes="агрегация поверх ребра — предагрегация (fanout_guard)",
        intent_tags=["sku", "product"],
        left_table="products",
        left_columns=("product_id",),
        right_table="order_items",
        right_columns=("order_id",),
        cardinality="1:n",
        fanout_guard=True,
    ),
    "e_employees_bonuses": EdgeSemantics(
        rel_id="e_employees_bonuses",
        join_type="left",
        meaning="сотрудник получил премию (или нет)",
        usage_notes="LEFT JOIN … IS NULL — «не был премирован»; bonuses не у всех сотрудников",
        intent_tags=["bonus", "hr"],
        left_table="employees",
        left_columns=("employee_id",),
        right_table="bonuses",
        right_columns=("employee_id",),
        cardinality="1:n",
    ),
}


def _path(*edges: tuple[str, str, str, float], total: float) -> JoinPath:
    """JoinPath из кортежей (rel_id, from, to, weight)."""
    return JoinPath(
        edges=[JoinEdge(rel_id=r, from_table=f, to_table=t, weight=w) for r, f, t, w in edges],
        total_weight=total,
    )


_DEMO_PATH = _path(
    ("e_customers_orders", "customers", "orders", 1.0),
    ("e_campaigns_orders", "orders", "campaigns", 1.2),
    total=2.2,
)


class ScriptedLLM:
    """Fake LLMProvider: embed возвращает константу, complete — сценарий ответов."""

    def __init__(
        self, *, embedding: list[float] | None = None, results: list[LLMResponse] | None = None
    ) -> None:
        self.embed_calls: list[Sequence[str]] = []
        self.complete_calls: list[tuple[Role, list[dict[str, str]], Any]] = []
        self._embedding = embedding or [0.1, 0.2, 0.3, 0.4]
        self._results = list(results or [])

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.embed_calls.append(texts)
        return [list(self._embedding) for _ in texts]

    def complete(self, role: Role, messages: Any, *, tools: Any = None) -> LLMResponse:
        self.complete_calls.append((role, list(messages), tools))
        if not self._results:
            raise AssertionError("complete вызван чаще, чем сценарий")
        return self._results.pop(0)


class StaticVectorStore:
    """Fake VectorStore: фиксированные документы (или сбой)."""

    def __init__(self, documents: list[KnowledgeDocument] | Exception) -> None:
        self._documents = documents

    def search(
        self, query_embedding: list[float], *, ctx: RunContext, top_k: int
    ) -> list[KnowledgeDocument]:
        if isinstance(self._documents, Exception):
            raise self._documents
        return self._documents[:top_k]


class ScriptedGraphStore:
    """Fake SchemaGraphStore: пути по парам + семантика по rel_id (или сбои).

    version — ревизия снапшота; sequence версий (список) имитирует конкурентный
    sync-graph между запросами (регресс ревью I6).
    """

    def __init__(
        self,
        paths: dict[tuple[str, str], list[JoinPath]],
        *,
        version: int | list[int] = 13,
        sem: dict[str, EdgeSemantics] | None = None,
        paths_error: str | None = None,
        semantics_error: str | None = None,
    ) -> None:
        self.calls: list[tuple[str, str, frozenset[str], int]] = []
        self._paths = paths
        self._version: int = version if isinstance(version, int) else 0
        self._version_sequence = version if isinstance(version, list) else None
        self._sem = sem or _REGISTRY_SEM
        self._paths_error = paths_error
        self._semantics_error = semantics_error

    def graph_version(self) -> int:
        if self._version_sequence:
            self._version = self._version_sequence.pop(0)
        return self._version

    def find_join_paths(
        self,
        from_table: str,
        to_table: str,
        *,
        ctx: RunContext,
        allowed_tables: frozenset[str],
        k: int,
    ) -> list[JoinPath]:
        self.calls.append((from_table, to_table, allowed_tables, k))
        if self._paths_error:
            raise SchemaGraphError(self._paths_error)
        return self._paths.get((from_table, to_table), [])[:k]

    def path_semantics(self, edge_ids: list[str], *, ctx: RunContext) -> list[EdgeSemantics]:
        if self._semantics_error:
            raise SchemaGraphError(self._semantics_error)
        missing = [rel for rel in edge_ids if rel not in self._sem]
        if missing:
            raise SchemaGraphError(f"рёбра отсутствуют в active-реестре: {missing}")
        return [self._sem[rel] for rel in edge_ids]


class RecordingProposals:
    """Fake EdgeProposalStore: запись вызовов (или сбой)."""

    def __init__(self, error: str | None = None) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self._error = error

    def record_missing_edge(
        self, *, left_table: str, right_table: str, question_sample: str, ctx: RunContext
    ) -> None:
        self.calls.append((left_table, right_table, question_sample))
        if self._error:
            raise EdgeProposalError(self._error)


def _service(
    llm: ScriptedLLM,
    vector_store: StaticVectorStore,
    graph_store: ScriptedGraphStore,
    proposals: RecordingProposals,
    budget: Callable[[], float] | None = None,
) -> PlanJoinsService:
    return PlanJoinsService(
        llm=llm,
        vector_store=vector_store,
        graph_store=graph_store,
        edge_proposals=proposals,
        budget_remaining_roubles=budget,
    )


def _tool_response(args: dict[str, Any] | str) -> LLMResponse:
    arguments = json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else args
    return LLMResponse(
        tool_calls=[ToolCall(id="c1", name="rank_paths", arguments=arguments)],
        usage=Usage(tokens_in=10, tokens_out=5),
        model="fake",
    )


def _text_response(text: str) -> LLMResponse:
    return LLMResponse(text=text, usage=Usage(), model="fake")


def _rel_doc() -> KnowledgeDocument:
    return KnowledgeDocument(
        doc_kind=DocKind.RELATIONSHIP,
        table_name="orders",
        referenced_tables=["orders", "campaigns"],
        content="заказ атрибутирован кампании (first-touch)",
        model_version="bge-m3",
        source_ref="rel:e_campaigns_orders",
        score=0.9,
    )


def _ddl_doc(table: str) -> KnowledgeDocument:
    return KnowledgeDocument(
        doc_kind=DocKind.DDL,
        table_name=table,
        referenced_tables=[table],
        content=f"DDL {table}",
        model_version="bge-m3",
        source_ref=f"ddl:{table}",
        score=0.5,
    )


def _demo_docs() -> list[KnowledgeDocument]:
    return [_rel_doc(), _ddl_doc("customers"), _ddl_doc("orders")]


# --- Task 1: валидатор (domain/join_plan.py) ---


def _demo_plan(**overrides: Any) -> JoinPlan:
    edges = [
        JoinPlanEdge(
            rel_id="e_customers_orders",
            from_table="customers",
            to_table="orders",
            from_columns=("customer_id",),
            to_columns=("customer_id",),
            join_type="inner",
        ),
        JoinPlanEdge(
            rel_id="e_campaigns_orders",
            from_table="orders",
            to_table="campaigns",
            from_columns=("campaign_id",),
            to_columns=("campaign_id",),
            join_type="inner",
        ),
    ]
    fields: dict[str, Any] = {
        "tables": ["customers", "orders", "campaigns"],
        "edges": edges,
        "graph_version": 13,
    }
    fields.update(overrides)
    return JoinPlan(**fields)


def test_validator_accepts_consistent_plan() -> None:
    assert (
        validate_join_plan(
            _demo_plan(),
            permissions=PERMISSIONS_ALL,
            active_rel_ids=frozenset({"e_customers_orders", "e_campaigns_orders"}),
        )
        == []
    )


def test_validator_cuts_invented_join() -> None:
    """Критерий приёмки: подсунутый выдуманный джойн отсекается (hw-01 §2.1)."""
    hallucinated = JoinPlanEdge(
        rel_id="e_customers_campaigns",
        from_table="customers",
        to_table="campaigns",
        from_columns=("customer_id",),
        to_columns=("campaign_id",),
        join_type="inner",
    )
    issues = validate_join_plan(
        _demo_plan(edges=[hallucinated], tables=["customers", "campaigns"]),
        permissions=PERMISSIONS_ALL,
        active_rel_ids=frozenset({"e_customers_orders", "e_campaigns_orders"}),
    )
    assert any("выдуманный джойн" in issue and "e_customers_campaigns" in issue for issue in issues)


def test_validator_denies_table_outside_rights() -> None:
    issues = validate_join_plan(
        _demo_plan(),
        permissions={"customers": ["*"], "orders": ["*"]},  # campaigns нет в правах
        active_rel_ids=frozenset({"e_customers_orders", "e_campaigns_orders"}),
    )
    assert any("campaigns" in issue and "вне прав" in issue for issue in issues)


def test_validator_join_key_acl_blocks_edge() -> None:
    """ACL колонок соединения: ребро без доступа к ключам недоступно целиком."""
    issues = validate_join_plan(
        _demo_plan(),
        permissions={
            "customers": ["customer_id", "name"],
            "orders": ["customer_id", "campaign_id", "total"],
            "campaigns": ["campaign_id"],
        },
        active_rel_ids=frozenset({"e_customers_orders", "e_campaigns_orders"}),
    )
    assert issues == []  # все ключи джойна покрываются перечисленными колонками

    issues = validate_join_plan(
        _demo_plan(),
        permissions={
            "customers": ["name"],
            "orders": ["customer_id"],
            "campaigns": ["campaign_id"],
        },
        active_rel_ids=frozenset({"e_customers_orders", "e_campaigns_orders"}),
    )
    assert any("customer_id" in issue and "недоступно целиком" in issue for issue in issues)


def test_join_plan_model_caps_four_tables() -> None:
    """Структурная граница — в самой модели (pydantic max_length): 5-я таблица не строится."""
    from pydantic import ValidationError

    tables = ["customers", "orders", "campaigns", "order_items", "products"]
    with pytest.raises(ValidationError):
        _demo_plan(tables=tables)
    assert MAX_JOIN_PLAN_TABLES == 4  # инвариант hw-01 (JoinPlan ≤ 4 таблиц)


def test_validator_connectivity_and_dangling() -> None:
    # связность: ребро соединяет customers→orders, campaigns вне цепочки
    partial = JoinPlanEdge(
        rel_id="e_customers_orders",
        from_table="customers",
        to_table="orders",
        from_columns=("customer_id",),
        to_columns=("customer_id",),
        join_type="inner",
    )
    issues = validate_join_plan(
        _demo_plan(tables=["customers", "orders", "campaigns"], edges=[partial]),
        permissions=PERMISSIONS_ALL,
        active_rel_ids=frozenset({"e_customers_orders"}),
    )
    assert any("не связный" in issue for issue in issues)
    assert any("campaigns" in issue and "не задействованы" in issue for issue in issues)

    # фантом: ребро ссылается на таблицу вне списка плана
    issues = validate_join_plan(
        _demo_plan(tables=["customers", "orders"]),
        permissions=PERMISSIONS_ALL,
        active_rel_ids=frozenset({"e_customers_orders", "e_campaigns_orders"}),
    )
    assert any("вне списка плана" in issue and "campaigns" in issue for issue in issues)


# --- Q2: маппинг left/right реестра ↔ направление обхода ---


def test_columns_follow_natural_direction() -> None:
    edge = JoinEdge(rel_id="r1", from_table="customers", to_table="orders", weight=1.0)
    from_columns, to_columns = _columns_for_direction(edge, _REGISTRY_SEM["e_customers_orders"])
    assert from_columns == ("customer_id",) and to_columns == ("customer_id",)

    sem = EdgeSemantics(
        rel_id="r2",
        join_type="inner",
        meaning="m",
        left_table="a",
        left_columns=("a_key",),
        right_table="b",
        right_columns=("b_key",),
    )
    natural = JoinEdge(rel_id="r2", from_table="a", to_table="b", weight=1.0)
    assert _columns_for_direction(natural, sem) == (("a_key",), ("b_key",))


def test_columns_follow_reversed_direction() -> None:
    """Обход против реестра: ключи разворачиваются (JoinEdge развёрнут, джойн симметричен)."""
    sem = EdgeSemantics(
        rel_id="r3",
        join_type="inner",
        meaning="m",
        left_table="campaigns",
        left_columns=("camp_key",),
        right_table="orders",
        right_columns=("ord_key",),
    )
    reversed_edge = JoinEdge(rel_id="r3", from_table="orders", to_table="campaigns", weight=1.0)
    assert _columns_for_direction(reversed_edge, sem) == (("ord_key",), ("camp_key",))


def test_columns_fail_closed_on_registry_drift() -> None:
    sem = EdgeSemantics(
        rel_id="r4", join_type="inner", meaning="m", left_table="x", left_columns=("k",)
    )
    edge = JoinEdge(rel_id="r4", from_table="a", to_table="b", weight=1.0)
    with pytest.raises(ValueError, match="не совпадает с реестром"):
        _columns_for_direction(edge, sem)


# --- Task 2: гибридный сценарий на фейках ---


def test_demo_question_builds_plan_without_llm_ranking() -> None:
    """Демо ДЗ-04: единственный кандидат → план без вызова шлюза (ранжировать нечего)."""
    llm = ScriptedLLM()
    # Пара (campaigns, customers) — единственная со связным путём; порядок
    # кандидатов задают retrieval-доки ([orders, campaigns, customers]).
    graph = ScriptedGraphStore({("campaigns", "customers"): [_DEMO_PATH]})
    proposals = RecordingProposals()
    result = _service(llm, StaticVectorStore(_demo_docs()), graph, proposals).plan(
        _QUESTION, ctx=CTX, permissions=PERMISSIONS_ALL
    )
    assert result.outcome is PlanOutcome.PLAN
    assert result.graph_version == 13
    assert result.plan is not None
    assert result.plan.tables == ["customers", "orders", "campaigns"]
    assert [edge.rel_id for edge in result.plan.edges] == [
        "e_customers_orders",
        "e_campaigns_orders",
    ]
    assert result.plan.assumptions == [
        "customers.created_at — критерий «новый клиент» (по created_at)",
        _FIRST_TOUCH_NOTE,
    ]
    assert result.hit_edge_ids == ["e_campaigns_orders"]
    # Права переданы в граф deny-by-default (S00 контракт 3)
    assert graph.calls and graph.calls[0][2] == frozenset(PERMISSIONS_ALL)
    assert llm.complete_calls == []  # один кандидат — шлюз не тратится


def test_llm_ranking_selects_winner() -> None:
    path_a = _path(("e_campaigns_orders", "orders", "campaigns", 1.2), total=1.2)
    path_b = _path(("e_customers_orders", "orders", "customers", 1.0), total=1.0)
    path_c = _path(
        ("e_customers_orders", "customers", "orders", 1.0),
        ("e_campaigns_orders", "orders", "campaigns", 1.2),
        total=2.2,
    )
    graph = ScriptedGraphStore(
        {
            ("orders", "campaigns"): [path_a],
            ("orders", "customers"): [path_b],
            ("campaigns", "customers"): [path_c],
        }
    )
    llm = ScriptedLLM(results=[_tool_response({"ranking": [1, 0, 2]})])
    result = _service(llm, StaticVectorStore(_demo_docs()), graph, RecordingProposals()).plan(
        _QUESTION, ctx=CTX, permissions=PERMISSIONS_ALL
    )
    assert result.outcome is PlanOutcome.PLAN
    assert result.plan is not None and result.plan.tables == path_c.tables
    assert [call[0] for call in llm.complete_calls] == ["cheap"]  # попытка 1 — матрица ADR-007
    _, messages, tools = llm.complete_calls[0]
    assert tools is not None and tools[0]["function"]["name"] == "rank_paths"  # ADR-008: тул
    payload = json.loads(messages[1]["content"].split("Кандидаты путей (JSON):\n", 1)[1])
    assert (
        payload[0]["index"] == 0 and payload[0]["tables"] == path_a.tables
    )  # pre-rank: hit-ребро + минимальный план (hops перед весом, ADR-003 §4)


def test_hit_edge_prefilter_ranks_lighter_path_first() -> None:
    """Intent-фильтр кодом (Task 2): путь с retrieval-подтверждённым ребром — вперёд по весу."""
    light_hit = _path(("e_customers_orders", "orders", "customers", 1.0), total=1.0)
    heavy_no_hit = _path(("e_orders_items", "orders", "order_items", 2.0), total=2.0)
    graph = ScriptedGraphStore(
        {
            ("orders", "customers"): [light_hit],
            ("orders", "order_items"): [heavy_no_hit],
        }
    )
    docs = [
        KnowledgeDocument(
            doc_kind=DocKind.RELATIONSHIP,
            table_name="orders",
            referenced_tables=["orders", "customers"],
            content="клиент разместил заказ",
            model_version="bge-m3",
            source_ref="rel:e_customers_orders",
        ),
        _ddl_doc("order_items"),
    ]
    llm = ScriptedLLM(results=[_tool_response({"ranking": [0]})])
    result = _service(llm, StaticVectorStore(docs), graph, RecordingProposals()).plan(
        _QUESTION, ctx=CTX, permissions=PERMISSIONS_ALL
    )
    assert result.outcome is PlanOutcome.PLAN
    _, messages, _ = llm.complete_calls[0]
    payload = json.loads(messages[1]["content"].split("Кандидаты путей (JSON):\n", 1)[1])
    assert payload[0]["tables"] == ["orders", "customers"]  # hit-путь легче, но впереди


def test_weight_tie_clarifies_not_escalates() -> None:
    """Равные кандидаты → исход «уточнение» (ADR-007: не эскалация; interrupt — V4/S08)."""
    path_a = _path(("e_campaigns_orders", "orders", "campaigns", 1.2), total=1.2)
    path_b = _path(("e_customers_orders", "orders", "customers", 1.2), total=1.2)
    graph = ScriptedGraphStore(
        {("orders", "campaigns"): [path_a], ("orders", "customers"): [path_b]}
    )
    llm = ScriptedLLM(results=[_tool_response({"ranking": [0]})])
    result = _service(llm, StaticVectorStore(_demo_docs()), graph, RecordingProposals()).plan(
        _QUESTION, ctx=CTX, permissions=PERMISSIONS_ALL
    )
    assert result.outcome is PlanOutcome.CLARIFY
    assert result.plan is None
    assert "orders↔campaigns" in (result.clarifying_question or "")


def test_llm_ambiguous_uses_its_clarifying_question() -> None:
    path_a = _path(("e_campaigns_orders", "orders", "campaigns", 1.2), total=1.2)
    path_b = _path(("e_customers_orders", "orders", "customers", 1.0), total=1.0)
    graph = ScriptedGraphStore(
        {("orders", "campaigns"): [path_a], ("orders", "customers"): [path_b]}
    )
    llm = ScriptedLLM(
        results=[
            _tool_response(
                {"ranking": [0], "ambiguous": True, "clarifying_question": "Какая атрибуция?"}
            )
        ]
    )
    result = _service(llm, StaticVectorStore(_demo_docs()), graph, RecordingProposals()).plan(
        _QUESTION, ctx=CTX, permissions=PERMISSIONS_ALL
    )
    assert result.outcome is PlanOutcome.CLARIFY
    assert result.clarifying_question == "Какая атрибуция?"


def test_few_candidates_clarifies() -> None:
    result = _service(
        ScriptedLLM(),
        StaticVectorStore([_ddl_doc("customers")]),
        ScriptedGraphStore({}),
        RecordingProposals(),
    ).plan(_QUESTION, ctx=CTX, permissions=PERMISSIONS_ALL)
    assert result.outcome is PlanOutcome.CLARIFY
    assert result.clarifying_question and "Уточните" in result.clarifying_question


# --- Task 3: ретраи M1 (D-016) ---


def _multi_path_graph() -> ScriptedGraphStore:
    path_a = _path(("e_campaigns_orders", "orders", "campaigns", 1.2), total=1.2)
    path_b = _path(("e_customers_orders", "orders", "customers", 1.0), total=1.0)
    return ScriptedGraphStore(
        {("orders", "campaigns"): [path_a], ("orders", "customers"): [path_b]}
    )


def test_retry_m1_same_role_then_escalation() -> None:
    """Попытка 2 — та же роль + текст ошибки; попытка 3 — роль вверх (D-016)."""
    llm = ScriptedLLM(
        results=[
            _text_response("не то"),  # попытка 1: нет тула
            _tool_response('{"ranking": [7]}'),  # попытка 2: выдуманный индекс
            _tool_response({"ranking": [0]}),  # попытка 3 (mid): валидно
        ]
    )
    result = _service(
        llm, StaticVectorStore(_demo_docs()), _multi_path_graph(), RecordingProposals()
    ).plan(_QUESTION, ctx=CTX, permissions=PERMISSIONS_ALL)
    assert result.outcome is PlanOutcome.PLAN
    assert [call[0] for call in llm.complete_calls] == ["cheap", "cheap", "mid"]
    # M1-контекст ретрая: последний выход + текст ошибки, не цепочка попыток
    attempt2 = llm.complete_calls[1][1]
    assert any("Ошибка предыдущей попытки" in m["content"] for m in attempt2)
    assert any("не содержит вызова тула" in m["content"] for m in attempt2)
    assert not any("существующие индексы" in m["content"] for m in attempt2)
    attempt3 = llm.complete_calls[2][1]
    assert any("существующие индексы" in m["content"] for m in attempt3)


def test_escalation_blocked_by_budget_reserve() -> None:
    """Ловушка-4: остаток не покрывает резерв 0.6 ₽ → честный отказ, не эскалация."""
    llm = ScriptedLLM(results=[_text_response("нет"), _text_response("снова нет")])
    result = _service(
        llm,
        StaticVectorStore(_demo_docs()),
        _multi_path_graph(),
        RecordingProposals(),
        budget=lambda: 0.5,
    ).plan(_QUESTION, ctx=CTX, permissions=PERMISSIONS_ALL)
    assert result.outcome is PlanOutcome.FAIL
    assert "0.6" in (result.error or "")
    assert len(llm.complete_calls) == 2  # третья попытка не выполнялась
    assert result.clarifying_question  # fail несёт уточняющий вопрос


def test_escalation_allowed_with_enough_budget() -> None:
    llm = ScriptedLLM(
        results=[
            _text_response("нет"),
            _text_response("снова нет"),
            _tool_response({"ranking": [0]}),
        ]
    )
    result = _service(
        llm,
        StaticVectorStore(_demo_docs()),
        _multi_path_graph(),
        RecordingProposals(),
        budget=lambda: 5.0,
    ).plan(_QUESTION, ctx=CTX, permissions=PERMISSIONS_ALL)
    assert result.outcome is PlanOutcome.PLAN
    assert [call[0] for call in llm.complete_calls] == ["cheap", "cheap", "mid"]


def test_retries_exhausted_honest_fail() -> None:
    """Исчерпание ретраев → fail с уточняющим вопросом, НЕ выдуманный план."""
    llm = ScriptedLLM(results=[_text_response("x"), _text_response("y"), _text_response("z")])
    result = _service(
        llm, StaticVectorStore(_demo_docs()), _multi_path_graph(), RecordingProposals()
    ).plan(_QUESTION, ctx=CTX, permissions=PERMISSIONS_ALL)
    assert result.outcome is PlanOutcome.FAIL
    assert result.plan is None
    assert result.clarifying_question and "переформулируйте" in result.clarifying_question
    assert len(llm.complete_calls) == 3


def test_provider_failure_fails_without_retries() -> None:
    """Политика провайдера исчерпана (backoff/breaker) — ретрай M1 поверх неё не делается."""

    class FailingOnce(ScriptedLLM):
        def complete(self, role: Role, messages: Any, *, tools: Any = None) -> LLMResponse:
            self.complete_calls.append((role, list(messages), tools))
            raise LLMRequestError("шлюз не ответил")

    llm2 = FailingOnce()
    result = _service(
        llm2, StaticVectorStore(_demo_docs()), _multi_path_graph(), RecordingProposals()
    ).plan(_QUESTION, ctx=CTX, permissions=PERMISSIONS_ALL)
    assert result.outcome is PlanOutcome.FAIL
    assert "шлюз не ответил" in (result.error or "")
    assert len(llm2.complete_calls) == 1


# --- Task 4: заявки при «нет активного пути» ---


def test_no_path_records_proposals_and_degrades() -> None:
    proposals = RecordingProposals()
    result = _service(
        ScriptedLLM(), StaticVectorStore(_demo_docs()), ScriptedGraphStore({}), proposals
    ).plan(_QUESTION, ctx=CTX, permissions=PERMISSIONS_ALL)
    assert result.outcome is PlanOutcome.NO_PATH
    assert result.plan is None
    assert {("orders", "campaigns"), ("orders", "customers"), ("campaigns", "customers")} == {
        (left, right) for left, right, _ in proposals.calls
    }
    assert all(sample == _QUESTION for _, _, sample in proposals.calls)
    assert "заявки записаны" in (result.error or "")
    assert result.clarifying_question


def test_no_path_survives_proposal_write_failure() -> None:
    proposals = RecordingProposals(error="заявку записать не удалось: permission denied")
    result = _service(
        ScriptedLLM(), StaticVectorStore(_demo_docs()), ScriptedGraphStore({}), proposals
    ).plan(_QUESTION, ctx=CTX, permissions=PERMISSIONS_ALL)
    assert result.outcome is PlanOutcome.NO_PATH
    assert "заявку записать не удалось" in (result.error or "")


def test_retrieval_failure_fails_without_proposal() -> None:
    """S00 правило 8: retrieval недоступен — fail БЕЗ заявки (пары не известны)."""
    proposals = RecordingProposals()
    result = _service(
        ScriptedLLM(),
        StaticVectorStore(VectorStoreError("pg недоступен")),
        ScriptedGraphStore({}),
        proposals,
    ).plan(_QUESTION, ctx=CTX, permissions=PERMISSIONS_ALL)
    assert result.outcome is PlanOutcome.FAIL
    assert "retrieval недоступен" in (result.error or "")
    assert proposals.calls == []


def test_graph_failure_fails_without_proposal() -> None:
    """Сбой снапшота — не факт отсутствия пути: заявки нет (Ловушка-3)."""
    proposals = RecordingProposals()
    result = _service(
        ScriptedLLM(),
        StaticVectorStore(_demo_docs()),
        ScriptedGraphStore({}, paths_error="Neo4j недоступен"),
        proposals,
    ).plan(_QUESTION, ctx=CTX, permissions=PERMISSIONS_ALL)
    assert result.outcome is PlanOutcome.FAIL
    assert proposals.calls == []


def test_semantics_drift_fails_closed() -> None:
    path = _path(("e_ghost_edge", "orders", "campaigns", 1.0), total=1.0)
    graph = ScriptedGraphStore(
        {("orders", "campaigns"): [path]}, semantics_error="дрейф граф↔реестр"
    )
    result = _service(
        ScriptedLLM(), StaticVectorStore(_demo_docs()), graph, RecordingProposals()
    ).plan(_QUESTION, ctx=CTX, permissions=PERMISSIONS_ALL)
    assert result.outcome is PlanOutcome.FAIL
    assert "дрейф" in (result.error or "")


# --- Разбор выхода тула rank_paths (ADR-008) ---


def test_parse_ranking_contracts() -> None:
    with pytest.raises(ValueError, match="не содержит вызова тула"):
        _parse_ranking(_text_response("json в тексте"), 3)
    double = LLMResponse(
        tool_calls=[
            ToolCall(id="a", name="rank_paths", arguments='{"ranking":[0]}'),
            ToolCall(id="b", name="rank_paths", arguments='{"ranking":[1]}'),
        ],
        model="fake",
    )
    with pytest.raises(ValueError, match="один вызов тула"):
        _parse_ranking(double, 3)
    with pytest.raises(ValueError, match="не JSON"):
        _parse_ranking(_tool_response("{битый"), 3)
    with pytest.raises(ValueError, match="не соответствует схеме"):
        _parse_ranking(_tool_response('{"ranking": "ноль"}'), 3)
    with pytest.raises(ValueError, match="выдуманных путей не существует"):
        _parse_ranking(_tool_response('{"ranking": [9]}'), 3)
    with pytest.raises(ValueError, match="дубликаты индексов"):
        _parse_ranking(_tool_response('{"ranking": [0, 0]}'), 3)
    parsed = _parse_ranking(_tool_response({"ranking": [1, 0], "ambiguous": True}), 3)
    assert parsed == PathRanking(ranking=[1, 0], ambiguous=True)


# --- Fix-волна ревью (регрессионные тесты) ---


def test_snapshot_unloaded_fails_without_proposals() -> None:
    """I1: graph_version=0 — снапшот не загружен; это НЕ «чистое нет пути»: без заявок."""
    proposals = RecordingProposals()
    result = _service(
        ScriptedLLM(),
        StaticVectorStore(_demo_docs()),
        ScriptedGraphStore({}, version=0),
        proposals,
    ).plan(_QUESTION, ctx=CTX, permissions=PERMISSIONS_ALL)
    assert result.outcome is PlanOutcome.FAIL
    assert "sync-graph" in (result.error or "")
    assert proposals.calls == []


def test_snapshot_version_change_fails_closed() -> None:
    """I6: конкурентный sync-graph между запросами пар — пин снапшота ловит, плана нет."""
    graph = ScriptedGraphStore({}, version=[13, 14])
    result = _service(
        ScriptedLLM(), StaticVectorStore(_demo_docs()), graph, RecordingProposals()
    ).plan(_QUESTION, ctx=CTX, permissions=PERMISSIONS_ALL)
    assert result.outcome is PlanOutcome.FAIL
    assert "изменился во время планирования" in (result.error or "")


def test_no_path_without_proposal_write_honest_question() -> None:
    """I2: отказ записи заявки — вопрос не утверждает «заявка отправлена»."""
    proposals = RecordingProposals(error="permission denied")
    result = _service(
        ScriptedLLM(), StaticVectorStore(_demo_docs()), ScriptedGraphStore({}), proposals
    ).plan(_QUESTION, ctx=CTX, permissions=PERMISSIONS_ALL)
    assert result.outcome is PlanOutcome.NO_PATH
    assert "заявка отправлена" not in (result.clarifying_question or "")
    assert "не удалось" in (result.clarifying_question or "")

    ok = _service(
        ScriptedLLM(),
        StaticVectorStore(_demo_docs()),
        ScriptedGraphStore({}),
        RecordingProposals(),
    ).plan(_QUESTION, ctx=CTX, permissions=PERMISSIONS_ALL)
    assert "заявка отправлена" in (ok.clarifying_question or "")


def test_overlong_path_is_filtered_not_raised() -> None:
    """I3: путь длиннее лимита JoinPlan отфильтровывается кодом; все длинные → честный FAIL."""
    five = JoinPath(
        edges=[
            JoinEdge(rel_id=f"r{i}", from_table=f"t{i}", to_table=f"t{i + 1}", weight=1.0)
            for i in range(4)
        ],
        total_weight=4.0,
    )
    # 5 таблиц в пути — без фильтра сборка упала бы ValidationError-ом JoinPlan
    heavy = ScriptedGraphStore(
        {("campaigns", "customers"): [five]},
        sem={f"r{i}": _REGISTRY_SEM["e_customers_orders"] for i in range(4)},
    )
    result = _service(
        ScriptedLLM(), StaticVectorStore(_demo_docs()), heavy, RecordingProposals()
    ).plan(_QUESTION, ctx=CTX, permissions=PERMISSIONS_ALL)
    assert result.outcome is PlanOutcome.FAIL
    assert "длиннее лимита" in (result.error or "")


def test_direction_drift_fails_typed_not_raised() -> None:
    """I3: дрейф направления пути против реестра на уровне сервиса → FAIL, не ValueError."""
    drifted = _path(("e_customers_orders", "orders", "widget", 1.0), total=1.0)
    graph = ScriptedGraphStore({("orders", "customers"): [drifted]})
    result = _service(
        ScriptedLLM(), StaticVectorStore(_demo_docs()), graph, RecordingProposals()
    ).plan(_QUESTION, ctx=CTX, permissions=PERMISSIONS_ALL)
    assert result.outcome is PlanOutcome.FAIL
    assert "сборка плана не удалась" in (result.error or "")


def test_validator_rejects_string_permissions() -> None:
    """I4: строковые колонки — нарушение контракта прав (подстрочный обход ACL)."""
    with pytest.raises(ValueError, match="контракт прав нарушен"):
        validate_join_plan(
            _demo_plan(),
            permissions={"customers": "customer_id,name", "orders": ["*"], "campaigns": ["*"]},
            active_rel_ids=frozenset({"e_customers_orders", "e_campaigns_orders"}),
        )


def test_service_normalizes_csv_permissions() -> None:
    """I4: CSV-строка колонок нормализуется на границе сервиса (agent_permissions.columns)."""
    csv_permissions = {
        "customers": "customer_id,name",
        "orders": "customer_id,order_id,campaign_id,total",
        "campaigns": "campaign_id",
    }
    graph = ScriptedGraphStore({("campaigns", "customers"): [_DEMO_PATH]})
    result = _service(
        ScriptedLLM(), StaticVectorStore(_demo_docs()), graph, RecordingProposals()
    ).plan(_QUESTION, ctx=CTX, permissions=csv_permissions)
    assert result.outcome is PlanOutcome.PLAN
    assert result.plan is not None


def test_temporal_path_ranks_after_plain() -> None:
    """I5: код-фильтр temporal — путь без временных рёбер впереди равного."""
    temporal_sem = EdgeSemantics(
        rel_id="e_customers_orders",
        join_type="inner",
        meaning="клиент разместил заказ",
        left_table="customers",
        left_columns=("customer_id",),
        right_table="orders",
        right_columns=("customer_id",),
        temporal={"mode": "на дату заказа"},
    )
    sem = {**_REGISTRY_SEM, "e_customers_orders": temporal_sem}
    # Веса противопоставлены фильтру (temporal тяжелее) — tie не срабатывает,
    # а порядок pre-fix (по весу) противоречит код-фильтру: тест кусает
    temporal_path = _path(("e_customers_orders", "orders", "customers", 1.2), total=1.2)
    plain_path = _path(("e_orders_items", "orders", "order_items", 1.0), total=1.0)
    graph = ScriptedGraphStore(
        {("orders", "customers"): [temporal_path], ("orders", "order_items"): [plain_path]},
        sem=sem,
    )
    docs = [
        KnowledgeDocument(
            doc_kind=DocKind.RELATIONSHIP,
            table_name="orders",
            referenced_tables=["orders", "customers"],
            content="клиент разместил заказ",
            model_version="bge-m3",
            source_ref="rel:e_customers_orders",
        ),
        KnowledgeDocument(
            doc_kind=DocKind.RELATIONSHIP,
            table_name="orders",
            referenced_tables=["orders", "order_items"],
            content="заказ раскрывается в позиции",
            model_version="bge-m3",
            source_ref="rel:e_orders_items",
        ),
    ]
    llm = ScriptedLLM(results=[_tool_response({"ranking": [0]})])
    result = _service(llm, StaticVectorStore(docs), graph, RecordingProposals()).plan(
        _QUESTION, ctx=CTX, permissions=PERMISSIONS_ALL
    )
    assert result.outcome is PlanOutcome.PLAN
    _, messages, _ = llm.complete_calls[0]
    payload = json.loads(messages[1]["content"].split("Кандидаты путей (JSON):\n", 1)[1])
    assert payload[0]["tables"] == ["orders", "order_items"]  # без temporal — вперёд
    assert result.plan is not None
    assert result.plan.tables == ["orders", "order_items"]


def test_candidate_cap_survives_single_document() -> None:
    """M4: один документ с длинным referenced_tables не уводит кандидатов за кап."""
    fat_doc = KnowledgeDocument(
        doc_kind=DocKind.DDL,
        table_name="customers",
        referenced_tables=[
            "customers",
            "orders",
            "campaigns",
            "order_items",
            "products",
            "employees",
            "bonuses",
        ],
        content="fat",
        model_version="bge-m3",
        source_ref="ddl:customers",
    )
    tables = PlanJoinsService._candidate_tables(
        [fat_doc], set(PERMISSIONS_ALL) | {"employees", "bonuses"}
    )
    assert len(tables) <= 6  # _MAX_CANDIDATE_TABLES — кап соблюлся


def test_proposals_capped_per_run() -> None:
    """M12: 4+ кандидата без путей — заявок не больше капа прогона."""
    many = ["orders", "customers", "campaigns", "order_items", "products", "employees"]
    permissions = {t: ["*"] for t in many}
    docs = [_ddl_doc(t) for t in many]
    proposals = RecordingProposals()
    result = _service(
        ScriptedLLM(), StaticVectorStore(docs), ScriptedGraphStore({}), proposals
    ).plan(_QUESTION, ctx=CTX, permissions=permissions)
    assert result.outcome is PlanOutcome.NO_PATH
    assert len(proposals.calls) == 3  # _MAX_PROPOSALS_PER_RUN


def test_hit_edge_ids_deterministic_order() -> None:
    """M5: диагностика retrieval-ноги — детерминированный порядок рёбер."""
    docs = [
        KnowledgeDocument(
            doc_kind=DocKind.RELATIONSHIP,
            table_name="orders",
            referenced_tables=["orders", "campaigns"],
            content="x",
            model_version="bge-m3",
            source_ref="rel:e_campaigns_orders",
        ),
        KnowledgeDocument(
            doc_kind=DocKind.RELATIONSHIP,
            table_name="orders",
            referenced_tables=["orders", "customers"],
            content="y",
            model_version="bge-m3",
            source_ref="rel:e_customers_orders",
        ),
    ]
    assert PlanJoinsService._hit_edge_ids(docs) == ["e_campaigns_orders", "e_customers_orders"]

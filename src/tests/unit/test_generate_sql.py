"""Юнит-тесты feat-010 (S10): build_skeleton (код) + generate_sql (наполнение скелета).

Покрытие по Tasks плана S10:
- Task 1 — domain/skeleton.py: JOIN-секции из рёбер, mandatory_predicates на
  месте, skeleton_hash стабилен (детерминизм) и чувствителен к реальным
  изменениям (Ловушка 4: нормализация глушит пробел/регистр, не смысл);
  assemble_sql собирает финальный SELECT кодом (ruling R1: модель не пишет джойны);
- Task 2 — сервис: тул fill_skeleton (4 поля), reasoning-only → ретрай (D-021),
  выдуманная таблица → ретрай (ruling R5), LLMProviderError → честный отказ;
- Task 3 — ретраи M1: попытка 2 — mid + текст ошибки (в контексте ТОЛЬКО
  последний выход, контракт 2 S00); попытка 3 — strong при остатке бюджета
  с резервом 0,6 ₽, иначе честный отказ (шов ruling Q7);
- Task 4 — узлы plan_joins/build_skeleton/generate_sql: вердикты → кодовые
  флаги (ruling R2: NO_PATH → degrade, FAIL → exhausted→fail, FAILED → degrade),
  бюджетный учёт (ruling R6), трейс-события; routing-ветки.
"""

import json
from typing import Any

import pytest

from text_to_sql_agent.application.ports.llm_provider import (
    LLMRequestError,
    LLMResponse,
    ToolCall,
    Usage,
)
from text_to_sql_agent.application.ports.tracer import (
    EVENT_GENERATE_SQL_UNAVAILABLE,
    EVENT_GENERATE_SQL_VERDICT,
    EVENT_PLAN_JOINS_UNAVAILABLE,
    EVENT_PLAN_JOINS_VERDICT,
    EVENT_SKELETON_UNAVAILABLE,
)
from text_to_sql_agent.application.ports.vector_store import (
    DocKind,
    KnowledgeDocument,
    VectorStoreError,
)
from text_to_sql_agent.application.services.build_skeleton_node import (
    GENERATE_SQL_FAILED_FLAG as BUILDER_FAILED_FLAG,
)
from text_to_sql_agent.application.services.build_skeleton_node import make_build_skeleton_node
from text_to_sql_agent.application.services.generate_sql_node import (
    GENERATE_SQL_FAILED_FLAG,
    GENERATE_SQL_RETRYABLE_FLAG,
    RETRY_TARGET_FLAG,
    make_generate_sql_node,
)
from text_to_sql_agent.application.services.generate_sql_service import (
    GenerateFailKind,
    GenerateSqlOutcome,
    GenerateSqlResult,
    GenerateSqlService,
)
from text_to_sql_agent.application.services.plan_joins_node import (
    PLAN_JOINS_CLARIFY_FLAG,
    PLAN_JOINS_EXHAUSTED_FLAG,
    PLAN_JOINS_NO_PATH_FLAG,
    make_plan_joins_node,
)
from text_to_sql_agent.application.services.plan_joins_service import (
    PlanJoinsResult,
    PlanOutcome,
)
from text_to_sql_agent.domain.entities import Assumption, RunContext
from text_to_sql_agent.domain.join_plan import JoinPlan, JoinPlanEdge
from text_to_sql_agent.domain.skeleton import (
    assemble_sql,
    build_skeleton,
    skeleton_digest,
    validate_fill_text,
)

QUESTION = "Сколько позиций в каждом заказе?"
CTX = RunContext(trace_id="t-trace", user_id="u_analyst")
PERMISSIONS = {"orders": ["*"], "order_items": ["*"], "customers": ["*"]}


def _plan(**overrides: Any) -> JoinPlan:
    """Валидный план из активных рёбер сидов: orders → order_items."""
    defaults: dict[str, Any] = {
        "tables": ["orders", "order_items"],
        "edges": [
            JoinPlanEdge(
                rel_id="e_orders_items",
                from_table="orders",
                to_table="order_items",
                from_columns=("order_id",),
                to_columns=("order_id",),
                join_type="inner",
            )
        ],
        "graph_version": 3,
        "assumptions": ["агрегация поверх ребра — предагрегация (fanout_guard)"],
    }
    defaults.update(overrides)
    return JoinPlan(**defaults)


# ===================== Task 1: build_skeleton (код) =====================


class TestBuildSkeleton:
    def test_join_sections_from_edges(self) -> None:
        skeleton = build_skeleton(_plan())
        assert skeleton.from_table == "orders"
        assert skeleton.join_sections == (
            "INNER JOIN order_items ON orders.order_id = order_items.order_id",
        )
        assert skeleton.tables == ("orders", "order_items")

    def test_composite_keys_joined_with_and(self) -> None:
        edge = JoinPlanEdge(
            rel_id="r1",
            from_table="a",
            to_table="b",
            from_columns=("k1", "k2"),
            to_columns=("k1", "k2"),
            join_type="left",
            join_operator="=",
        )
        skeleton = build_skeleton(_plan(edges=[edge], tables=["a", "b"]))
        assert skeleton.join_sections == ("LEFT JOIN b ON a.k1 = b.k1 AND a.k2 = b.k2",)

    def test_hash_is_deterministic(self) -> None:
        # Контракт стыка 1: одинаковый план → одинаковый хэш (вход валидатора S11)
        assert build_skeleton(_plan()).skeleton_hash == build_skeleton(_plan()).skeleton_hash

    def test_hash_ignores_whitespace_and_case_noise(self) -> None:
        # Ловушка 4: пробельно-регистровые шумы не меняют хэш (фантомы валидатора)
        edge_normal = JoinPlanEdge(
            rel_id="r1",
            from_table="a",
            to_table="b",
            from_columns=("k1",),
            to_columns=("k1",),
            join_type="inner",
        )
        skeleton = build_skeleton(_plan(edges=[edge_normal], tables=["a", "b"]))
        # «Реальные изменения» той же семантики — другой текст, тот же смысл после
        # нормализации: секция с лишними пробелами/капсом даёт тот же дайджест
        assert skeleton_digest(["INNER   JOIN B ON A.K1 = B.K1"], []) == skeleton.skeleton_hash

    def test_hash_catches_real_change(self) -> None:
        skeleton = build_skeleton(_plan())
        other = build_skeleton(_plan(graph_version=4))
        other_edge = JoinPlanEdge(
            rel_id="e_orders_items",
            from_table="orders",
            to_table="order_items",
            from_columns=("order_id",),
            to_columns=("order_id",),
            join_type="left",  # изменился тип джойна — реальное изменение скелета
        )
        changed = build_skeleton(_plan(edges=[other_edge]))
        assert changed.skeleton_hash != skeleton.skeleton_hash
        # graph_version НЕ входит в скелет (неизменяемая часть — текст рёбер):
        # та же топология под другой версией снапшота — тот же скелет
        assert other.skeleton_hash == skeleton.skeleton_hash

    def test_stored_hash_matches_digest_of_stored_skeleton(self) -> None:
        # Re-review I-r2: дайджест считается по ТОМУ ЖЕ дедуплицированному
        # списку, что хранится — иначе S11-сверка давала фантом на любом
        # многотабличном пути (до-фикса падает на shared-table плане)
        first = JoinPlanEdge(
            rel_id="e_orders_items",
            from_table="orders",
            to_table="order_items",
            from_columns=("order_id",),
            to_columns=("order_id",),
            join_type="inner",
        )
        second = JoinPlanEdge(
            rel_id="e_customers_orders",
            from_table="orders",
            to_table="customers",
            from_columns=("customer_id",),
            to_columns=("customer_id",),
            join_type="inner",
        )
        plan = _plan(
            tables=["orders", "order_items", "customers"],
            edges=[first, second],
            mandatory_predicates={
                "e_orders_items": {"mandt": "100"},
                "e_customers_orders": {"mandt": "100"},
            },
        )
        skeleton = build_skeleton(plan)
        # Канонический контракт S11: digest(хранённого скелета) == хранённый хэш
        assert (
            skeleton_digest(skeleton.join_sections, skeleton.mandatory_predicates)
            == skeleton.skeleton_hash
        )

    def test_reintroduced_table_in_chain_is_defect(self) -> None:
        # Re-review minor: повторный ввод таблицы (параллельное a→b) — дублирующий
        # JOIN; честный ValueError, не невалидный SQL
        first = JoinPlanEdge(
            rel_id="r1",
            from_table="a",
            to_table="b",
            from_columns=("k",),
            to_columns=("k",),
            join_type="inner",
        )
        second = JoinPlanEdge(
            rel_id="r2",
            from_table="a",
            to_table="b",
            from_columns=("k",),
            to_columns=("k",),
            join_type="inner",
        )
        with pytest.raises(ValueError, match="повторно вводит таблицу"):
            build_skeleton(_plan(tables=["a", "b"], edges=[first, second]))

    def test_hash_preserves_case_inside_string_literals(self) -> None:
        # I2 (ревью S10): lowercase НЕ трогает литералы — 'EN' и 'en' в
        # case-sensitive коллации разные значения; хэш обязан их различать
        a = build_skeleton(_plan(mandatory_predicates={"e_orders_items": {"orders.lang": "EN"}}))
        b = build_skeleton(_plan(mandatory_predicates={"e_orders_items": {"orders.lang": "en"}}))
        assert a.skeleton_hash != b.skeleton_hash

    def test_hash_normalizes_before_sorting(self) -> None:
        # I2 (ревью S10): предикаты сортируются ПОСЛЕ нормализации — регистр
        # не меняет порядок сортировки и не порождает фантомный хэш
        set_a = {"mandt": "100", "orders.lang": "EN"}
        # другой порядок dict + регистр КЛЮЧА (ключ вне кавычек — нормализуется);
        # значения-литералы одинаковы (их регистр — реальное изменение, см. выше)
        set_b = {"orders.lang": "EN", "MANDT": "100"}
        a = build_skeleton(_plan(mandatory_predicates={"e_orders_items": set_a}))
        b = build_skeleton(_plan(mandatory_predicates={"e_orders_items": set_b}))
        assert a.skeleton_hash == b.skeleton_hash  # регистр-шум не меняет дайджест

    def test_hash_preserves_whitespace_inside_literals(self) -> None:
        # Re-review minor: «литералы святы» полно — пробелы ВНУТРИ литерала
        # не нормализуются (кавычки разрезают до collapse-whitespace)
        a = build_skeleton(
            _plan(mandatory_predicates={"e_orders_items": {"orders.status": "in progress"}})
        )
        b = build_skeleton(
            _plan(mandatory_predicates={"e_orders_items": {"orders.status": "in  progress"}})
        )
        assert a.skeleton_hash != b.skeleton_hash

    def test_first_table_outside_first_edge_is_defect(self) -> None:
        # I1 (ревью S10): первая таблица плана ≠ начало первого ребра — fail-closed
        # (иначе FROM orders при ребре order_items→orders дал бы дубль FROM)
        with pytest.raises(ValueError, match="не совпадает с началом первого ребра"):
            build_skeleton(_plan(tables=["customers", "orders", "order_items"]))

    def test_reversed_first_edge_is_defect(self) -> None:
        # I1: развёрнутое первое ребро (order_items→orders при FROM orders) —
        # не «FROM orders INNER JOIN orders», а честный ValueError
        reversed_edge = JoinPlanEdge(
            rel_id="e_orders_items",
            from_table="order_items",
            to_table="orders",
            from_columns=("order_id",),
            to_columns=("order_id",),
            join_type="inner",
        )
        with pytest.raises(ValueError, match="не совпадает с началом первого ребра"):
            build_skeleton(_plan(tables=["orders", "order_items"], edges=[reversed_edge]))

    def test_broken_chain_is_defect(self) -> None:
        # I1: второе ребро начинается с не введённой таблицы (план — не цепочка):
        # два JOIN одной таблицы вместо цепочки — fail-closed на дрейф сборки
        first = JoinPlanEdge(
            rel_id="r1",
            from_table="a",
            to_table="b",
            from_columns=("k",),
            to_columns=("k",),
            join_type="inner",
        )
        second = JoinPlanEdge(
            rel_id="r2",
            from_table="c",
            to_table="b",
            from_columns=("k",),
            to_columns=("k",),
            join_type="inner",
        )
        with pytest.raises(ValueError, match="не является цепочкой"):
            build_skeleton(
                _plan(tables=["a", "b", "c"], edges=[first, second]),
            )


class TestMandatoryPredicates:
    def _plan_with_predicates(self, raw: dict[str, Any]) -> JoinPlan:
        return _plan(mandatory_predicates={"e_orders_items": raw})

    def test_unqualified_predicate_applies_to_both_edge_tables(self) -> None:
        # SAP-ключ (MANDT/языковой) фильтрует каждую сторону ребра (ADR-006 §4.7)
        skeleton = build_skeleton(self._plan_with_predicates({"mandt": "100"}))
        assert skeleton.mandatory_predicates == (
            "orders.mandt = '100'",
            "order_items.mandt = '100'",
        )

    def test_qualified_predicate_applies_to_named_table(self) -> None:
        skeleton = build_skeleton(self._plan_with_predicates({"orders.deleted": False}))
        assert skeleton.mandatory_predicates == ("orders.deleted = FALSE",)

    def test_null_renders_is_null_and_strings_are_quoted(self) -> None:
        skeleton = build_skeleton(self._plan_with_predicates({"orders.note": None}))
        assert skeleton.mandatory_predicates == ("orders.note IS NULL",)
        skeleton = build_skeleton(self._plan_with_predicates({"orders.note": "o'brien"}))
        assert skeleton.mandatory_predicates == ("orders.note = 'o''brien'",)

    def test_non_scalar_value_is_fail_closed(self) -> None:
        # Реестр хранит только скалярные константы — список не рендерится молча
        with pytest.raises(ValueError, match="не скаляр"):
            build_skeleton(self._plan_with_predicates({"mandt": ["100"]}))

    def test_predicate_table_outside_edge_is_fail_closed(self) -> None:
        with pytest.raises(ValueError, match="вне ребра"):
            build_skeleton(self._plan_with_predicates({"customers.mandt": "100"}))

    def test_shared_table_predicate_is_deduped(self) -> None:
        # Ревью S10 minor: неквалифицированный предикат рендерится на ОБЕ таблицы
        # КАЖДОГО ребра — общая таблица двух рёбер не должна дать дубль
        first = JoinPlanEdge(
            rel_id="e_orders_items",
            from_table="orders",
            to_table="order_items",
            from_columns=("order_id",),
            to_columns=("order_id",),
            join_type="inner",
        )
        second = JoinPlanEdge(
            rel_id="e_customers_orders",
            from_table="orders",
            to_table="customers",
            from_columns=("customer_id",),
            to_columns=("customer_id",),
            join_type="inner",
        )
        plan = _plan(
            tables=["orders", "order_items", "customers"],
            edges=[first, second],
            mandatory_predicates={
                "e_orders_items": {"mandt": "100"},
                "e_customers_orders": {"mandt": "100"},
            },
        )
        skeleton = build_skeleton(plan)
        assert skeleton.mandatory_predicates == (
            "orders.mandt = '100'",
            "order_items.mandt = '100'",
            "customers.mandt = '100'",
        )

    def test_hash_covers_predicates_sorted(self) -> None:
        # Сортировка предикатов: порядок dict реестра не должен менять хэш
        a = build_skeleton(self._plan_with_predicates({"mandt": "100", "orders.deleted": False}))
        b = build_skeleton(self._plan_with_predicates({"orders.deleted": False, "mandt": "100"}))
        assert a.skeleton_hash == b.skeleton_hash
        # Другое значение предиката — другой хэш (S11 ловит «mandatory_predicates
        # на месте» по этому дайджесту)
        c = build_skeleton(self._plan_with_predicates({"mandt": "200"}))
        assert c.skeleton_hash != a.skeleton_hash


class TestFillBarrier:
    """C1 (ревью S10): текст модели — НЕ доверенный; лексический барьер в DOMAIN —
    assemble_sql «заряжен» ни для кого (комментарий закрывает скелет, дисбаланс
    скобок выносит обязательные предикаты из-под AND, «;» — вторая инструкция)."""

    def test_comments_are_rejected(self) -> None:
        for fragment in ("count(*) /*", "*/ count(*)", "x -- 注", "a /* inline */"):
            with pytest.raises(ValueError, match="комментарий"):
                validate_fill_text(fragment, field="select_list")

    def test_semicolon_is_rejected(self) -> None:
        with pytest.raises(ValueError, match=";"):
            validate_fill_text("1; DROP TABLE orders", field="where")

    def test_unbalanced_parens_are_rejected(self) -> None:
        # Ровно вектор ревью C1: (A AND B) OR TRUE обнуляет mandatory-предикаты
        with pytest.raises(ValueError, match="не сбалансированы"):
            validate_fill_text("1=1) OR (1=1", field="where")
        with pytest.raises(ValueError, match="не сбалансированы"):
            validate_fill_text("(a AND (b", field="group_by")

    def test_balanced_parens_pass(self) -> None:
        validate_fill_text("(a AND b) OR c", field="where")  # не бросает

    def test_assemble_sql_raises_on_hostile_fill(self) -> None:
        # Барьер стоит В assemble_sql — доменная функция не собирает враждебный SQL
        skeleton = build_skeleton(_plan())
        with pytest.raises(ValueError, match="комментарий"):
            assemble_sql(skeleton, select_list="count(*) /*", order_by="*/")
        with pytest.raises(ValueError, match="не сбалансированы"):
            assemble_sql(skeleton, select_list="count(*)", where="1=1) OR (1=1")


class TestAssembleSql:
    def test_assembles_skeleton_with_fill(self) -> None:
        skeleton = build_skeleton(_plan())
        sql = assemble_sql(
            skeleton,
            select_list="orders.order_id, count(*)",
            where="orders.created_at >= '2026-01-01'",
            group_by="orders.order_id",
            order_by="count(*) DESC",
        )
        assert sql.splitlines()[0] == "SELECT orders.order_id, count(*)"
        assert "FROM orders" in sql
        assert "INNER JOIN order_items ON orders.order_id = order_items.order_id" in sql
        assert "WHERE (orders.created_at >= '2026-01-01')" in sql
        assert "GROUP BY orders.order_id" in sql
        assert "ORDER BY count(*) DESC" in sql

    def test_user_where_parenthesised_around_mandatory(self) -> None:
        # OR в пользовательском WHERE не должен разорвать обязательные предикаты
        skeleton = build_skeleton(_plan(mandatory_predicates={"e_orders_items": {"mandt": "100"}}))
        sql = assemble_sql(skeleton, select_list="*", where="a = 1 OR b = 2")
        assert "orders.mandt = '100'" in sql
        assert "order_items.mandt = '100'" in sql
        assert "AND (a = 1 OR b = 2)" in sql  # скобки вокруг OR-выражения

    def test_optional_sections_omitted(self) -> None:
        skeleton = build_skeleton(_plan())
        sql = assemble_sql(skeleton, select_list="count(*)")
        assert "WHERE" not in sql
        assert "GROUP BY" not in sql
        assert "ORDER BY" not in sql


# ===================== Tasks 2–3: сервис generate_sql =====================


class _ScriptedLLM:
    """Фейковый шлюз: скрипт ответов по вызовам + запись (role, messages)."""

    def __init__(self, responses: list[Any], *, fail_after: int | None = None) -> None:
        self._responses = list(responses)
        self._fail_after = fail_after
        self.calls: list[tuple[str, list[dict[str, str]]]] = []
        self.embed_calls = 0

    def complete(self, role: Any, messages: Any, *, tools: Any = None) -> LLMResponse:
        self.calls.append((role, [dict(m) for m in messages]))
        if self._fail_after is not None and len(self.calls) > self._fail_after:
            raise LLMRequestError("шлюз не ответил")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def embed(self, texts: Any) -> list[list[float]]:
        self.embed_calls += 1
        return [[0.01] * 8]

    def reset_request_budget(self) -> None: ...


class _EmptyVectorStore:
    """Фейковый VectorStore: пустая выдача (контекст-блок «не нашлось»)."""

    def __init__(self, documents: list[KnowledgeDocument] | None = None) -> None:
        self._documents = documents or []

    def search(self, *args: Any, **kwargs: Any) -> list[KnowledgeDocument]:
        return self._documents


def _tool_response(*, select: str, where: str | None = None, **extra: str) -> LLMResponse:
    arguments: dict[str, Any] = {"select_list": select}
    if where is not None:
        arguments["where"] = where
    arguments.update(extra)
    return LLMResponse(
        tool_calls=[ToolCall(id="c1", name="fill_skeleton", arguments=json.dumps(arguments))],
        usage=Usage(tokens_in=100, tokens_out=30),
        model="deepseek",
    )


def _service(
    llm: _ScriptedLLM,
    documents: list[KnowledgeDocument] | None = None,
    budget_remaining_roubles: Any = None,
) -> GenerateSqlService:
    return GenerateSqlService(
        llm=llm,
        vector_store=_EmptyVectorStore(documents),
        budget_remaining_roubles=budget_remaining_roubles,
    )


class TestGenerateSqlService:
    def test_happy_path_assembles_sql_from_tool_fields(self) -> None:
        # Behavior: «по скелету и вопросу модель возвращает один валидный по
        # форме SELECT» — модель даёт 4 поля, SQL собирает КОД (ruling R1)
        llm = _ScriptedLLM([_tool_response(select="order_id, count(*)", where="o.x > 1")])
        result = _service(llm).generate(
            skeleton=build_skeleton(_plan()),
            question=QUESTION,
            ctx=CTX,
            permissions=PERMISSIONS,
        )
        assert result.outcome == GenerateSqlOutcome.SQL
        assert result.sql is not None
        assert result.sql.startswith("SELECT order_id, count(*)")
        assert "INNER JOIN order_items ON" in result.sql  # джойн — из скелета
        assert result.llm_calls == 1
        assert result.escalated is False
        assert llm.calls[0][0] == "mid"  # матрица ADR-007 §4.2: generate_sql — mid
        assert llm.embed_calls == 1  # retrieval-нога вызвана

    def test_context_block_in_prompt_and_noise_filtered(self) -> None:
        doc = KnowledgeDocument(
            doc_kind=DocKind.DDL,
            content="CREATE TABLE orders (...)",
            model_version="m",
            source_ref="ddl:orders",
        )
        noise = KnowledgeDocument(
            doc_kind=DocKind.NOISE,
            content="дистрактор",
            model_version="m",
            source_ref="noise:n1",
        )
        llm = _ScriptedLLM([_tool_response(select="count(*)")])
        _service(llm, [doc, noise]).generate(
            skeleton=build_skeleton(_plan()), question=QUESTION, ctx=CTX
        )
        user_text = llm.calls[0][1][1]["content"]
        assert "CREATE TABLE orders" in user_text
        assert "дистрактор" not in user_text  # NOISE в промпт не идёт (S26)

    def test_assumptions_from_clarify_are_in_prompt(self) -> None:
        llm = _ScriptedLLM([_tool_response(select="count(*)")])
        _service(llm).generate(
            skeleton=build_skeleton(_plan()),
            question=QUESTION,
            ctx=CTX,
            assumptions=[Assumption(source="classifier", text="период — 2026")],
        )
        assert "период — 2026" in llm.calls[0][1][1]["content"]

    def test_retry_attempt2_same_role_with_error_and_last_output_only(self) -> None:
        # Контракт 2 S00 (D-016): попытка 2 — та же модель + текст ошибки; в
        # контексте — ТОЛЬКО последний выход + ошибка, не цепочка попыток
        reasoning_only = LLMResponse(text="думаю…", usage=Usage(), model="deepseek")
        llm = _ScriptedLLM(
            [reasoning_only, _tool_response(select="count(*)")],
        )
        result = _service(llm).generate(
            skeleton=build_skeleton(_plan()), question=QUESTION, ctx=CTX
        )
        assert result.outcome == GenerateSqlOutcome.SQL
        assert result.llm_calls == 2
        assert llm.calls[1][0] == "mid"  # попытка 2 — та же роль
        retry_messages = llm.calls[1][1]
        assert any("не прошёл" in m["content"] or "Ошибка" in m["content"] for m in retry_messages)
        assert len(retry_messages) == 4  # system, user, assistant(последний), user(ошибка)
        # последний выход предыдущей попытки передан, цепочки попыток нет
        assert retry_messages[2]["role"] == "assistant"

    def test_reasoning_only_without_tool_fails_after_m1(self) -> None:
        # D-021 (Ловушка 3 S08): reasoning-only текст не «понимается» — 3 попытки → fail
        reasoning_only = LLMResponse(text="думаю…", usage=Usage(), model="deepseek")
        llm = _ScriptedLLM([reasoning_only, reasoning_only, reasoning_only])
        result = _service(llm).generate(
            skeleton=build_skeleton(_plan()), question=QUESTION, ctx=CTX
        )
        assert result.outcome == GenerateSqlOutcome.FAIL
        assert result.llm_calls == 3
        assert result.clarifying_question  # честный отказ с вопросом переформулировки

    def test_invented_table_in_fill_is_rejected_to_retry(self) -> None:
        # Ruling R5: наполнение с таблицей прав, отсутствующей в скелете, —
        # выдуманная для этого запроса → ретрай, не сборка мусорного SQL
        foreign = _tool_response(select="customers.name, count(*)")
        good = _tool_response(select="count(*)")
        llm = _ScriptedLLM([foreign, good])
        result = _service(llm).generate(
            skeleton=build_skeleton(_plan()),
            question=QUESTION,
            ctx=CTX,
            permissions=PERMISSIONS,  # customers ∈ правам, ∉ скелету
        )
        assert result.outcome == GenerateSqlOutcome.SQL  # вторая попытка чистая
        assert result.llm_calls == 2
        # Сборка кода содержит таблицы ТОЛЬКО из скелета — model не пишeт FROM
        assert "customers" not in result.sql

    def test_invented_table_persists_fail_after_m1(self) -> None:
        foreign = _tool_response(select="customers.name")
        llm = _ScriptedLLM([foreign, foreign, foreign])
        result = _service(llm).generate(
            skeleton=build_skeleton(_plan()), question=QUESTION, ctx=CTX, permissions=PERMISSIONS
        )
        assert result.outcome == GenerateSqlOutcome.FAIL
        assert "customers" in result.error

    def test_escalation_attempt3_to_strong_when_budget_allows(self) -> None:
        # Политика M1 (ADR-007 §4.1): попытка 3 — уровень вверх (mid→strong)
        reasoning_only = LLMResponse(text="думаю…", usage=Usage(), model="deepseek")
        llm = _ScriptedLLM([reasoning_only, reasoning_only, _tool_response(select="count(*)")])
        result = _service(llm, budget_remaining_roubles=lambda: 1.0).generate(
            skeleton=build_skeleton(_plan()), question=QUESTION, ctx=CTX
        )
        assert result.outcome == GenerateSqlOutcome.SQL
        assert result.llm_calls == 3
        assert [c[0] for c in llm.calls] == ["mid", "mid", "strong"]  # D-016-лесенка
        assert result.escalated is True

    def test_escalation_denied_when_budget_below_reserve(self) -> None:
        # ADR-007 §4.3: остаток ≤ резерва 0,6 ₽ — эскалации нет, честный отказ
        # (не молчаливое продолжение на mid); strong НЕ вызывается
        reasoning_only = LLMResponse(text="думаю…", usage=Usage(), model="deepseek")
        llm = _ScriptedLLM([reasoning_only, reasoning_only, reasoning_only])
        result = _service(llm, budget_remaining_roubles=lambda: 0.6).generate(
            skeleton=build_skeleton(_plan()), question=QUESTION, ctx=CTX
        )
        assert result.outcome == GenerateSqlOutcome.FAIL
        assert [c[0] for c in llm.calls] == ["mid", "mid"]  # strong не звали
        assert result.llm_calls == 2
        assert "резерв" in result.error

    def test_escalation_unchecked_when_budget_seam_missing(self) -> None:
        # Шов None = документированный fallback (прецедент plan_joins ruling Q7):
        # эскалация без проверки — контур бюджета ещё не подключён
        reasoning_only = LLMResponse(text="думаю…", usage=Usage(), model="deepseek")
        llm = _ScriptedLLM([reasoning_only, reasoning_only, _tool_response(select="count(*)")])
        result = _service(llm, budget_remaining_roubles=None).generate(
            skeleton=build_skeleton(_plan()), question=QUESTION, ctx=CTX
        )
        assert result.outcome == GenerateSqlOutcome.SQL
        assert [c[0] for c in llm.calls] == ["mid", "mid", "strong"]

    def test_fsm_retry_visit_is_exactly_one_attempt(self) -> None:
        # Re-review I-r3: визит FSM-ретрая = РОВНО одна попытка (внутренний
        # цикл — только первый визит); иначе attempt 3 дважды и кап «≤ 3 на
        # узел» ломается (до-фикса визит 2 вгонял попытки 2 и 3 = 2 вызова)
        broken = LLMResponse(text="думаю…", usage=Usage(), model="deepseek")
        llm = _ScriptedLLM([broken, _tool_response(select="count(*)")])
        service = _service(llm, budget_remaining_roubles=lambda: 1.0)
        visit2 = service.generate(
            skeleton=build_skeleton(_plan()),
            question=QUESTION,
            ctx=CTX,
            last_sql="SELECT broken",
            last_error="колонка broken не существует",
            start_attempt=2,
        )
        assert visit2.outcome == GenerateSqlOutcome.FAIL  # битый выход — fail визита
        assert visit2.llm_calls == 1  # БЕЗ внутривизитного ретрая на попытку 3
        visit3 = service.generate(
            skeleton=build_skeleton(_plan()), question=QUESTION, ctx=CTX, start_attempt=3
        )
        assert visit3.outcome == GenerateSqlOutcome.SQL
        assert visit3.llm_calls == 1
        # Ревью-2 M3: первый визит не участвует в этом тесте — проверены
        # ровно-по-одному вызовы визитов 2 и 3 (суммарный кап = M1-лесенка)
        assert [c[0] for c in llm.calls] == ["mid", "strong"]  # 2-я попытка mid, 3-я strong

    def test_llm_provider_error_is_honest_fail(self) -> None:
        # Политика провайдера исчерпана — ретрай M1 поверх неё бессмыслен (S00-8)
        llm = _ScriptedLLM([], fail_after=0)
        result = _service(llm).generate(
            skeleton=build_skeleton(_plan()), question=QUESTION, ctx=CTX
        )
        assert result.outcome == GenerateSqlOutcome.FAIL
        assert result.llm_calls == 1
        assert "шлюз недоступен" in result.error

    def test_retrieval_failure_is_honest_fail_without_llm_complete(self) -> None:
        # Генерация без контекста БЗ — SQL не по семантике реестра: fail (S00-8)
        llm = _ScriptedLLM([])

        class _BrokenStore:
            def search(self, *args: Any, **kwargs: Any) -> Any:
                raise VectorStoreError("поиск недоступен")

        service = GenerateSqlService(llm=llm, vector_store=_BrokenStore())
        result = service.generate(skeleton=build_skeleton(_plan()), question=QUESTION, ctx=CTX)
        assert result.outcome == GenerateSqlOutcome.FAIL
        assert result.llm_calls == 0
        assert llm.calls == []  # complete не звали

    def test_empty_question_fails_before_legs(self) -> None:
        llm = _ScriptedLLM([])
        result = _service(llm).generate(skeleton=build_skeleton(_plan()), question="   ", ctx=CTX)
        assert result.outcome == GenerateSqlOutcome.FAIL
        assert llm.calls == [] and llm.embed_calls == 0

    def test_start_attempt_role_seeded_for_fsm_retry(self) -> None:
        # Ruling R7: FSM-ретрай (S14) сеет стартовую попытку/роль из attempts_used —
        # сервис стартует с попытки 2 (mid + ошибка) и эскалирует на 3
        llm = _ScriptedLLM([_tool_response(select="count(*)")])
        result = _service(llm, budget_remaining_roubles=lambda: 1.0).generate(
            skeleton=build_skeleton(_plan()),
            question=QUESTION,
            ctx=CTX,
            last_sql="SELECT broken",
            last_error="колонка broken не существует",
            start_role="mid",
            start_attempt=2,
        )
        assert result.outcome == GenerateSqlOutcome.SQL
        assert result.llm_calls == 1
        messages = llm.calls[0][1]
        assert any("SELECT broken" in m["content"] for m in messages)
        assert any("колонка broken" in m["content"] for m in messages)

    def test_comment_skeleton_split_is_rejected_never_sql(self) -> None:
        # Регресс C1: «/*» в select_list + «*/» в order_by комментировали бы весь
        # скелет (FROM/JOIN/WHERE mandatory) и собирали валидный чужой SQL.
        # До-фикс: outcome=SQL; пост-фикс: барьер → ретрай × 3 → FAIL
        hostile = _tool_response(select="count(*) /*", order_by="*/")
        llm = _ScriptedLLM([hostile, hostile, hostile])
        result = _service(llm).generate(
            skeleton=build_skeleton(_plan()), question=QUESTION, ctx=CTX, permissions=PERMISSIONS
        )
        assert result.outcome == GenerateSqlOutcome.FAIL
        # Слой отклонения не важен (sqlglot-токенизатор или доменный барьер —
        # «/*» ловят оба): важно, что враждебный наполнение НИКОГДА не становится SQL
        assert "select_list" in result.error
        assert all(role in ("mid", "strong") for role, _ in llm.calls)

    def test_join_in_select_list_is_rejected(self) -> None:
        # Регресс C1 (Ловушка 1 плана): модель дописывает JOIN с собственным ON —
        # «галлюцинация связи» возвращается; до-фикс собрал бы мусорный SQL
        hostile = _tool_response(
            select="count(*) FROM orders x JOIN order_items y ON x.order_id = y.order_id"
        )
        llm = _ScriptedLLM([hostile, hostile, hostile])
        result = _service(llm).generate(
            skeleton=build_skeleton(_plan()), question=QUESTION, ctx=CTX, permissions=PERMISSIONS
        )
        assert result.outcome == GenerateSqlOutcome.FAIL
        assert "структуру SQL" in result.error

    def test_subquery_in_where_is_rejected(self) -> None:
        # Регресс C1: подзапрос в WHERE — та же «структура SQL от модели»
        # Подзапрос по таблицам СКЕЛЕТА — bite по outcome (не по тексту ошибки):
        # R5 таблицы не ловит (обе в плане), только структурная проверка
        hostile = _tool_response(
            select="count(*)", where="order_id IN (SELECT order_id FROM order_items)"
        )
        llm = _ScriptedLLM([hostile, hostile, hostile])
        result = _service(llm).generate(
            skeleton=build_skeleton(_plan()), question=QUESTION, ctx=CTX, permissions=PERMISSIONS
        )
        assert result.outcome == GenerateSqlOutcome.FAIL
        assert "структуру SQL" in result.error

    def test_unbalanced_parens_in_where_is_rejected(self) -> None:
        # Регресс C1: несбалансированная скобка выносит mandatory-предикаты
        # из-под AND ((A AND B) OR TRUE); до-фикс собрал бы такой SQL
        hostile = _tool_response(select="count(*)", where="1=1) OR (1=1")
        llm = _ScriptedLLM([hostile, hostile, hostile])
        result = _service(llm).generate(
            skeleton=build_skeleton(_plan()), question=QUESTION, ctx=CTX, permissions=PERMISSIONS
        )
        assert result.outcome == GenerateSqlOutcome.FAIL

    def test_select_into_is_rejected(self) -> None:
        # Re-review I-r1: SELECT … INTO — DDL-класс, модель создаёт таблицы тем
        # более не может; до-фикса (барьер без Into) outcome=sql
        hostile = _tool_response(select="* INTO evil")
        llm = _ScriptedLLM([hostile, hostile, hostile])
        result = _service(llm).generate(
            skeleton=build_skeleton(_plan()), question=QUESTION, ctx=CTX, permissions=PERMISSIONS
        )
        assert result.outcome == GenerateSqlOutcome.FAIL
        assert "структуру SQL" in result.error

    def test_valid_fill_with_function_calls_still_passes(self) -> None:
        # Уровень строгости: агрегаты/выражения/скобки валидны — барьер не
        # душит нормальное наполнение
        good = _tool_response(
            select="orders.customer_id, count(*)",
            where="orders.total > 100",
            group_by="orders.customer_id",
        )
        llm = _ScriptedLLM([good])
        result = _service(llm).generate(
            skeleton=build_skeleton(_plan()), question=QUESTION, ctx=CTX, permissions=PERMISSIONS
        )
        assert result.outcome == GenerateSqlOutcome.SQL

    def test_start_attempt_beyond_cap_is_honest_fail(self) -> None:
        llm = _ScriptedLLM([_tool_response(select="count(*)")])
        result = _service(llm).generate(
            skeleton=build_skeleton(_plan()), question=QUESTION, ctx=CTX, start_attempt=4
        )
        assert result.outcome == GenerateSqlOutcome.FAIL
        assert llm.calls == []  # вызовов нет — попытки исчерпаны до сервиса


# ===================== Task 4: узлы + routing =====================


class _ScriptedPlanJoins:
    """Скриптовый планер с записью входов (ревью S10 I6): узел обязан передать
    question/ctx/permissions из state — молчаливая потеря permissions отключила бы
    defense-in-depth (ruling R5)."""

    def __init__(self, result: PlanJoinsResult) -> None:
        self._result = result
        self.called = 0
        self.seen: list[dict[str, Any]] = []

    def plan(self, question: str, *, ctx: Any, permissions: Any) -> PlanJoinsResult:
        self.called += 1
        self.seen.append({"question": question, "ctx": ctx, "permissions": permissions})
        return self._result


class _RecordingTracer:
    def __init__(self) -> None:
        self.records: list[Any] = []

    def record(self, trace: Any) -> None:
        self.records.append(trace)


class TestPlanJoinsNode:
    def test_plan_outcome_writes_join_plan_and_clean_flags(self) -> None:
        tracer = _RecordingTracer()
        service = _ScriptedPlanJoins(
            PlanJoinsResult(
                outcome=PlanOutcome.PLAN,
                plan=_plan(),
                graph_version=3,
                llm_calls=2,
            )
        )
        node = make_plan_joins_node(service, tracer=tracer)
        updated = node(
            {
                "question": QUESTION,
                "user_id": "u_analyst",
                "permissions": PERMISSIONS,
                "budget_used": {"llm_calls": 1},
                "trace_id": "t-1",
            }
        )
        assert updated["join_plan"]["tables"] == ["orders", "order_items"]
        assert updated["route_flags"]["plan_joins_invalid"] is False
        assert updated["route_flags"]["plan_joins_no_path"] is False
        assert updated["error"] is None
        assert updated["budget_used"]["llm_calls"] == 3  # 1 + result.llm_calls (R6)
        verdict = [t for t in tracer.records if t.event == EVENT_PLAN_JOINS_VERDICT]
        assert len(verdict) == 1
        assert verdict[0].payload["outcome"] == "plan"
        assert verdict[0].payload["graph_version"] == 3
        # I6 (ревью S10): входы дошли до сервиса из state
        assert service.seen[0]["question"] == QUESTION
        assert service.seen[0]["permissions"] == PERMISSIONS
        assert service.seen[0]["ctx"].user_id == "u_analyst"

    def test_clarify_outcome_writes_question_with_flag(self) -> None:
        # Правило карты владения S08: планер пишет clarify_question ВМЕСТЕ
        # с флагом (иначе clarify выполнит повторную классификацию)
        service = _ScriptedPlanJoins(
            PlanJoinsResult(outcome=PlanOutcome.CLARIFY, clarifying_question="какие кампании?")
        )
        updated = make_plan_joins_node(service)({"question": QUESTION, "user_id": "u_analyst"})
        assert updated["clarify_question"] == "какие кампании?"
        assert updated["route_flags"][PLAN_JOINS_CLARIFY_FLAG] is True

    def test_no_path_outcome_flags_degrade_branch(self) -> None:
        # Ruling R2: «нет активного пути» → degrade (behavior feat-009 + заявка)
        tracer = _RecordingTracer()
        service = _ScriptedPlanJoins(
            PlanJoinsResult(outcome=PlanOutcome.NO_PATH, error="активных рёбер нет")
        )
        updated = make_plan_joins_node(service, tracer=tracer)(
            {"question": QUESTION, "user_id": "u_analyst"}
        )
        assert updated["route_flags"][PLAN_JOINS_NO_PATH_FLAG] is True
        assert updated["error"]["class"] == "plan_no_path"
        assert "рёбер" not in updated["error"]["message"]  # S9: фиксированный текст
        events = [t for t in tracer.records if t.event == EVENT_PLAN_JOINS_UNAVAILABLE]
        assert len(events) == 1
        assert "рёбер" in events[0].payload["detail"]  # деталь — оператору

    def test_fail_outcome_flags_exhausted_terminal(self) -> None:
        # Ruling R2: FAIL планера → error в state + exhausted → terminal_fail
        service = _ScriptedPlanJoins(
            PlanJoinsResult(outcome=PlanOutcome.FAIL, error="шлюз недоступен")
        )
        updated = make_plan_joins_node(service)({"question": QUESTION, "user_id": "u_analyst"})
        assert updated["route_flags"][PLAN_JOINS_EXHAUSTED_FLAG] is True
        assert updated["error"]["class"] == "plan_failed"

    def test_missing_identity_is_fail_closed(self) -> None:
        service = _ScriptedPlanJoins(PlanJoinsResult(outcome=PlanOutcome.PLAN, plan=_plan()))
        updated = make_plan_joins_node(service)({"question": QUESTION, "user_id": ""})
        assert updated["route_flags"][PLAN_JOINS_EXHAUSTED_FLAG] is True
        assert updated["error"]["class"] == "identity_missing"
        assert service.called == 0

    def test_empty_question_is_fail_closed_before_legs(self) -> None:
        # Прецедент clarify M-r6b: пустой вопрос дошёл бы до VectorStore.search
        service = _ScriptedPlanJoins(PlanJoinsResult(outcome=PlanOutcome.PLAN, plan=_plan()))
        for question in ("", "   "):
            updated = make_plan_joins_node(service)({"question": question, "user_id": "u_analyst"})
            assert updated["error"]["class"] == "plan_empty_question"
        assert service.called == 0


class TestBuildSkeletonNode:
    def test_writes_skeleton_and_hash_into_state(self) -> None:
        node = make_build_skeleton_node()
        updated = node({"join_plan": _plan().model_dump()})
        assert updated["skeleton"]["from_table"] == "orders"
        assert updated["skeleton_hash"] == build_skeleton(_plan()).skeleton_hash
        assert updated["error"] is None

    def test_missing_plan_is_fail_closed_to_degenerate_branch(self) -> None:
        # Дефект каркаса: план не дошёл — флаг генерации невозможна → degrade
        tracer = _RecordingTracer()
        node = make_build_skeleton_node(tracer=tracer)
        updated = node({"join_plan": None})
        assert updated["skeleton"] is None
        assert updated["route_flags"][BUILDER_FAILED_FLAG] is True
        assert updated["error"]["class"] == "skeleton_defect"
        events = [t for t in tracer.records if t.event == EVENT_SKELETON_UNAVAILABLE]
        assert len(events) == 1

    def test_invalid_plan_dict_is_fail_closed(self) -> None:
        node = make_build_skeleton_node()
        updated = node({"join_plan": {"tables": "not-a-list"}})
        assert updated["route_flags"][BUILDER_FAILED_FLAG] is True


class TestGenerateSqlNode:
    def _service_result(self, outcome: GenerateSqlOutcome, **kwargs: Any) -> Any:
        class _Scripted:
            """Скриптовый генератор с записью входов (ревью S10 I6)."""

            def __init__(self, result: GenerateSqlResult) -> None:
                self._result = result
                self.called = 0
                self.seen: list[dict[str, Any]] = []

            def generate(self, **kwargs: Any) -> GenerateSqlResult:
                self.called += 1
                self.seen.append(dict(kwargs))
                return self._result

        return _Scripted(GenerateSqlResult(outcome=outcome, **kwargs))

    def test_sql_outcome_writes_sql_and_clears_failed_flag(self) -> None:
        tracer = _RecordingTracer()
        service = self._service_result(
            GenerateSqlOutcome.SQL, sql="SELECT count(*)\nFROM orders", llm_calls=1
        )
        node = make_generate_sql_node(service, tracer=tracer)
        updated = node(
            {
                "question": QUESTION,
                "user_id": "u_analyst",
                "skeleton": build_skeleton(_plan()).model_dump(),
                "permissions": PERMISSIONS,
                "budget_used": {"llm_calls": 4},
                "route_flags": {
                    GENERATE_SQL_FAILED_FLAG: True,  # от дефекта скелета
                    GENERATE_SQL_RETRYABLE_FLAG: True,  # stale от ретрай-визита
                },
                "trace_id": "t-2",
            }
        )
        assert updated["sql"].startswith("SELECT count(*)")
        assert updated["route_flags"][GENERATE_SQL_FAILED_FLAG] is False
        # Ревью S14 Minor-2: симметричная очистка — stale RETRYABLE не доживает
        # до следующего решения ребра (корректность не на fuse-инварианте)
        assert updated["route_flags"][GENERATE_SQL_RETRYABLE_FLAG] is False
        assert updated["error"] is None
        assert updated["budget_used"]["llm_calls"] == 5  # 4 + 1 (R6)
        verdict = [t for t in tracer.records if t.event == EVENT_GENERATE_SQL_VERDICT]
        assert len(verdict) == 1
        assert verdict[0].payload["outcome"] == "sql"
        assert verdict[0].payload["escalated"] is False

    def test_fail_outcome_flags_degrade_branch(self) -> None:
        # Ruling R2: отказ генератора → FLAG_GENERATE_SQL_FAILED → degrade
        tracer = _RecordingTracer()
        service = self._service_result(
            GenerateSqlOutcome.FAIL, error="наполнение не удалось", llm_calls=3
        )
        updated = make_generate_sql_node(service, tracer=tracer)(
            {
                "question": QUESTION,
                "user_id": "u_analyst",
                "skeleton": build_skeleton(_plan()).model_dump(),
            }
        )
        assert updated["sql"] is None
        assert updated["route_flags"][GENERATE_SQL_FAILED_FLAG] is True
        assert updated["error"]["class"] == "generate_sql_unavailable"
        assert updated["budget_used"]["llm_calls"] == 3
        events = [t for t in tracer.records if t.event == EVENT_GENERATE_SQL_UNAVAILABLE]
        assert len(events) == 1
        assert "не удалось" in events[0].payload["detail"]

    def test_state_inputs_reach_the_service(self) -> None:
        # I6 (ревью S10): молчаливая потеря inputs отключила бы R5 — узел
        # обязан передать skeleton/permissions/assumptions/question из state
        service = self._service_result(GenerateSqlOutcome.SQL, sql="SELECT 1")
        assumptions = [Assumption(source="classifier", text="период — 2026")]
        make_generate_sql_node(service)(
            {
                "question": QUESTION,
                "user_id": "u_analyst",
                "skeleton": build_skeleton(_plan()).model_dump(),
                "permissions": PERMISSIONS,
                "assumptions": assumptions,
            }
        )
        seen = service.seen[0]
        assert seen["question"] == QUESTION
        assert seen["permissions"] == PERMISSIONS
        assert seen["assumptions"] == assumptions
        assert seen["skeleton"].from_table == "orders"
        assert seen["start_attempt"] == 1  # попытка 1 — вне FSM-ретрая
        assert seen["last_sql"] is None
        assert seen["last_error"] is None  # M2: затерявшийся error не сеется

    def test_fsm_retry_context_is_seeded_from_state(self) -> None:
        # I3 (ревью S10): FSM-ретрай (гейт S14) инкрементирует retry_count —
        # визит продолжения должен стартовать с попытки 2 + контекст ошибки,
        # иначе per-node кап стал бы 3×3 (ревью Important-3)
        service = self._service_result(GenerateSqlOutcome.SQL, sql="SELECT 1")
        make_generate_sql_node(service)(
            {
                "question": QUESTION,
                "user_id": "u_analyst",
                "skeleton": build_skeleton(_plan()).model_dump(),
                "permissions": PERMISSIONS,
                "retry_count": 1,
                "route_flags": {RETRY_TARGET_FLAG: "generate_sql"},
                "sql": "SELECT broken_column FROM orders",
                "error": {
                    "class": "hallucination",
                    "message": "колонка broken_column не существует",
                },
            }
        )
        seen = service.seen[0]
        assert seen["start_attempt"] == 2
        assert seen["last_sql"] == "SELECT broken_column FROM orders"
        assert seen["last_error"] == "колонка broken_column не существует"

    def test_fsm_retry_seeds_raw_detail_not_fixed_message(self) -> None:
        # S33 (двухканальность S9/A7): в контекст ретрая сеется error["detail"]
        # (сырой текст БД — фактические имена, ловушка 2 «та же подсказка при
        # temp 0»); фиксированный message — пользовательский текст, модели
        # больше не отдаётся. На до-фиксовом коде last_error сеется из
        # message → «does not exist» в контекст не попадает (bite)
        service = self._service_result(GenerateSqlOutcome.SQL, sql="SELECT 1")
        make_generate_sql_node(service)(
            {
                "question": QUESTION,
                "user_id": "u_analyst",
                "skeleton": build_skeleton(_plan()).model_dump(),
                "permissions": PERMISSIONS,
                "retry_count": 1,
                "route_flags": {RETRY_TARGET_FLAG: "generate_sql"},
                "sql": "SELECT client_id FROM orders",
                "error": {
                    "class": "semantics",
                    "message": "по этому вопросу данных не нашлось",
                    "detail": "column orders.client_id does not exist",
                },
            }
        )
        seen = service.seen[0]
        assert "does not exist" in seen["last_error"]  # факт — сырой канал
        assert "данных не нашлось" not in seen["last_error"]  # message не сеется

    def test_retry_counter_of_other_node_does_not_seed(self) -> None:
        # Счётчик валиден только для активного ретрая (routing.attempts_used):
        # чужой retry_target — попытка 1, без контекста ретрая
        service = self._service_result(GenerateSqlOutcome.SQL, sql="SELECT 1")
        make_generate_sql_node(service)(
            {
                "question": QUESTION,
                "user_id": "u_analyst",
                "skeleton": build_skeleton(_plan()).model_dump(),
                "retry_count": 2,
                "route_flags": {RETRY_TARGET_FLAG: "plan_joins"},
            }
        )
        assert service.seen[0]["start_attempt"] == 1

    def test_guards_without_service_call(self) -> None:
        service = self._service_result(GenerateSqlOutcome.SQL, sql="SELECT 1")
        node = make_generate_sql_node(service)
        skeleton = build_skeleton(_plan()).model_dump()
        cases = [
            ({"question": QUESTION, "user_id": "", "skeleton": skeleton}, "identity_missing"),
            ({"question": "", "user_id": "u", "skeleton": skeleton}, "generate_empty_question"),
            ({"question": QUESTION, "user_id": "u", "skeleton": None}, "skeleton_missing"),
            (
                {"question": QUESTION, "user_id": "u", "skeleton": {"tables": 1}},
                "skeleton_invalid",
            ),
        ]
        for state, expected_class in cases:
            updated = node(dict(state))
            assert updated["route_flags"][GENERATE_SQL_FAILED_FLAG] is True
            assert updated["error"]["class"] == expected_class
        assert service.called == 0

    # --- BL-016 / ruling R3 ARCH-GATE S14: generation-fail на ретрай-визите ---

    def _fail_result(self, kind: GenerateFailKind) -> Any:
        return self._service_result(
            GenerateSqlOutcome.FAIL,
            error="наполнение не разобрано",
            fail_kind=kind,
            llm_calls=1,
        )

    def test_retry_visit_generation_fail_sets_retryable(self) -> None:
        """Попытка 2 дала битый выход — флаг RETRYABLE → ребро эскалации (R3/BL-016)."""
        service = self._fail_result(GenerateFailKind.GENERATION)
        updated = make_generate_sql_node(service)(
            {
                "question": QUESTION,
                "user_id": "u_analyst",
                "skeleton": build_skeleton(_plan()).model_dump(),
                "permissions": PERMISSIONS,
                "retry_count": 1,
                "route_flags": {RETRY_TARGET_FLAG: "generate_sql"},
                "sql": "SELECT 1",
                "error": {"class": "validate_hallucination", "message": "галлюцинация"},
            }
        )
        assert updated["route_flags"][GENERATE_SQL_FAILED_FLAG] is True
        assert updated["route_flags"][GENERATE_SQL_RETRYABLE_FLAG] is True

    def test_first_visit_generation_fail_stays_degrade(self) -> None:
        """Первый визит: внутренний M1 уже потрачен — RETRYABLE не ставится (R2)."""
        service = self._fail_result(GenerateFailKind.GENERATION)
        updated = make_generate_sql_node(service)(
            {
                "question": QUESTION,
                "user_id": "u_analyst",
                "skeleton": build_skeleton(_plan()).model_dump(),
                "permissions": PERMISSIONS,
            }
        )
        assert updated["route_flags"][GENERATE_SQL_FAILED_FLAG] is True
        assert GENERATE_SQL_RETRYABLE_FLAG not in updated["route_flags"]

    def test_unavailable_fail_never_retryable(self) -> None:
        """Шлюз/retrieval/бюджет — ретрай бессмыслен даже на ретрай-визите (R2)."""
        service = self._fail_result(GenerateFailKind.UNAVAILABLE)
        updated = make_generate_sql_node(service)(
            {
                "question": QUESTION,
                "user_id": "u_analyst",
                "skeleton": build_skeleton(_plan()).model_dump(),
                "permissions": PERMISSIONS,
                "retry_count": 1,
                "route_flags": {RETRY_TARGET_FLAG: "generate_sql"},
            }
        )
        assert GENERATE_SQL_RETRYABLE_FLAG not in updated["route_flags"]

    def test_guard_fail_flags_not_retryable(self) -> None:
        """Дефекты каркаса (identity/skeleton) — не generation-fail: без RETRYABLE."""
        service = self._service_result(GenerateSqlOutcome.SQL, sql="SELECT 1")
        updated = make_generate_sql_node(service)(
            {"question": QUESTION, "user_id": "", "skeleton": build_skeleton(_plan()).model_dump()}
        )
        assert updated["route_flags"][GENERATE_SQL_FAILED_FLAG] is True
        assert GENERATE_SQL_RETRYABLE_FLAG not in updated["route_flags"]


class TestRoutingS10:
    """Новые рёбра отказа (ruling R2 ARCH-GATE): маршрут читает только флаги."""

    def _route(self, name: str, state: dict[str, Any]) -> str:
        from text_to_sql_agent.orchestration.routing import CONDITIONAL_ROUTES

        return CONDITIONAL_ROUTES[name](state)

    def test_plan_joins_no_path_routes_to_degrade_node(self) -> None:
        outcome = self._route("plan_joins", {"route_flags": {"plan_joins_no_path": True}})
        assert outcome == "degrade"  # узел (не терминал) — отказ формулирует S15

    def test_plan_joins_exhausted_still_terminal_fail(self) -> None:
        # Fuse-приоритет: exhausted проверяется раньше no_path
        outcome = self._route(
            "plan_joins",
            {"route_flags": {"plan_joins_exhausted": True, "plan_joins_no_path": True}},
        )
        assert outcome == "terminal_fail"

    def test_generate_sql_failed_routes_to_degrade_node(self) -> None:
        outcome = self._route("generate_sql", {"route_flags": {"generate_sql_failed": True}})
        assert outcome == "degrade"

    def test_generate_sql_retryable_routes_to_retry_gate(self) -> None:
        # R3/BL-016: generation-fail на ретрай-визите при незатраченном fuse —
        # гейт retry_generate_sql (инкремент кодом ребра → визит 3, эскалация)
        outcome = self._route(
            "generate_sql",
            {
                "route_flags": {
                    "generate_sql_failed": True,
                    "generate_sql_retryable": True,
                    "retry_target": "generate_sql",
                },
                "retry_count": 1,
            },
        )
        assert outcome == "retry_generate_sql"

    def test_generate_sql_retryable_with_exhausted_fuse_degrades(self) -> None:
        # Fuse ≤ 3: визит 3 исчерпан — RETRYABLE бесполезен → degrade (R2)
        outcome = self._route(
            "generate_sql",
            {
                "route_flags": {
                    "generate_sql_failed": True,
                    "generate_sql_retryable": True,
                    "retry_target": "generate_sql",
                },
                "retry_count": 2,
            },
        )
        assert outcome == "degrade"

    def test_generate_sql_retryable_foreign_target_counts_fresh(self) -> None:
        # Fuse читает attempts_used целевого узла: чужой retry_target — счётчик
        # с нуля (попытка 1) — fuse не исчерпан, ребро ретрая работает
        outcome = self._route(
            "generate_sql",
            {
                "route_flags": {
                    "generate_sql_retryable": True,
                    "retry_target": "plan_joins",
                },
                "retry_count": 2,
            },
        )
        assert outcome == "retry_generate_sql"

    def test_generate_sql_default_still_validate(self) -> None:
        # Валидация не перепрыгивается (ADR-002 §5)
        outcome = self._route("generate_sql", {"route_flags": {}})
        assert outcome == "validate_sql"

    def test_build_skeleton_defect_routes_to_degrade_node(self) -> None:
        outcome = self._route("build_skeleton", {"route_flags": {"generate_sql_failed": True}})
        assert outcome == "degrade"

    def test_flag_constants_match_routing(self) -> None:
        # I5 (ревью S10): имена флагов дублируются application↔orchestration
        # (application не импортирует orchestration) — parity-гвард, иначе
        # расхождение строк молча ломает рёбра отказа
        from text_to_sql_agent.orchestration import routing

        assert GENERATE_SQL_FAILED_FLAG == routing.FLAG_GENERATE_SQL_FAILED
        assert GENERATE_SQL_RETRYABLE_FLAG == routing.FLAG_GENERATE_SQL_RETRYABLE
        assert PLAN_JOINS_NO_PATH_FLAG == routing.FLAG_PLAN_JOINS_NO_PATH
        assert PLAN_JOINS_CLARIFY_FLAG == routing.FLAG_PLAN_JOINS_CLARIFY
        assert PLAN_JOINS_EXHAUSTED_FLAG == routing.FLAG_PLAN_JOINS_EXHAUSTED
        assert RETRY_TARGET_FLAG == routing.FLAG_RETRY_TARGET

    def test_max_retries_parity_routing_and_policy(self) -> None:
        # I4 (ревью S10): fuse FSM и внутренние ретраи сервисов — одна константа
        from text_to_sql_agent.application.services.m1_policy import MAX_ATTEMPTS
        from text_to_sql_agent.orchestration.routing import MAX_NODE_RETRIES

        assert MAX_NODE_RETRIES == MAX_ATTEMPTS

    def test_flags_not_in_transition_set(self) -> None:
        # Решенческие флаги не гасятся биндером — узел перерешает сам
        from text_to_sql_agent.orchestration.routing import TRANSITION_FLAGS

        assert "generate_sql_failed" not in TRANSITION_FLAGS
        assert "generate_sql_retryable" not in TRANSITION_FLAGS
        assert "plan_joins_no_path" not in TRANSITION_FLAGS

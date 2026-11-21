"""Сервис узла plan_joins — гибридный поиск: retrieval + граф → JoinPlan (feat-009).

Гибрид (hw-plan §4, флагман ДЗ-04): semantic retrieval находит релевантные
таблицы и рёбра (usage_notes — допущения), граф даёт путь джойнов по снапшоту
активных рёбер, модель ранжирует готовых кандидатов (ADR-007 §4: «код строит
k взвешенных путей; qwen ранжирует топ-5»). Результат — JoinPlan ≤ 4 таблиц
по rel_id или честный вердикт: нет пути (+заявка владельцу, ADR-006 §4.4) /
уточнение / исчерпание ретраев.

Инвариант hw-01 §2.1: LLM наполняет, код решает. Маршрут (кандидаты путей),
валидация, сборка плана, исходы узла — код; модель только ранжирует индексы
существующих кандидатов через тул (ADR-008 слой 2: tool-based enforcement) —
выдуманный джойн невозможен по построению, и всё равно отсекается валидатором
(domain/join_plan.py, defense-in-depth; тест-инвариант).

Ретраи M1 (D-016, ADR-007 §4.1): попытка 1 — cheap (матрица plan_joins);
попытка 2 — та же роль + текст ошибки (в контекст — только последний выход и
ошибка, не цепочка попыток); попытка 3 — роль вверх при остатке бюджета с
резервом (ruling Q7 ARCH-GATE: инъекция budget_remaining_roubles(), подключена
к резидентному аккумулятору адаптера; ESCALATION_RESERVE_ROUBLES = 0.6 ₽).

Retrieval-нога — через порт VectorStore (Ловушка-5): dense или гибрид (S26)
подменяются реализацией под портом, сервис не меняется. FSM-подключение,
state.permissions/graph_version в state, interrupt() — V4/S04, S08.
"""

import json
from collections.abc import Callable, Mapping, Sequence
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from text_to_sql_agent.application.ports.edge_proposal_store import (
    EdgeProposalError,
    EdgeProposalStore,
)
from text_to_sql_agent.application.ports.llm_provider import (
    LLMProvider,
    LLMProviderError,
    LLMResponse,
    Role,
)
from text_to_sql_agent.application.ports.schema_graph_store import (
    EdgeSemantics,
    JoinEdge,
    JoinPath,
    SchemaGraphError,
    SchemaGraphStore,
)
from text_to_sql_agent.application.ports.vector_store import (
    RELATIONSHIP_SOURCE_REF_PREFIX,
    DocKind,
    KnowledgeDocument,
    VectorStore,
    VectorStoreError,
)
from text_to_sql_agent.domain.entities import RunContext
from text_to_sql_agent.domain.join_plan import (
    MAX_JOIN_PLAN_TABLES,
    JoinPlan,
    JoinPlanEdge,
    validate_join_plan,
)

# --- Константы (AGENTS.md: магические числа — именованные, с «почему») ---

# top_k retrieval: баланс полноты/шума (решение S24 #1а — top_k 8 поверх шумного корпуса)
_RETRIEVAL_TOP_K = 8
# Кап кандидатов-таблиц: пары ≤ 15, бюджет вызовов графа ограничен (caller-side
# кап до стороннего лимита энумерации путей — BL-011 открыт, P3, не закрывает его)
_MAX_CANDIDATE_TABLES = 6
# k взвешенных путей НА ПАРУ (ADR-003 §4: k путей, multigraph)
_K_PATHS_PER_PAIR = 3
# Топ-5 кандидатов на ранжирование моделью (ADR-007 §4 матрица plan_joins)
_MAX_RANK_CANDIDATES = 5
# Ретраи M1: три попытки на узел (ADR-007 §4.1; per-node кап ≤ 3 — S00 стык 1)
_MAX_PLAN_ATTEMPTS = 3
# Резерв бюджета: эскалация только при остатке ВЫШЕ резерва (ADR-007 §4.3;
# после эскалации прогон сохраняет запас на interpret_result)
_ESCALATION_RESERVE_ROUBLES = 0.6
# Номер попытки с эскалацией роли вверх (D-016: попытка 3 — уровень вверх)
_ESCALATION_ATTEMPT = 3
# Равенство весов кандидатов: NUMERIC реестра → float, сравнение с допуском
_WEIGHT_TIE_EPSILON = 1e-9
# Кап заявок за прогон: очередь владельцу не спамим (частота = COUNT(*) строк)
_MAX_PROPOSALS_PER_RUN = 3
# Точность веса в payload промпта: NUMERIC реестра несёт лишние знаки,
# 4 достаточно для сопоставимости кандидатов (ревью M6 — именованная константа)
_PAYLOAD_WEIGHT_PRECISION = 4

# Роль попытки 1 — матрица ADR-007 §4: план_joins = дешёвая модель (ранжирование
# готовых кандидатов — классификационная задача, ошибка дёшево исправима ретраем)
_RANKING_ROLE_START: Role = "cheap"
# D-016: попытка 3 — роль вверх (cheap→mid→strong); вершина — сама на себя
_ROLE_UP: dict[Role, Role] = {"cheap": "mid", "mid": "strong", "strong": "strong"}

# ADR-008 слой 2: structured output — через тул (tool-based enforcement), не
# свободный JSON-текст (отвергнутый вариант B ADR-008); pydantic ниже —
# валидация СУЩНОСТИ выхода, формат гарантирует механизм тула.
_RANK_TOOL_NAME = "rank_paths"
_RANK_TOOL_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": _RANK_TOOL_NAME,
        "description": (
            "Ранжирование кандидатов путей джойна по релевантности вопросу. "
            "Пути построены кодом — джойны выдумывать нельзя, ранжируйте только "
            "существующие индексы."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ranking": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "minItems": 1,
                    "description": "Индексы кандидатов в порядке убывания релевантности",
                },
                "ambiguous": {
                    "type": "boolean",
                    "description": "true, если кандидаты равнозначны и нужен уточняющий вопрос",
                },
                "clarifying_question": {
                    "type": "string",
                    "description": "Один уточняющий вопрос пользователю (при ambiguous=true)",
                },
            },
            "required": ["ranking"],
        },
    },
}

_RANKING_SYSTEM_PROMPT = (
    "Ты — планировщик SQL-джойнов в аналитическом агенте. Тебе дают вопрос пользователя "
    "и кандидатов путей: каждый путь — цепочка активных рёбер реестра связей с бизнес-"
    "семантикой (meaning — что связь значит, usage_notes — ловушки, intent_tags — intent "
    "ребра). Вызови тул rank_paths ровно один раз: ranking — индексы кандидатов в порядке "
    "убывания релевантности вопросу (учитывай meaning/intent_tags/usage_notes и то, какие "
    "сущности упомянуты в вопросе). Если кандидаты равнозначны для вопроса — поставь "
    "ambiguous=true и предложи один уточняющий вопрос. Не выдумывай пути и джойны."
)

# Честный отказ/уточнение — шаблоны кода, не выдумка модели (Task 3: исчерпание →
# fail с уточняющим вопросом, НЕ выдуманный план)
_FAIL_CLARIFY_QUESTION = (
    "Не удалось построить план соединения для этого вопроса — переформулируйте его: "
    "какие сущности (таблицы) и связь между ними вы имеете в виду?"
)
_CLARIFY_TIE_QUESTION = (
    "Для вопроса есть несколько равнозначных путей соединения ({chains}) — уточните, "
    "какая связь между сущностями вам нужна."
)


class PlanOutcome(StrEnum):
    """Исход узла plan_joins (Task 2: план или вердикт). FSM-маршрутизация — V4."""

    PLAN = "plan"
    NO_PATH = "no_path"  # чистое «нет активного пути» → degrade + заявка (Task 4)
    CLARIFY = "clarify"  # уточнение контентом узла (interrupt — V4/S08)
    FAIL = "fail"  # исчерпание ретраев / сбой инфраструктуры — честный отказ


class PlanJoinsResult(BaseModel):
    """Результат узла plan_joins: план или вердикт + пин снапшота (Ловушка-3).

    graph_version — ревизия снапшота на момент графовой ноги (пин в plan и
    результат — ADR-006 §4.3); plan.graph_version — тот же пин (сверка версий
    find_join_paths и JoinPlan). candidate_tables/hit_edge_ids — диагностика
    retrieval-ноги (layer-3 прогон, трейсинг — V4).
    """

    model_config = ConfigDict(frozen=True)

    outcome: PlanOutcome
    plan: JoinPlan | None = None
    graph_version: int = Field(ge=0, default=0)
    clarifying_question: str | None = None
    error: str | None = None
    candidate_tables: list[str] = Field(default_factory=list)
    hit_edge_ids: list[str] = Field(default_factory=list)


class PathRanking(BaseModel):
    """Structured output ранжирования (ADR-008): валидация СУЩНОСТИ выхода тула.

    ranking — индексы кандидатов в порядке предпочтения (первый = победитель);
    ambiguous/clarifying_question — сигнал «равные кандидаты → уточнение»
    (ADR-007: неоднозначность ранжирования → clarify, не эскалация).
    """

    model_config = ConfigDict(frozen=True)

    ranking: list[int] = Field(min_length=1)
    ambiguous: bool = False
    clarifying_question: str | None = None


class PlanJoinsService:
    """Узел plan_joins: retrieval-нога + графовая + ранжирование + валидация."""

    def __init__(
        self,
        *,
        llm: LLMProvider,
        vector_store: VectorStore,
        graph_store: SchemaGraphStore,
        edge_proposals: EdgeProposalStore,
        budget_remaining_roubles: Callable[[], float] | None = None,
    ) -> None:
        # budget_remaining_roubles — шов бюджета (ruling Q7 ARCH-GATE S09):
        # composition подключает к резидентному аккумулятору адаптера
        # (cap − budget_used_rub); None = бюджетный контур ещё не подключён —
        # документированный fallback до FSM (V4/S15 BudgetGuard).
        self._llm = llm
        self._vector_store = vector_store
        self._graph_store = graph_store
        self._edge_proposals = edge_proposals
        self._budget_remaining_roubles = budget_remaining_roubles

    def plan(
        self,
        question: str,
        *,
        ctx: RunContext,
        permissions: Mapping[str, Sequence[str]],
    ) -> PlanJoinsResult:
        """План джойнов для вопроса под правами пользователя (S00 контракт 3).

        permissions — {table: [columns|'*']} (строка-CSV «a,b» нормализуется на
        границе — agent_permissions.columns хранит текст), единственный источник
        мира агента (resolve_access передаёт из state — V4); deny-by-default.
        """
        permissions = _normalize_permissions(permissions)
        allowed = set(permissions)

        # --- Нога 1: retrieval (VectorStore-порт; сбой → fail БЕЗ заявки: пары
        # таблиц не известны — заявлять нечего, S00 правило 8) ---
        try:
            [query_embedding] = self._llm.embed([question])
            # Сигнатура гибрида S26: текст вопроса — FTS-ноге, вектор — dense-ноге
            documents = self._vector_store.search(
                question, query_embedding, ctx=ctx, top_k=_RETRIEVAL_TOP_K
            )
        except (LLMProviderError, VectorStoreError) as exc:
            return self._fail(f"retrieval недоступен: {exc}")

        candidate_tables = self._candidate_tables(documents, allowed)
        hit_edge_ids = self._hit_edge_ids(documents)
        if len(candidate_tables) < 2:
            # Джойн строится минимум из двух таблиц; retrieval не дал контекста —
            # спрашиваем пользователя (уточнение контентом узла, не interrupt)
            return PlanJoinsResult(
                outcome=PlanOutcome.CLARIFY,
                clarifying_question=(
                    "Уточните, о каких сущностях речь: в базе знаний не нашлось "
                    f"релевантных таблиц для вопроса «{question}»"
                ),
                candidate_tables=candidate_tables,
                hit_edge_ids=hit_edge_ids,
            )

        # --- Нога 2: граф (кандидаты путей строит код; сбой снапшота → fail
        # БЕЗ заявки: это инфраструктурный сбой, не факт отсутствия пути,
        # Ловушка-3). Пин снапшота: версия ДО и ПОСЛЕ ноги И ПОСЛЕ чтения
        # семантики — конкурентный sync-graph между запросами пар перемешал бы
        # пути/семантику vN/vN+1 (ревью I6 + re-review m4; оба чтения версии
        # под тем же try — re-review C1: graph_version() штатно бросает
        # SchemaGraphError при недоступном Neo4j)
        try:
            version_before = self._graph_store.graph_version()
            paths_by_rel_ids, disconnected_pairs = self._graph_leg(
                candidate_tables, ctx=ctx, allowed=frozenset(allowed)
            )
            graph_version = self._graph_store.graph_version()
        except SchemaGraphError as exc:
            return self._fail(str(exc))
        if graph_version != version_before:
            return self._fail(
                f"снапшот графа изменился во время планирования "
                f"(graph_version {version_before} → {graph_version}) — повторите прогон"
            )
        if graph_version == 0:
            # 0 = снапшот ещё не загружен (порт SchemaGraphStore) — это НЕ «чистое
            # нет пути»: заявка по несуществующему снапшоту — ложный сигнал
            # владельцу (ревью I1, Q6/Ловушка-3)
            return self._fail(
                "снапшот графа не загружен (graph_version=0) — выполните make sync-graph"
            )

        if not paths_by_rel_ids:
            return self._no_path_result(
                question,
                candidate_tables,
                disconnected_pairs,
                ctx=ctx,
                graph_version=graph_version,
            )

        # --- Семантика рёбер: fail-closed на дрейф граф↔реестр (S07) ---
        unique_rel_ids: list[str] = []
        for path in paths_by_rel_ids.values():
            for edge in path.edges:
                if edge.rel_id not in unique_rel_ids:
                    unique_rel_ids.append(edge.rel_id)
        try:
            semantics = self._graph_store.path_semantics(unique_rel_ids, ctx=ctx)
            # Третий контроль пина: семантика прочитана из PG позже путей —
            # sync в этом окне склеил бы топологию vN с семантикой vN+1
            # (re-review m4; остаточное окно после этой проверки — только
            # сборка/валидация в памяти, без IO)
            if self._graph_store.graph_version() != graph_version:
                raise SchemaGraphError(
                    "снапшот графа изменился при чтении семантики "
                    f"(graph_version {graph_version}) — повторите прогон"
                )
        except SchemaGraphError as exc:
            return self._fail(str(exc))
        sem_by_rel = {s.rel_id: s for s in semantics}

        # --- Кандидаты на ранжирование: код pre-rank, кап топ-5 ---
        # Фильтры рёбер — КОДОМ (Task 2 п.4): (1) intent — рёбра с retrieval-
        # подтверждением (rel:{edge_id}-доки в хитах) идут вперёд; (2) temporal —
        # пути БЕЗ временных рёбер вперёд: date-чувствительность = допущение,
        # которое вопрос не просил (temporal-ребро всплывает assumptions);
        # (3) МИНИМАЛЬНЫЙ план — лишние таблицы раздувают скелет S10 и бюджет;
        # вес реестра ранжирует параллельные рёбра ОДНОЙ пары (ADR-003 §4), а
        # не выбор длины пути между разными парами; затем вес и rel_id-тайбрейк.
        candidates = sorted(
            (p for p in paths_by_rel_ids.values() if len(p.tables) <= MAX_JOIN_PLAN_TABLES),
            key=lambda p: (
                not any(e.rel_id in hit_edge_ids for e in p.edges),
                any(sem_by_rel[e.rel_id].temporal for e in p.edges),
                len(p.edges),
                -p.total_weight,
                tuple(e.rel_id for e in p.edges),
            ),
        )[:_MAX_RANK_CANDIDATES]
        if not candidates:
            # Все найденные пути длиннее лимита JoinPlan (ревью I3: порт не
            # ограничивает хопы — константа хопа живёт в реализации Neo4j,
            # замена хранилища по D-015 может вернуть больше)
            return self._fail(
                f"связные пути длиннее лимита {MAX_JOIN_PLAN_TABLES} таблиц — "
                "такой вопрос подлежит декомпозиции, а не джойн-плану"
            )

        winner = candidates[0]
        if len(candidates) > 1:
            verdict, ranking = self._rank_by_llm(question, candidates, sem_by_rel)
            if verdict is not None:  # FAIL — честный исход без плана
                return verdict
            tie = (
                abs(candidates[0].total_weight - candidates[1].total_weight) <= _WEIGHT_TIE_EPSILON
            )
            if ranking.ambiguous or tie:
                chains = " / ".join(self._chain(candidates[i]) for i in ranking.ranking[:2])
                return PlanJoinsResult(
                    outcome=PlanOutcome.CLARIFY,
                    graph_version=graph_version,
                    clarifying_question=(
                        ranking.clarifying_question or _CLARIFY_TIE_QUESTION.format(chains=chains)
                    ),
                    candidate_tables=candidate_tables,
                    hit_edge_ids=hit_edge_ids,
                )
            winner = candidates[ranking.ranking[0]]
        # Один кандидат: ранжировать нечего — вызов шлюза не тратим (экономия
        # бюджета; модель подключается там, где есть выбор)

        try:
            plan = self._build_plan(winner, sem_by_rel, graph_version)
        except ValueError as exc:
            # Дрейф граф↔реестр (направление ≠ left/right) или брак сборки —
            # типизированный честный FAIL, не сырой вылет (ревью I3)
            return self._fail(f"сборка плана не удалась: {exc}")
        # Defense-in-depth: план собран кодом из активного снапшота, но инвариант
        # «план проверяет код» — валидатор отсекает любой брак до ретраев/SQL
        issues = validate_join_plan(
            plan, permissions=permissions, active_rel_ids=frozenset(unique_rel_ids)
        )
        if issues:
            return self._fail("план не прошёл валидацию: " + "; ".join(issues))
        return PlanJoinsResult(
            outcome=PlanOutcome.PLAN,
            plan=plan,
            graph_version=graph_version,
            candidate_tables=candidate_tables,
            hit_edge_ids=hit_edge_ids,
        )

    # --- Ноги узла ---

    @staticmethod
    def _candidate_tables(documents: Sequence[KnowledgeDocument], allowed: set[str]) -> list[str]:
        """Кандидаты-таблицы из retrieval-хитов ∩ права (deny-by-default).

        Порядок = релевантность поиска (score убывает); якорь + все
        referenced_tables документа; кап константой (BL-011 caller-side).
        Шумовой корпус (doc_kind='noise', S26/D-026) — дистрактор замера,
        не знание: таблицы шумового документа не становятся кандидатами
        (measurement-инфраструктура не должна подмешиваться в план джойнов).
        """
        tables: list[str] = []
        seen: set[str] = set()
        for doc in documents:
            if doc.doc_kind is DocKind.NOISE:
                continue
            for table in (doc.table_name, *doc.referenced_tables):
                if table and table in allowed and table not in seen:
                    seen.add(table)
                    tables.append(table)
        # Слайс ПОСЛЕ сбора: break по документу мог утащить якоря из последнего
        # документа и превысить кап (ревью M4)
        return tables[:_MAX_CANDIDATE_TABLES]

    @staticmethod
    def _hit_edge_ids(documents: Sequence[KnowledgeDocument]) -> list[str]:
        """Рёбра, чьи RELATIONSHIP-документы retrieval нашёл (source_ref = rel:edge_id).

        Подтверждение релевантности ребра вопросу — код-фильтр intent (Task 2):
        пути с «подтверждёнными» рёбрами идут вперёд при pre-rank. Сортировка —
        диагностика узла детерминированна (ревью M5).
        """
        prefix = RELATIONSHIP_SOURCE_REF_PREFIX
        return sorted(
            doc.source_ref[len(prefix) :]
            for doc in documents
            if doc.doc_kind is DocKind.RELATIONSHIP and doc.source_ref.startswith(prefix)
        )

    def _graph_leg(
        self,
        candidate_tables: list[str],
        *,
        ctx: RunContext,
        allowed: frozenset[str],
    ) -> tuple[dict[tuple[str, ...], JoinPath], list[tuple[str, str]]]:
        """Все пары кандидатов → find_join_paths(k); дедуп по rel_id-последовательности.

        Возвращает (пути по rel_id-ключу, пары без пути). Демо-путь
        customers←orders→campaigns длиннее пары rel-дока — пары только из
        RELATIONSHIP-доков недостаточно (ruling Q3).
        """
        paths: dict[tuple[str, ...], JoinPath] = {}
        disconnected: list[tuple[str, str]] = []
        for i, left in enumerate(candidate_tables):
            for right in candidate_tables[i + 1 :]:
                found = self._graph_store.find_join_paths(
                    left, right, ctx=ctx, allowed_tables=allowed, k=_K_PATHS_PER_PAIR
                )
                for path in found:
                    paths.setdefault(tuple(edge.rel_id for edge in path.edges), path)
                if not found:
                    disconnected.append((left, right))
        return paths, disconnected

    def _rank_by_llm(
        self,
        question: str,
        candidates: list[JoinPath],
        sem_by_rel: dict[str, EdgeSemantics],
    ) -> tuple[PlanJoinsResult | None, PathRanking | None]:
        """Ранжирование моделью с ретраями M1 (Task 3).

        Возвращает (FAIL-вердикт, None) или (None, ranking) — исходы «уточнение»
        (tie/ambiguous) решает вызывающий по ranking. Попытка 1 — cheap;
        попытка 2 — cheap + текст ошибки; попытка 3 — роль вверх при остатке
        бюджета с резервом (ruling Q7), иначе честный отказ.
        """
        payload = self._ranking_payload(candidates, sem_by_rel)
        error_text: str | None = None
        last_output: str | None = None
        role: Role = _RANKING_ROLE_START
        for attempt in range(1, _MAX_PLAN_ATTEMPTS + 1):
            if attempt == _ESCALATION_ATTEMPT:
                if (
                    self._budget_remaining_roubles is not None
                    and self._budget_remaining_roubles() <= _ESCALATION_RESERVE_ROUBLES
                ):
                    return self._fail(
                        f"эскалация попытки {_ESCALATION_ATTEMPT} отклонена: остаток "
                        f"бюджета не покрывает резерв {_ESCALATION_RESERVE_ROUBLES} ₽ "
                        "(ADR-007 §4.3) — ретраи исчерпаны",
                    ), None
                role = _ROLE_UP[role]
            messages = self._ranking_messages(question, payload, error_text, last_output)
            try:
                response = self._llm.complete(role, messages, tools=[_RANK_TOOL_SPEC])
            except LLMProviderError as exc:
                # Политика провайдера исчерпана (backoff/breaker/бюджет) — ретрай
                # M1 поверх неё бессмыслен: честный отказ (S00 правило 8)
                return self._fail(f"шлюз недоступен: {exc}"), None
            try:
                ranking = _parse_ranking(response, len(candidates))
            except ValueError as exc:
                error_text = str(exc)
                last_output = _last_output_text(response)
                continue
            return None, ranking
        return self._fail(
            f"ранжирование не удалось за {_MAX_PLAN_ATTEMPTS} попытки (M1): "
            f"последняя ошибка: {error_text}"
        ), None

    @staticmethod
    def _ranking_payload(
        candidates: list[JoinPath], sem_by_rel: dict[str, EdgeSemantics]
    ) -> list[dict[str, Any]]:
        """Кандидаты для промпта: таблицы/вес/рёбра с семантикой (KISS-JSON)."""
        payload: list[dict[str, Any]] = []
        for index, path in enumerate(candidates):
            edges: list[dict[str, Any]] = []
            for edge in path.edges:
                sem = sem_by_rel[edge.rel_id]
                edges.append(
                    {
                        "rel_id": edge.rel_id,
                        "from_table": edge.from_table,
                        "to_table": edge.to_table,
                        "join_type": sem.join_type,
                        "join_operator": sem.join_operator,
                        "meaning": sem.meaning,
                        "usage_notes": sem.usage_notes,
                        "intent_tags": sem.intent_tags,
                        "cardinality": sem.cardinality,
                        "temporal": sem.temporal,
                    }
                )
            payload.append(
                {
                    "index": index,
                    "tables": list(path.tables),
                    "total_weight": round(path.total_weight, _PAYLOAD_WEIGHT_PRECISION),
                    "edges": edges,
                }
            )
        return payload

    @staticmethod
    def _ranking_messages(
        question: str,
        payload: list[dict[str, Any]],
        error_text: str | None,
        last_output: str | None,
    ) -> list[dict[str, str]]:
        """M1-контекст ретрая: последний выход + текст ошибки, не цепочка попыток (D-016)."""
        messages = [
            {"role": "system", "content": _RANKING_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Вопрос: {question}\n\n"
                    f"Кандидаты путей (JSON):\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
                ),
            },
        ]
        if error_text is not None:
            messages.append({"role": "assistant", "content": last_output or ""})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"Ошибка предыдущей попытки: {error_text} Повтори вызов тула "
                        f"{_RANK_TOOL_NAME} с корректным ранжированием."
                    ),
                }
            )
        return messages

    def _no_path_result(
        self,
        question: str,
        candidate_tables: list[str],
        disconnected_pairs: list[tuple[str, str]],
        *,
        ctx: RunContext,
        graph_version: int,
    ) -> PlanJoinsResult:
        """Чистое «нет активного пути» → degrade + заявка владельцу (Task 4, BL-003 п.2).

        Заявка — только от ФАКТА отсутствия пути по живому снапшоту (не от
        исключения синка — Ловушка-3); пар без пути может быть много — кап
        константой, очередь владельцу не спамим (ruling Q1).
        """
        notes: list[str] = []
        recorded: list[str] = []
        for left, right in disconnected_pairs[:_MAX_PROPOSALS_PER_RUN]:
            try:
                self._edge_proposals.record_missing_edge(
                    left_table=left,
                    right_table=right,
                    question_sample=question,
                    ctx=ctx,
                )
                recorded.append(f"{left}↔{right}")
            except (EdgeProposalError, ValueError) as exc:
                # ValueError адаптера — нарушение контракта вызова (пустой
                # question_sample): заявка не записана, вердикт честный (ревью M7)
                notes.append(str(exc))
        detail = (
            f"активных рёбер нет для пар: {', '.join(f'{a}↔{b}' for a, b in disconnected_pairs)}"
        )
        if recorded:
            detail += f"; заявки записаны ({len(recorded)}): {', '.join(recorded)}"
        if notes:
            # Заявка не записана — вердикт остаётся честным: сбой записи виден
            detail += "; " + "; ".join(notes)
        if recorded:
            question_text = (
                "Путь между этими сущностями пока не заведён владельцем данных — "
                "заявка отправлена; попробуйте переформулировать вопрос"
            )
        else:
            # Ложь «заявка отправлена» при упавших записях — нарушение честных
            # исходов (ревью I2)
            question_text = (
                "Путь между этими сущностями не заведён, и записать заявку владельцу "
                "не удалось — попробуйте переформулировать вопрос"
            )
        return PlanJoinsResult(
            outcome=PlanOutcome.NO_PATH,
            graph_version=graph_version,
            error=detail,
            clarifying_question=question_text,
            candidate_tables=candidate_tables,
        )

    @staticmethod
    def _build_plan(
        winner: JoinPath, sem_by_rel: dict[str, EdgeSemantics], graph_version: int
    ) -> JoinPlan:
        """Сборка JoinPlan кодом из победившего пути (модель план не строит).

        Ключи следуют своей таблице: обход против left/right реестра — разворот
        (ruling Q2, юнит-тест маппинга). assumptions — usage_notes рёбер +
        temporal-замечания; mandatory_predicates — skeleton-якоря S10.
        """
        edges: list[JoinPlanEdge] = []
        assumptions: list[str] = []
        mandatory: dict[str, dict[str, Any]] = {}
        for edge in winner.edges:
            sem = sem_by_rel[edge.rel_id]
            from_columns, to_columns = _columns_for_direction(edge, sem)
            edges.append(
                JoinPlanEdge(
                    rel_id=edge.rel_id,
                    from_table=edge.from_table,
                    to_table=edge.to_table,
                    from_columns=from_columns,
                    to_columns=to_columns,
                    join_operator=sem.join_operator,
                    join_type=sem.join_type,
                )
            )
            if sem.usage_notes:
                assumptions.append(sem.usage_notes)
            if sem.temporal:
                assumptions.append(
                    f"ребро {edge.rel_id} — временная связь {sem.temporal}: уточните период"
                )
            if sem.mandatory_predicates:
                mandatory[edge.rel_id] = sem.mandatory_predicates
        return JoinPlan(
            tables=list(winner.tables),
            edges=edges,
            graph_version=graph_version,
            assumptions=assumptions,
            mandatory_predicates=mandatory,
        )

    # --- Вспомогательное ---

    @staticmethod
    def _fail(error: str) -> PlanJoinsResult:
        """Честный отказ: fail с уточняющим вопросом, НЕ выдуманный план (Task 3)."""
        return PlanJoinsResult(
            outcome=PlanOutcome.FAIL,
            error=error,
            clarifying_question=_FAIL_CLARIFY_QUESTION,
        )

    @staticmethod
    def _chain(path: JoinPath) -> str:
        """Человекочитаемая цепочка пути для уточняющего вопроса: a←b→c."""
        arrows: list[str] = [path.edges[0].from_table]
        for edge in path.edges:
            arrows.append(f"↔{edge.to_table}")
        return "".join(arrows)


def _normalize_permissions(
    permissions: Mapping[str, Sequence[str]],
) -> dict[str, list[str]]:
    """Нормализация карты прав на границе узла (S00 контракт 3).

    agent_permissions.columns — CSV-текст («id,name», миграция 0001): строка
    разворачивается в список колонок ОДИН раз на границе (ревью I4); валидатор
    домена строку отвергает — fail-closed против подстрочного прохода ключа.
    """
    normalized: dict[str, list[str]] = {}
    for table, columns in permissions.items():
        if isinstance(columns, str):
            normalized[table] = [c.strip() for c in columns.split(",") if c.strip()]
        else:
            normalized[table] = list(columns)
    return normalized


def _columns_for_direction(
    edge: JoinEdge, sem: EdgeSemantics
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Ключи джойна по направлению обхода: from_columns принадлежат from_table.

    Направление пути может быть развёрнуто против left/right реестра (джойн
    физически симметричен); расхождение таблиц пути и семантики — дрейф,
    fail-closed ValueError (семантика только что прочитана из реестра).
    """
    if edge.from_table == sem.left_table and edge.to_table == sem.right_table:
        return sem.left_columns, sem.right_columns
    if edge.from_table == sem.right_table and edge.to_table == sem.left_table:
        return sem.right_columns, sem.left_columns
    raise ValueError(
        f"ребро {edge.rel_id}: направление пути {edge.from_table}→{edge.to_table} "
        f"не совпадает с реестром {sem.left_table}/{sem.right_table}"
    )


def _parse_ranking(response: LLMResponse, candidate_count: int) -> PathRanking:
    """Разбор выхода тула rank_paths → PathRanking; любой брак — ValueError (текст для M1)."""
    calls = [c for c in response.tool_calls if c.name == _RANK_TOOL_NAME]
    if not calls:
        raise ValueError(f"ответ не содержит вызова тула {_RANK_TOOL_NAME}")
    if len(calls) > 1:
        raise ValueError(f"ожидался один вызов тула {_RANK_TOOL_NAME}, получено {len(calls)}")
    try:
        arguments = json.loads(calls[0].arguments)
    except json.JSONDecodeError as exc:
        raise ValueError(f"аргументы тула — не JSON: {exc}") from exc
    try:
        ranking = PathRanking.model_validate(arguments)
    except ValidationError as exc:
        raise ValueError(f"выход тула не соответствует схеме ранжирования: {exc}") from exc
    indices = ranking.ranking
    if any(not isinstance(i, int) or not 0 <= i < candidate_count for i in indices):
        raise ValueError(
            f"ranking ссылается на несуществующие индексы {indices} "
            f"(кандидатов: {candidate_count}) — выдуманных путей не существует"
        )
    if len(set(indices)) != len(indices):
        raise ValueError(f"дубликаты индексов в ranking: {indices}")
    return ranking


def _last_output_text(response: LLMResponse) -> str:
    """Последний выход модели для M1-контекста ретрая (D-016: без цепочки попыток)."""
    if response.tool_calls:
        return response.tool_calls[0].arguments
    return response.text or ""

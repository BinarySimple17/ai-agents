"""Интеграционные тесты retry-цикла (feat-014, S14 Task 4): живой прогон цикла
самокоррекции ADR-002 §4 через собранный Container, не напрямую сервисы.

Сценарии (LLM-ноги — скрипты, S00 правило 7; канал исполнения — живой PG под
agent_ro; права/каталог — живой PgPermissionStore, сиды 0003, u_analyst):
1) SQL проходит валидатор (S11), но падает при исполнении (division by zero)
   → diagnose → ретрай попыткой 2 (последний SQL + разбор) → успех → done;
2) все попытки падают → fuse ≤ 3 → валидный degrade (не бесконечный цикл);
3) 0 строк на живой БД → probe COUNT тем же каналом agent_ro → данные в
   таблицах есть, джойн пуст → diagnose → ретрай → строка получена → done.
Скипаются без инфры с понятной причиной (conftest).
"""

import os
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import MemorySaver
from pydantic import SecretStr

from text_to_sql_agent.application.services.generate_sql_service import (
    GenerateSqlOutcome,
    GenerateSqlResult,
)
from text_to_sql_agent.application.services.stub_nodes import initial_state
from text_to_sql_agent.composition.container import build_container
from text_to_sql_agent.config.settings import Settings

_DEMO_TOKENS = "u_analyst:dev-analyst-token,u_logist:dev-logist-token"

# Скелет плана: orders ↔ order_items (активное ребро сидов); валидатор S11
# сверяет джойны SQL с рёбрами плана — оба SQL ниже соответствуют скелету
_JOIN = "FROM orders INNER JOIN order_items ON orders.order_id = order_items.order_id"
# Проходит валидатор (таблицы/колонки в правах), падает при ИСПОЛНЕНИИ
_BAD_SQL = f"SELECT 1 / 0 AS broken {_JOIN}"
# Валидный и живой: количество заказов (сиды — сотни строк)
_GOOD_SQL = f"SELECT count(*) AS orders_total {_JOIN}"
# Валидный, но пустой: order_id = -1 не существует — 0 строк → probe COUNT
_EMPTY_SQL = f"SELECT orders.order_id {_JOIN} WHERE orders.order_id = -1"


def _dsn(env_var: str) -> str:
    dsn = os.getenv(env_var)
    if not dsn:
        pytest.skip(f"{env_var} не задан — скопируйте .env.example в .env")
    return dsn


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        llm_api_key=SecretStr("sk-test"),
        model_cheap="cheap",
        model_mid="mid",
        model_strong="strong",
        budget_per_request_rub=2.0,
        model_cheap_price_in=0,
        model_cheap_price_out=0,
        model_mid_price_in=0,
        model_mid_price_out=0,
        model_strong_price_in=0,
        model_strong_price_out=0,
        embedding_model="embed-1",
        postgres_dsn=SecretStr(_dsn("POSTGRES_DSN")),
        agent_ro_dsn=SecretStr(_dsn("AGENT_RO_DSN")),
        agent_trace_dsn=SecretStr(_dsn("AGENT_TRACE_DSN")),
        valkey_url="redis://127.0.0.1:6379/0",
        neo4j_uri="bolt://127.0.0.1:7687",
        neo4j_user="neo4j",
        neo4j_password=SecretStr("neo4j-dev-password"),
        agent_ro_password=SecretStr("agent_ro"),
        agent_planner_password=SecretStr(os.getenv("AGENT_PLANNER_PASSWORD", "agent_planner")),
        agent_trace_password=SecretStr("agent_trace"),
        registry_writer_password=SecretStr("registry_writer"),
        trace_jsonl_path=tmp_path / "trace-retry.jsonl",
        agent_dev_tokens=_DEMO_TOKENS,
    )


class _ScriptedClarify:
    """Цикл тестирует ретраи, не clarify (S08): вердикт — мок (S00-7)."""

    def clarify(self, question: str, *, ctx: object, permissions: object) -> object:
        from text_to_sql_agent.application.services.clarify_service import (
            ClarifyOutcome,
            ClarifyResult,
        )

        return ClarifyResult(outcome=ClarifyOutcome.UNAMBIGUOUS)


class _ScriptedPlanJoins:
    """Планер — мок (S00-7): PLAN по активному ребру сидов orders ↔ order_items."""

    def plan(self, question: str, *, ctx: object, permissions: object) -> object:
        from text_to_sql_agent.application.services.plan_joins_service import (
            PlanJoinsResult,
            PlanOutcome,
        )
        from text_to_sql_agent.domain.join_plan import JoinPlan, JoinPlanEdge

        return PlanJoinsResult(
            outcome=PlanOutcome.PLAN,
            plan=JoinPlan(
                tables=["orders", "order_items"],
                edges=[
                    JoinPlanEdge(
                        rel_id="e_orders_items",
                        from_table="orders",
                        to_table="order_items",
                        from_columns=("order_id",),
                        to_columns=("order_id",),
                        join_type="inner",
                    )
                ],
                graph_version=1,
            ),
        )


class _QueueGenerateSql:
    """Генератор-очередь: исход на каждый визит узла + запись retry-контекста."""

    def __init__(self, results: list[GenerateSqlResult]) -> None:
        self._results = list(results)
        self.seen_attempts: list[int] = []
        self.seen_last_sql: list[Any] = []
        self.seen_last_error: list[Any] = []

    def generate(self, **kwargs: Any) -> GenerateSqlResult:
        assert self._results, "генератор позван больше числа визитов сценария"
        self.seen_attempts.append(kwargs.get("start_attempt"))
        self.seen_last_sql.append(kwargs.get("last_sql"))
        self.seen_last_error.append(kwargs.get("last_error"))
        return self._results.pop(0)


class _ScriptedDiagnose:
    """Диагност — мок (S00-7): класс retryable + подсказка (ретрай оправдан)."""

    def diagnose(self, **kwargs: Any) -> object:
        from text_to_sql_agent.application.services.diagnose_error_service import (
            DiagnoseClass,
            DiagnoseResult,
            DiagnoseVerdict,
        )

        return DiagnoseResult(
            verdict=DiagnoseVerdict(
                error_class=DiagnoseClass.SEMANTICS,
                regeneration_hint="исправь выражение и попробуй снова",
            ),
            llm_calls=0,  # скрипт не тратит кап прогона
        )


def _sql(text: str) -> GenerateSqlResult:
    return GenerateSqlResult(outcome=GenerateSqlOutcome.SQL, sql=text, llm_calls=1)


def _container(tmp_path: Path, generate: _QueueGenerateSql) -> Any:
    return build_container(
        _settings(tmp_path),
        checkpointer=MemorySaver(),
        clarify_service=_ScriptedClarify(),  # type: ignore[arg-type]
        plan_joins_service=_ScriptedPlanJoins(),  # type: ignore[arg-type]
        generate_sql_service=generate,  # type: ignore[arg-type]
        diagnose_service=_ScriptedDiagnose(),  # type: ignore[arg-type]
    )


class TestRetryCycleLive:
    """Task 4: цикл generate → validate → execute → diagnose → generate на живой БД."""

    def test_execute_error_retry_succeeds_on_attempt2(self, tmp_path, postgres_available) -> None:
        """Сломанный SQL проходит валидатор, падает при исполнении — ретрай чинит."""
        generate = _QueueGenerateSql([_sql(_BAD_SQL), _sql(_GOOD_SQL)])
        container = _container(tmp_path, generate)
        result = container.fsm_runner.run(
            initial_state("Сколько заказов?", user_id="u_analyst"), thread_id="t-retry-live-1"
        )
        assert result["outcome"] == "done"
        assert generate.seen_attempts == [1, 2]  # ретраится генератор, не диагностика
        # В retry-контексте — последний SQL + текст ошибки исполнения + подсказка
        assert generate.seen_last_sql[1] is not None
        assert "1 / 0" in generate.seen_last_sql[1]
        assert generate.seen_last_error[1] is not None
        assert "исправь выражение" in generate.seen_last_error[1]
        # Живой канал вернул сиды
        assert result["row_count"] == 1
        assert result["rows"][0]["orders_total"] > 0

    def test_exhaustion_is_valid_degrade_not_traceback(self, tmp_path, postgres_available) -> None:
        """Все попытки ломаются при исполнении — fuse ≤ 3 → валидный degrade."""
        generate = _QueueGenerateSql([_sql(_BAD_SQL)] * 5)
        container = _container(tmp_path, generate)
        result = container.fsm_runner.run(
            initial_state("Сколько заказов?", user_id="u_analyst"), thread_id="t-retry-live-2"
        )
        assert result["outcome"] == "degrade"
        assert generate.seen_attempts == [1, 2, 3]  # per-node fuse, не бесконечный цикл
        assert result["error"] is not None

    def test_empty_result_probe_lost_join_retries_to_success(
        self, tmp_path, postgres_available
    ) -> None:
        """0 строк → probe COUNT (живой канал agent_ro): данные в таблицах есть,
        джойн пуст → diagnose → ретрай → строка получена (state-agent v1.7)."""
        generate = _QueueGenerateSql([_sql(_EMPTY_SQL), _sql(_GOOD_SQL)])
        container = _container(tmp_path, generate)
        result = container.fsm_runner.run(
            initial_state("Сколько заказов?", user_id="u_analyst"), thread_id="t-retry-live-3"
        )
        assert result["outcome"] == "done"
        assert generate.seen_attempts == [1, 2]
        # ретраится с последним SQL — исполненный текст (R1: валидатор добавил LIMIT)
        assert generate.seen_last_sql[1] is not None
        assert generate.seen_last_sql[1].startswith(_EMPTY_SQL)
        assert "LIMIT" in generate.seen_last_sql[1]
        # diagnose_error в маршруте — проба заподозрила потерянный джойн
        assert "diagnose_error" in result["visited_nodes"]
        assert result["row_count"] == 1
        assert result["rows"][0]["orders_total"] > 0

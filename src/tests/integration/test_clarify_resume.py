"""Интеграционные тесты clarify resume (feat-008, S08): interrupt + checkpointer Valkey.

Живой Valkey (conftest valkey_available): сессия прогона с уточняющим вопросом
живёт в Valkey с TTL (канон 10 мин — S00 стык 1; в тестах — инъекция короткого
TTL, Ловушка 5 S08: не спать 10 минут). Resume тем же thread_id продолжает ТОТ
ЖЕ прогон (Ловушка 2); TTL истёк → clarification-терминал (ruling R3 ARCH-GATE,
валидный исход), не исключение. resolve_access — реальный PgPermissionStore
(сиды 0003, канал agent_planner — ruling R1 S05); clarify — скриптовый сервис
(LLM в тестах — моки, S00 правило 7): вердикт ambiguous → interrupt с
детерминированным вопросом. Скипаются без инфры с понятной причиной (conftest).
"""

import os
import time
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import SecretStr

from text_to_sql_agent.application.services.clarify_service import (
    ClarifyOutcome,
    ClarifyResult,
)
from text_to_sql_agent.composition.container import build_container
from text_to_sql_agent.config.settings import Settings

_SCRIPTED_QUESTION = "Заказы за какой период?"
_SCRIPTED_ANSWER = "за 2026 календарный год"


class _ScriptedClarify:
    """Скриптовый clarify-сервис: ambiguous-вердикт → interrupt (LLM — мок, S00-7)."""

    def __init__(self) -> None:
        self.classifications = 0

    def clarify(self, question: str, *, ctx: object, permissions: object) -> ClarifyResult:
        self.classifications += 1
        return ClarifyResult(outcome=ClarifyOutcome.CLARIFY, question=_SCRIPTED_QUESTION)


class _ScriptedPlanJoins:
    """S10 подключила планер к FSM: тесты interrupt/resume идут мимо LLM-ног
    планера (S00-7) — PLAN-исход с планом из активных рёбер сидов."""

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


class _ScriptedGenerateSql:
    """Генератор — мок (S00-7): SQL достаточен, чтобы happy path дошёл до done.
    S11: SQL соответствует скелету плана — реальный валидатор сверяет структуру
    (однотабличный SELECT дал бы hallucination-маршрут вместо done)."""

    def generate(self, **kwargs: object) -> object:
        from text_to_sql_agent.application.services.generate_sql_service import (
            GenerateSqlOutcome,
            GenerateSqlResult,
        )

        return GenerateSqlResult(
            outcome=GenerateSqlOutcome.SQL,
            sql=(
                "SELECT count(*) FROM orders o INNER JOIN order_items i ON o.order_id = i.order_id"
            ),
        )


def _dsn(env_var: str) -> str:
    dsn = os.getenv(env_var)
    if not dsn:
        pytest.skip(f"{env_var} не задан — скопируйте .env.example в .env")
    return dsn


_DEMO_TOKENS = "u_analyst:dev-analyst-token"


def _settings(tmp_path: Path, *, ttl_s: int) -> Settings:
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
        valkey_url=os.getenv("VALKEY_URL", "redis://127.0.0.1:6379/0"),
        neo4j_uri="bolt://127.0.0.1:7687",
        neo4j_user="neo4j",
        neo4j_password=SecretStr("neo4j-dev-password"),
        agent_ro_password=SecretStr("agent_ro"),
        agent_planner_password=SecretStr(os.getenv("AGENT_PLANNER_PASSWORD", "agent_planner")),
        agent_trace_password=SecretStr("agent_trace"),
        registry_writer_password=SecretStr("registry_writer"),
        trace_jsonl_path=tmp_path / "trace-clarify.jsonl",
        agent_dev_tokens=_DEMO_TOKENS,
        clarify_session_ttl_s=ttl_s,
    )


@pytest.fixture()
def container(tmp_path, postgres_available, valkey_available):
    """Собранный контейнер: реальный ValkeyCheckpointer (TTL 600) + скриптовые
    clarify/plan_joins/generate_sql (LLM-ноги — моки, S00-7; проверяется
    interrupt-механика, не генерация).

    Токены — только проверенные (ruling R4 S05): verify в тесте ДО прогона.
    """
    service = _ScriptedClarify()
    ck = build_container(
        _settings(tmp_path, ttl_s=600),
        clarify_service=service,
        plan_joins_service=_ScriptedPlanJoins(),  # type: ignore[arg-type]
        generate_sql_service=_ScriptedGenerateSql(),  # type: ignore[arg-type]
    )
    yield ck, service
    ck.checkpointer.close()


class TestInterruptResumeFlow:
    def test_interrupt_persists_session_in_valkey(self, container, tmp_path) -> None:
        ck, _ = container
        thread_id = uuid4().hex
        user_id = ck.auth_provider.verify("dev-analyst-token")
        result = ck.answer_question.run("Заказы", user_id=user_id, thread_id=thread_id)
        assert result.paused is True
        assert result.outcome is None  # не терминал — пауза (ruling R3)
        assert result.clarify_question == _SCRIPTED_QUESTION
        # Сессия физически в Valkey: ключи чекпоинтов с префиксом thread_id
        keys = list(ck.checkpointer._client.scan_iter(match=f"ckptr:{thread_id}:*"))  # noqa: SLF001
        assert keys, "чекпоинт приостановленного прогона не найден в Valkey"

    def test_resume_same_thread_continues_same_run(self, container) -> None:
        # Ловушка 2 S08: resume тем же thread_id — тот же прогон (ответ —
        # допущение user_answer), не новый invocation; классификация — один раз
        ck, service = container
        thread_id = uuid4().hex
        user_id = ck.auth_provider.verify("dev-analyst-token")
        paused = ck.answer_question.run("Заказы", user_id=user_id, thread_id=thread_id)
        assert paused.paused is True
        final = ck.answer_question.resume(_SCRIPTED_ANSWER, thread_id=thread_id, question="Заказы")
        assert final.outcome == "done"
        assert service.classifications == 1  # ruling R6: без повторного LLM-вызова
        assumptions = final.state["assumptions"]
        user_answers = [a for a in assumptions if a.source == "user_answer"]
        assert [a.text for a in user_answers] == [_SCRIPTED_ANSWER]
        assert final.state["visited_nodes"].count("clarify") == 2

    def test_expired_ttl_yields_clarification_terminal(
        self, tmp_path, postgres_available, valkey_available
    ) -> None:
        # TTL истёк (ruling R3): clarification-терминал — валидный исход, не
        # исключение; инъекция короткого TTL (Ловушка 5: не спать 10 минут)
        service = _ScriptedClarify()
        ck = build_container(
            _settings(tmp_path / "ttl", ttl_s=1),
            clarify_service=service,
            plan_joins_service=_ScriptedPlanJoins(),  # type: ignore[arg-type]
            generate_sql_service=_ScriptedGenerateSql(),  # type: ignore[arg-type]
        )
        try:
            thread_id = uuid4().hex
            user_id = ck.auth_provider.verify("dev-analyst-token")
            paused = ck.answer_question.run("Заказы", user_id=user_id, thread_id=thread_id)
            assert paused.paused is True
            time.sleep(1.6)  # TTL=1 c + запас: ключи сессии истекли
            final = ck.answer_question.resume(
                "поздний ответ", thread_id=thread_id, question="Заказы"
            )
            assert final.outcome == "clarification"
            assert final.state["error"]["class"] == "clarify_session_expired"
        finally:
            ck.checkpointer.close()

    def test_other_thread_is_not_resume(self, container) -> None:
        # resume с ЧУЖИМ thread_id — сессии нет → clarification (ruling R3),
        # не продолжение чужого прогона
        ck, _ = container
        thread_id = uuid4().hex
        user_id = ck.auth_provider.verify("dev-analyst-token")
        paused = ck.answer_question.run("Заказы", user_id=user_id, thread_id=thread_id)
        assert paused.paused is True
        final = ck.answer_question.resume(
            _SCRIPTED_ANSWER, thread_id=uuid4().hex, question="Заказы"
        )
        assert final.outcome == "clarification"

    def test_run_without_thread_id_returns_resumable_id(self, container) -> None:
        # Ревью S08 Important-1 (live): пауза с ненаружным id — тупик; use case
        # генерирует thread_id сам и возвращает его в FSMResult.thread_id
        ck, _ = container
        user_id = ck.auth_provider.verify("dev-analyst-token")
        paused = ck.answer_question.run("Заказы", user_id=user_id)  # thread_id не задан
        assert paused.paused is True
        assert paused.thread_id
        final = ck.answer_question.resume(
            _SCRIPTED_ANSWER, thread_id=paused.thread_id, question="Заказы"
        )
        assert final.outcome == "done"

    def test_fsm_through_container_full_path(self, container) -> None:
        # Полный контур: verify → resolve_access (БД, сиды 0003) → clarify
        # interrupt (Valkey-сессия) → resume → done с правами и допущениями
        ck, service = container
        thread_id = uuid4().hex
        user_id = ck.auth_provider.verify("dev-analyst-token")
        assert user_id == "u_analyst"
        paused = ck.answer_question.run(
            "Сколько заказов у клиентов из РФ?", user_id=user_id, thread_id=thread_id
        )
        assert paused.paused is True
        assert paused.state["permissions"] == {  # права resolve_access в сессии
            "orders": ["*"],
            "order_items": ["*"],
            "products": ["*"],
        }
        final = ck.answer_question.resume(
            _SCRIPTED_ANSWER, thread_id=thread_id, question="Сколько заказов у клиентов из РФ?"
        )
        assert final.outcome == "done"
        assert final.state["graph_version"] == 0  # пин S07 (заглушка plan_joins)
        assert any(a.text == _SCRIPTED_ANSWER for a in final.state["assumptions"])
        assert service.classifications == 1

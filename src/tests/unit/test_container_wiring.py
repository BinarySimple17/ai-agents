"""Проверка wiring контейнера (S06, ревью-2): дефолтный runtime-контур не держит
write_dsn памяти знаний (ruling W1 ARCH-GATE S06, ревью I2 — периметр прав);
writer — только офлайн-индексатор с флагом knowledge_writer=True.

build_container строит реальные компоненты, но ни один не открывает соединений
на сборке (ленивые клиенты/DSN-строки) — тест без сети и БД.
"""

import pytest
from pydantic import SecretStr

from text_to_sql_agent.application.ports.vector_store import DocKind, KnowledgeDocument
from text_to_sql_agent.composition.container import build_container
from text_to_sql_agent.config.settings import Settings

_DOC = KnowledgeDocument(doc_kind=DocKind.DDL, content="x", model_version="m", source_ref="s")
_VEC = [0.01] * 1024


def _settings(tmp_path) -> Settings:
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
        postgres_dsn="postgresql://postgres:pw@127.0.0.1:5432/agent",
        agent_ro_dsn="postgresql://agent_ro:pw@127.0.0.1:5432/agent",
        agent_trace_dsn="postgresql://agent_trace:pw@127.0.0.1:5432/agent",
        valkey_url="redis://127.0.0.1:6379/0",
        neo4j_uri="bolt://127.0.0.1:7687",
        neo4j_user="neo4j",
        neo4j_password=SecretStr("pw"),
        agent_ro_password=SecretStr("agent_ro"),
        agent_planner_password=SecretStr("agent_planner"),
        agent_trace_password=SecretStr("agent_trace"),
        registry_writer_password=SecretStr("registry_writer"),
        trace_jsonl_path=tmp_path / "trace.jsonl",
    )


def test_default_container_cannot_write_knowledge(tmp_path) -> None:
    # Runtime-контур (CLI/FSM) не держит сервисный DSN записи в память знаний
    container = build_container(_settings(tmp_path))
    with pytest.raises(RuntimeError, match="write_dsn"):
        container.vector_store.upsert([_DOC], [_VEC])


def test_knowledge_writer_container_has_write_dsn(tmp_path) -> None:
    # Офлайн-индексатор собирает контейнер с write-каналом (make index-knowledge)
    settings = _settings(tmp_path)
    container = build_container(settings, knowledge_writer=True)
    assert (
        container.vector_store._write_dsn  # noqa: SLF001 — инвариант ruling W1
        == settings.postgres_dsn.get_secret_value()
    )


def test_plan_joins_budget_shlew_wired_to_adapter(tmp_path) -> None:
    # Шов бюджета узла plan_joins (ruling Q7 ARCH-GATE S09): остаток =
    # кап запроса − резидентный аккумулятор адаптера; на сборке аккумулятор 0
    container = build_container(_settings(tmp_path))
    assert type(container.plan_joins_service).__name__ == "PlanJoinsService"
    assert container.plan_joins_service._budget_remaining_roubles is not None  # noqa: SLF001
    assert container.plan_joins_service._budget_remaining_roubles() == pytest.approx(2.0)  # noqa: SLF001
    assert container.edge_proposal_store is not None  # очередь заявок собрана (миграция 0009)


def test_build_container_respects_rrf_alpha_dense(tmp_path) -> None:
    # I3 ревью S26: измерительный контур (bench dense-only α=1.0) собирается
    # ТОЛЬКО через build_container — сборка адаптера вне composition запрещена
    container = build_container(_settings(tmp_path), rrf_alpha_dense=1.0)
    assert container.vector_store._rrf_alpha_dense == 1.0  # noqa: SLF001 — инвариант сборки

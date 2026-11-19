"""Юнит-тесты LLM-клиента (S03 Tasks 3-4) — шлюз ТОЛЬКО моками (S00 правило 7).

Проверяются пункты behavior feat-003: роль → ответ; 429/5xx → backoff (1→2→4с
с jitter, ADR-007 §4.3) и circuit breaker (5 сбоев → пауза 60с, half-open с
переоткрытием на неудачной пробе); превышение бюджета → исключение ДО вызова;
таймаут — отказ БЕЗ инкремента breaker (ARCH-GATE S03 W2); трейсер best-effort —
сбой записи не ломает оплаченный вызов (ревью S03 Important-2). Часы/sleep/rng —
инъекция (ловушка 3: тесты breaker не спят).
"""

import json
import random
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest
from pydantic import SecretStr

from text_to_sql_agent.application.ports.llm_provider import (
    BudgetExceededError,
    CircuitOpenError,
    LLMProviderError,
    LLMRequestError,
    LLMResponse,
)
from text_to_sql_agent.config.settings import Settings
from text_to_sql_agent.domain.entities import Trace
from text_to_sql_agent.infrastructure.llm.openai_provider import OpenAIProvider
from text_to_sql_agent.infrastructure.observability.jsonl_tracer import JsonlTracer

_MESSAGES = [{"role": "user", "content": "вопрос"}]
_GATEWAY_URL = "https://gateway.test/api/v1/chat/completions"
# 5 сбоев breaker × 4 попытки backoff = HTTP-вызовов на серию
_FAILURE_REQUESTS = 5 * 4


# --- герметичные Settings: все поля явно, ambient-.env не влияет ---
def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "llm_base_url": "https://gateway.test/api/v1",
        "llm_api_key": SecretStr("sk-test"),
        "llm_request_timeout_s": 15.0,
        "model_cheap": "cheap-1",
        "model_mid": "mid-1",
        "model_strong": "strong-1",
        "budget_per_request_rub": 2.0,
        "model_cheap_price_in": 3.2,
        "model_cheap_price_out": 13.0,
        "model_mid_price_in": 8.0,
        "model_mid_price_out": 12.0,
        "model_strong_price_in": 299.0,
        "model_strong_price_out": 939.0,
        "embedding_model": "embed-1",
        "postgres_dsn": "postgresql://postgres:postgres@127.0.0.1:5432/agent",
        "agent_ro_dsn": "postgresql://agent_ro:agent_ro@127.0.0.1:5432/agent",
        "agent_trace_dsn": "postgresql://agent_trace:agent_trace@127.0.0.1:5432/agent",
        "valkey_url": "redis://127.0.0.1:6379/0",
        "neo4j_uri": "bolt://127.0.0.1:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": SecretStr("neo4j-pw"),
        "agent_ro_password": SecretStr("agent_ro"),
        "agent_planner_password": SecretStr("agent_planner"),
        "agent_trace_password": SecretStr("agent_trace"),
        "registry_writer_password": SecretStr("registry_writer"),
        "trace_jsonl_path": Path(".harness/traces/trace.jsonl"),
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


# --- моки шлюза (S00 правило 7): скрипт событий вместо сети ---
def _ok_response(model: str = "cheap-1", tokens_in: int = 100, tokens_out: int = 20) -> Any:
    return SimpleNamespace(
        model=model,
        usage=SimpleNamespace(prompt_tokens=tokens_in, completion_tokens=tokens_out),
        choices=[SimpleNamespace(message=SimpleNamespace(content="SELECT 1", tool_calls=None))],
    )


def _ok_response_no_usage(model: str = "cheap-1") -> Any:
    """Шлюз без usage в ответе (ревью S03 Important-4: не маскировать нулевую стоимость)."""
    return SimpleNamespace(
        model=model,
        usage=None,
        choices=[SimpleNamespace(message=SimpleNamespace(content="SELECT 1", tool_calls=None))],
    )


def _http_error(status: int) -> openai.APIStatusError:
    request = httpx.Request("POST", _GATEWAY_URL)
    response = httpx.Response(status, request=request)
    cls = (
        openai.RateLimitError
        if status == 429
        else openai.InternalServerError
        if status >= 500
        else openai.BadRequestError
    )
    return cls(f"HTTP {status}", response=response, body=None)


def _timeout_error() -> openai.APITimeoutError:
    return openai.APITimeoutError(httpx.Request("POST", _GATEWAY_URL))


class FakeCompletions:
    """Скриптованный chat.completions.create: исключение — бросить, иное — вернуть.

    Пустой скрипт = AssertionError: тест громко падает, если провайдер дошёл
    до сети там, где не должен (например, бюджетный отказ после исчерпания).
    """

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls = 0
        self.kwargs: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls += 1
        self.kwargs.append(kwargs)
        if not self.script:
            raise AssertionError("мок-шлюз вызван, но сценарий исчерпан")
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def _provider(
    script: list[Any],
    settings: Settings | None = None,
    tracer: JsonlTracer | None = None,
) -> tuple[OpenAIProvider, FakeCompletions, list[float], list[float]]:
    completions = FakeCompletions(script)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    sleeps: list[float] = []
    clock = [1000.0]  # box: провайдер хранит состояние breaker по этому времени
    provider = OpenAIProvider(
        settings or _settings(),
        tracer=tracer,
        client=client,  # type: ignore[arg-type] — тестовый двойник SDK-клиента
        clock=lambda: clock[0],
        sleep=lambda delay: (sleeps.append(delay), clock.__setitem__(0, clock[0] + delay)),
        rng=random.Random(42),
    )
    return provider, completions, sleeps, clock


# --- успешный вызов ---
def test_role_resolves_model_temperature_zero() -> None:
    provider, completions, _, _ = _provider([_ok_response(model="mid-1")])
    response = provider.complete("mid", _MESSAGES)
    assert isinstance(response, LLMResponse)
    assert response.model == "mid-1"  # фактическая модель шлюза — passthrough
    assert response.text == "SELECT 1"
    call = completions.kwargs[0]
    assert call["model"] == "mid-1"
    assert call["temperature"] == 0.0  # ADR-008
    assert call["messages"] == _MESSAGES
    assert "tools" not in call  # без тулов параметр не передаётся


def test_tools_passed_through() -> None:
    provider, completions, _, _ = _provider([_ok_response()])
    tools = [{"type": "function", "function": {"name": "run_sql", "parameters": {}}}]
    provider.complete("cheap", _MESSAGES, tools=tools)
    assert completions.kwargs[0]["tools"] == tools


# --- reasoning_content: локальный шлюз (D-020) уносит ответ в think-поле ---
def _reasoning_response(
    *,
    content: str | None,
    reasoning: str | None,
    tool_calls: list[Any] | None,
) -> Any:
    """Шлюз в форме «reasoning отдельным полем» (прогон 2026-11-19)."""
    return SimpleNamespace(
        model="mid-1",
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=content,
                    reasoning_content=reasoning,
                    tool_calls=tool_calls,
                )
            )
        ],
    )


_TOOL_CALL = SimpleNamespace(
    id="t1",
    function=SimpleNamespace(name="run_sql", arguments='{"sql":"SELECT 1"}'),
)


def test_reasoning_content_used_as_text_when_content_empty() -> None:
    # Регрессия на прогон 2026-11-19: финальный ответ в reasoning_content при
    # content="" — раньше цикл получал «ни текста, ни тулов» и уходил в
    # деградацию, хотя ответ у модели был.
    provider, _, _, _ = _provider(
        [
            _reasoning_response(
                content="", reasoning="Ответ: невозможно определить.", tool_calls=None
            )
        ]
    )
    response = provider.complete("mid", _MESSAGES)
    assert response.text == "Ответ: невозможно определить."
    assert response.tool_calls == []


def test_content_takes_precedence_over_reasoning_content() -> None:
    # Финальный текст из content всегда главнее think-поля.
    provider, _, _, _ = _provider(
        [
            _reasoning_response(
                content="Финальный ответ", reasoning="черновик размышления", tool_calls=None
            )
        ]
    )
    assert provider.complete("mid", _MESSAGES).text == "Финальный ответ"


def test_reasoning_content_ignored_when_tool_calls_present() -> None:
    # Ход с тулом: reasoning не становится текстом ответа — content пуст это
    # норма хода с тулом, историю формирует блок tool_calls.
    provider, _, _, _ = _provider(
        [_reasoning_response(content="", reasoning="какой SQL звать?", tool_calls=[_TOOL_CALL])]
    )
    response = provider.complete("mid", _MESSAGES)
    assert response.text is None
    assert [call.name for call in response.tool_calls] == ["run_sql"]


def test_gateway_without_reasoning_field_unchanged() -> None:
    # Сообщение вовсе без поля reasoning_content (чужой шлюз / облако):
    # getattr-фолбэк не ломает — прежнее поведение (text=None).
    ok = _ok_response()
    ok.choices[0].message = SimpleNamespace(content=None, tool_calls=None)
    provider, _, _, _ = _provider([ok])
    assert provider.complete("mid", _MESSAGES).text is None


def test_usage_counts_toward_budget() -> None:
    provider, _, _, _ = _provider([_ok_response()])
    provider.complete("cheap", _MESSAGES)
    # 100 in × 3.2₽/1М + 20 out × 13₽/1М = 0.00058 ₽ (цены ролей — из Settings)
    assert provider.budget_used_rub == pytest.approx(0.00058, rel=1e-6)


def test_gateway_without_usage_zero_cost_flagged_in_trace(tmp_path: Path) -> None:
    tracer = JsonlTracer(tmp_path / "trace.jsonl")
    provider, _, _, _ = _provider([_ok_response_no_usage()], tracer=tracer)
    response = provider.complete("cheap", _MESSAGES)
    assert response.usage.tokens_in == 0
    assert provider.budget_used_rub == 0.0
    record = json.loads((tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["payload"]["usage_missing"] is True  # не маскировать нулевую стоимость


def test_unknown_role_rejected_before_http() -> None:
    provider, completions, _, _ = _provider([])
    with pytest.raises(LLMProviderError, match="роль"):
        provider.complete("mega", _MESSAGES)  # type: ignore[arg-type]
    assert completions.calls == 0


# --- backoff 1→2→4с с jitter (ADR-007 §4.3, ARCH-GATE W1) ---
def test_backoff_on_429_three_retries_then_success() -> None:
    script = [_http_error(429), _http_error(429), _http_error(429), _ok_response()]
    provider, completions, sleeps, _ = _provider(script)
    response = provider.complete("cheap", _MESSAGES)
    assert response.text == "SELECT 1"
    assert len(sleeps) == 3
    assert 1.0 <= sleeps[0] <= 1.5  # base 1с + jitter ≤ 50%
    assert 2.0 <= sleeps[1] <= 3.0  # base 2с + jitter
    assert 4.0 <= sleeps[2] <= 6.0  # base 4с + jitter — таблица 1→2→4 полная (Critical-1)
    assert provider.consecutive_failures == 0  # успех сбрасывает счётчик
    assert completions.calls == 4


def test_backoff_on_5xx_single_retry() -> None:
    provider, completions, sleeps, _ = _provider([_http_error(503), _ok_response()])
    assert provider.complete("cheap", _MESSAGES).text == "SELECT 1"
    assert completions.calls == 2
    assert 1.0 <= sleeps[0] <= 1.5


def test_retries_exhausted_raises_and_counts_one_failure() -> None:
    provider, completions, sleeps, _ = _provider([_http_error(500)] * 4)
    with pytest.raises(LLMRequestError, match="после 4 попыток"):
        provider.complete("cheap", _MESSAGES)
    assert completions.calls == 4
    assert len(sleeps) == 3  # паузы 1/2/4 (+jitter)
    assert provider.consecutive_failures == 1  # неудачный ЗАПРОС — один сбой


def test_non_retryable_4xx_immediate_no_breaker_count() -> None:
    provider, completions, sleeps, _ = _provider([_http_error(401)])
    with pytest.raises(LLMRequestError, match="401"):
        provider.complete("cheap", _MESSAGES)
    assert completions.calls == 1  # без ретраев: конфиг-ошибка не лечится повтором
    assert sleeps == []
    assert provider.consecutive_failures == 0


# --- таймаут ≠ сбой breaker (ARCH-GATE W2) ---
def test_timeout_no_breaker_increment_no_retry() -> None:
    provider, completions, sleeps, _ = _provider([_timeout_error()])
    with pytest.raises(LLMRequestError, match="таймаут"):
        provider.complete("cheap", _MESSAGES)
    assert completions.calls == 1  # таймаут не ретраится — отказ узла
    assert sleeps == []
    assert provider.consecutive_failures == 0  # «breaker считает ошибки, не медлительность»


def test_timeout_then_5xx_failure_breaker_still_closed() -> None:
    # Первый вызов — таймаут (не сбой), второй — 5xx на всех попытках (один сбой):
    # счётчик считает только сетевые ошибки, breaker не открывается
    provider, completions, _, _ = _provider([_timeout_error()] + [_http_error(500)] * 4)
    with pytest.raises(LLMRequestError):
        provider.complete("cheap", _MESSAGES)
    with pytest.raises(LLMRequestError):
        provider.complete("cheap", _MESSAGES)
    assert provider.consecutive_failures == 1  # только сетевой 5xx — сбой
    assert provider.breaker_open is False
    assert completions.calls == 5


# --- circuit breaker: 5 сбоев подряд → пауза 60с, half-open ---
def test_breaker_opens_after_five_failures_blocks_http() -> None:
    provider, completions, _, _ = _provider([_http_error(500)] * _FAILURE_REQUESTS)
    for _ in range(5):
        with pytest.raises(LLMRequestError):
            provider.complete("cheap", _MESSAGES)
    assert provider.breaker_open is True
    with pytest.raises(CircuitOpenError, match="breaker"):
        provider.complete("cheap", _MESSAGES)  # 6-й вызов отклонён ДО сети
    assert completions.calls == _FAILURE_REQUESTS  # HTTP больше не дёргается


def test_breaker_allows_call_after_cooldown() -> None:
    provider, completions, _, clock = _provider(
        [_http_error(500)] * _FAILURE_REQUESTS + [_ok_response()]
    )
    for _ in range(5):
        with pytest.raises(LLMRequestError):
            provider.complete("cheap", _MESSAGES)
    clock[0] += 61  # пауза 60с истекла — half-open
    assert provider.breaker_open is False
    response = provider.complete("cheap", _MESSAGES)
    assert response.text == "SELECT 1"
    assert provider.consecutive_failures == 0  # свежий отсчёт после открытия
    assert completions.calls == _FAILURE_REQUESTS + 1


def test_breaker_reopens_on_failed_half_open_probe() -> None:
    provider, completions, _, clock = _provider([_http_error(500)] * (_FAILURE_REQUESTS + 4))
    for _ in range(5):
        with pytest.raises(LLMRequestError):
            provider.complete("cheap", _MESSAGES)
    clock[0] += 61
    with pytest.raises(LLMRequestError):
        provider.complete("cheap", _MESSAGES)  # пробный запрос — снова 5xx
    assert provider.breaker_open is True  # переоткрыт немедленно, не через 5 серий
    with pytest.raises(CircuitOpenError):
        provider.complete("cheap", _MESSAGES)
    assert completions.calls == _FAILURE_REQUESTS + 4


# --- бюджетный предохранитель: исключение ДО вызова (behavior feat-003) ---
def test_budget_exceeded_raises_before_call() -> None:
    settings = _settings(budget_per_request_rub=0.0001)  # один вызов дороже капа
    provider, completions, _, _ = _provider([_ok_response()], settings=settings)
    provider.complete("cheap", _MESSAGES)  # первый вызов прошёл, копилка переполнена
    assert provider.budget_used_rub >= settings.budget_per_request_rub
    with pytest.raises(BudgetExceededError, match="кап бюджета"):
        provider.complete("cheap", _MESSAGES)  # следующий — отказ ДО шлюза
    assert completions.calls == 1  # мок не дёрнут


def test_reset_request_budget_allows_next_request() -> None:
    settings = _settings(budget_per_request_rub=0.0001)
    provider, _, _, _ = _provider([_ok_response(), _ok_response()], settings=settings)
    provider.complete("cheap", _MESSAGES)
    with pytest.raises(BudgetExceededError):
        provider.complete("cheap", _MESSAGES)
    provider.reset_request_budget()  # новый запрос пользователя (оркестратор, S04)
    assert provider.complete("cheap", _MESSAGES).text == "SELECT 1"


def test_real_client_disables_sdk_retries() -> None:
    provider = OpenAIProvider(_settings())  # настоящий SDK-клиент (без сети на init)
    assert provider._client.max_retries == 0  # noqa: SLF001 — инвариант ловушки 2
    assert provider._client.timeout == 15.0


def test_embed_declared_but_not_implemented_yet() -> None:
    provider, _, _, _ = _provider([])
    with pytest.raises(NotImplementedError, match="S06"):
        provider.embed(["SELECT 1"])


# --- Tracer (Task 4): JSONL вне git; best-effort; события вызовов ---
def test_jsonl_tracer_records_lines(tmp_path: Path) -> None:
    tracer = JsonlTracer(tmp_path / "traces" / "trace.jsonl")
    tracer.record(Trace(event="llm.complete", payload={"role": "cheap"}))
    tracer.record(Trace(event="llm.retry", payload={}))
    lines = (tmp_path / "traces" / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["event"] == "llm.complete"
    assert first["payload"]["role"] == "cheap"
    assert first["ts"].startswith("20")  # ISO-метка при записи


def test_tracer_failure_does_not_break_paid_call(tmp_path: Path) -> None:
    # parent — файл, mkdir/open невозможны: запись падает, вызов продолжается
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir", encoding="utf-8")
    tracer = JsonlTracer(blocker / "trace.jsonl")
    provider, _, _, _ = _provider([_ok_response()], tracer=tracer)
    with pytest.warns(RuntimeWarning, match="JsonlTracer"):
        response = provider.complete("cheap", _MESSAGES)
    assert response.text == "SELECT 1"
    assert provider.budget_used_rub > 0  # учёт не пострадал


def test_provider_traces_retry_and_complete_events(tmp_path: Path) -> None:
    tracer = JsonlTracer(tmp_path / "trace.jsonl")
    provider, _, _, _ = _provider([_http_error(429), _ok_response()], tracer=tracer)
    provider.complete("cheap", _MESSAGES)
    events = [
        json.loads(line)["event"]
        for line in (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert events == ["llm.retry", "llm.complete"]  # failure-события не теряются (Imp-5)


def test_provider_traces_budget_stop(tmp_path: Path) -> None:
    tracer = JsonlTracer(tmp_path / "trace.jsonl")
    settings = _settings(budget_per_request_rub=0.0001)
    provider, _, _, _ = _provider([_ok_response()], settings=settings, tracer=tracer)
    provider.complete("cheap", _MESSAGES)
    with pytest.raises(BudgetExceededError):
        provider.complete("cheap", _MESSAGES)
    events = [
        json.loads(line)["event"]
        for line in (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert events == ["llm.complete", "llm.budget_stop"]


def test_provider_writes_trace_on_success(tmp_path: Path) -> None:
    tracer = JsonlTracer(tmp_path / "trace.jsonl")
    provider, _, _, _ = _provider([_ok_response()], tracer=tracer)
    provider.complete("cheap", _MESSAGES)
    record = json.loads((tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["event"] == "llm.complete"
    assert record["payload"]["model"] == "cheap-1"
    assert record["payload"]["tokens_in"] == 100
    assert record["payload"]["cost_rub"] == pytest.approx(0.00058, abs=1e-6)

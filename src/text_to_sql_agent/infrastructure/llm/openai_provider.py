"""Адаптер `LLMProvider`: OpenAI-совместимый шлюз — облако routerai.ru или локальный
LM Studio (D-020: пустой `LLM_BASE_URL` → `http://127.0.0.1:1234/v1`; S03 Task 3).

Инварианты стыка 2 (S00):
- backoff 1→2→4с **с jitter** (ADR-007 §4.3; ARCH-GATE S03 W1 — ADR старше и
  специфичнее S00, синхрон формулировки — в том же коммите): 4 попытки,
  паузы 1(+j)/2(+j)/4(+j) между ними — таблица 1→2→4 достижима только на
  4-й попытке (ревью S03 Critical-1);
- circuit breaker: 5 сбоев подряд → пауза 60с. Сбой = неудачный запрос после
  исчерпания ретраев (429/5xx/сеть); **таймаут — отказ без инкремента счётчика**
  («breaker считает ошибки, не медлительность» — ARCH-GATE S03 W2). Half-open:
  первый вызов после паузы — пробный, неудача пробного запроса переоткрывает
  breaker немедленно (ревью S03 Minor-7);
- temperature 0 (ADR-008), SDK-ретраи выключены (`max_retries=0`, ловушка 2 —
  иначе 429 обрабатывается дважды и тайминги плавают);
- `usage` в ответе (FIX-005) → учёт ₽ по ценам `MODEL_*_PRICE_*` из Settings;
  шлюз без usage фиксируется в трейсе (`usage_missing`) — стоимость 0 не маскируется
  (ревью S03 Important-4);
- состояние breaker/бюджета — в экземпляре провайдера, не в модуле (ловушка 1:
  иначе тесты сцеплены порядком выполнения).

Бюджетный предохранитель S03 — кап **на запрос**, best-effort: провайдер
накапливает стоимость успешных вызовов против `BUDGET_PER_REQUEST_RUB`,
превышение → `BudgetExceededError` ДО следующего вызова (первая строка
`complete()`, решение ARCH-GATE S03). Превышение фиксируется ПО факту вызова —
usage недоступен до ответа, поэтому один вызов может выйти за кап; жёсткий
пре-колл-контроль с оценкой стоимости и резервом — S15 (порт `BudgetGuard`
поглощает эту проверку, ADR-007 §4.1/§4.3). Учёт пер-запросный: оркестратор
(S04) вызывает `reset_request_budget()` на старте прогона.

Трейс — события вызовов (FR-11): `llm.complete`/`llm.retry`/`llm.timeout`/
`llm.breaker_open`/`llm.budget_stop`; сборка агрегата `node_timings` — S04/S15
(ревью S03 Important-5). Часы/sleep/rng — инъекция (ловушка 3: тесты breaker
не растягивают прогон реальными sleep); в проде — monotonic/time.sleep/random.
"""

import random
import time
from collections.abc import Callable, Sequence
from typing import Any

import openai

from text_to_sql_agent.application.ports.llm_provider import (
    BudgetExceededError,
    CircuitOpenError,
    Embedding,
    LLMProviderError,
    LLMRequestError,
    LLMResponse,
    Messages,
    Role,
    ToolCall,
    ToolSpec,
    Usage,
)
from text_to_sql_agent.application.ports.tracer import Tracer
from text_to_sql_agent.config.settings import Settings
from text_to_sql_agent.domain.entities import Trace

_BACKOFF_BASE_S = 1.0
_BACKOFF_ATTEMPTS = 4  # попытки 0..3: паузы 1(+j), 2(+j), 4(+j) — таблица 1→2→4
_BREAKER_THRESHOLD = 5
_BREAKER_COOLDOWN_S = 60.0
_JITTER_SHARE = 0.5  # jitter: base + uniform(0, base/2) — ADR-007 §4.3


def _jittered(base_s: float, rng: random.Random) -> float:
    """Пауза backoff с jitter: без него синхронные ретраи бьют шлюз волнами (ADR-007)."""
    return base_s + rng.uniform(0, base_s * _JITTER_SHARE)


class OpenAIProvider:
    """Реализация `LLMProvider` на openai SDK; роль → модель/цены — из `Settings`."""

    def __init__(
        self,
        settings: Settings,
        *,
        tracer: Tracer | None = None,
        client: openai.OpenAI | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._settings = settings
        self._client = client or openai.OpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key.get_secret_value(),
            max_retries=0,  # ретраи — наша политика, SDK-ретраи дают двойной backoff
            timeout=settings.llm_request_timeout_s,
        )
        self._tracer = tracer
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._models: dict[str, str] = {
            "cheap": settings.model_cheap,
            "mid": settings.model_mid,
            "strong": settings.model_strong,
        }
        self._prices: dict[str, tuple[float, float]] = {
            "cheap": (settings.model_cheap_price_in, settings.model_cheap_price_out),
            "mid": (settings.model_mid_price_in, settings.model_mid_price_out),
            "strong": (settings.model_strong_price_in, settings.model_strong_price_out),
        }
        # Пер-запросное состояние (см. reset_request_budget): кап — «на один запрос»
        self._budget_used_rub = 0.0
        # Состояние breaker — в экземпляре, не в глобальной переменной модуля (ловушка 1)
        self._consecutive_failures = 0
        self._breaker_open_until = 0.0
        self._probe_pending = False  # half-open: первый вызов после паузы — пробный

    # --- наблюдаемость состояния (для тестов и узлов деградации) ---
    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def breaker_open(self) -> bool:
        return self._clock() < self._breaker_open_until

    @property
    def budget_used_rub(self) -> float:
        return self._budget_used_rub

    def reset_request_budget(self) -> None:
        """Обнулить учёт на новый запрос пользователя (вызывает оркестратор, S04)."""
        self._budget_used_rub = 0.0

    # --- трейс: best-effort события; ломающийся трейсер не влияет на вызов ---
    def _emit(self, event: str, **payload: Any) -> None:
        if self._tracer is not None:
            self._tracer.record(Trace(event=event, payload=payload))

    # --- предохранители: кап бюджета ПЕРВОЙ строкой, затем breaker ---
    def _guard_budget(self) -> None:
        if self._budget_used_rub >= self._settings.budget_per_request_rub:
            self._emit(
                "llm.budget_stop",
                budget_used_rub=round(self._budget_used_rub, 6),
                cap_rub=self._settings.budget_per_request_rub,
            )
            raise BudgetExceededError(
                f"кап бюджета запроса исчерпан: {self._budget_used_rub:.4f} ₽ "
                f">= {self._settings.budget_per_request_rub} ₽ — вызов шлюза не выполняется"
            )

    def _guard_breaker(self) -> None:
        if self.breaker_open:
            raise CircuitOpenError(
                "circuit breaker открыт после серии сбоев шлюза — вызов отклонён без HTTP; "
                "деградацию решает оркестратор (ADR-007 §4.3, M6)"
            )

    def complete(
        self,
        role: Role,
        messages: Messages,
        *,
        tools: Sequence[ToolSpec] | None = None,
    ) -> LLMResponse:
        self._guard_budget()
        self._guard_breaker()
        model = self._models.get(role)
        if model is None:
            raise LLMProviderError(
                f"неизвестная роль модели: {role!r} (ожидаются cheap/mid/strong)"
            )

        payload: list[dict[str, Any]] = [dict(m) for m in messages]
        request_kwargs: dict[str, Any] = {
            "model": model,
            "messages": payload,
            "temperature": 0.0,  # ADR-008: структурированные выходы — детерминизм
        }
        if tools:
            request_kwargs["tools"] = [dict(t) for t in tools]

        started = self._clock()
        last_error: Exception | None = None
        for attempt in range(_BACKOFF_ATTEMPTS):
            try:
                response = self._client.chat.completions.create(**request_kwargs)
            except openai.APITimeoutError as exc:
                # W2: таймаут — отказ узла БЕЗ инкремента breaker и без ретраев
                self._emit(
                    "llm.timeout",
                    role=role,
                    elapsed_ms=round((self._clock() - started) * 1000),
                )
                raise LLMRequestError(
                    f"таймаут шлюза ({self._settings.llm_request_timeout_s}s) на роли {role}"
                ) from exc
            except openai.APIStatusError as exc:
                if not self._is_retryable(exc):
                    # 4xx (кроме 429) — конфигурационная ошибка: ретраи и breaker не помогут
                    self._emit("llm.reject", role=role, status_code=exc.status_code)
                    raise LLMRequestError(f"шлюз отклонил запрос (HTTP {exc.status_code})") from exc
                last_error = exc
            except openai.APIConnectionError as exc:
                last_error = exc  # сетевая недоступность — сбой шлюза: ретраим, считаем
            except openai.APIError as exc:
                # прочие SDK-ошибки (в т.ч. APIResponseValidationError) — наружу
                # типизированно, без ретрая и без счётчика breaker (семантика неизвестна)
                raise LLMRequestError(f"SDK-ошибка шлюза: {exc}") from exc
            else:
                return self._on_success(role, response, latency_s=self._clock() - started)
            if attempt < _BACKOFF_ATTEMPTS - 1:
                # llm.retry — только когда ретрай реально будет: последняя попытка
                # видна в итоговом LLMRequestError (ревью S03, нит к Important-5)
                self._emit(
                    "llm.retry",
                    role=role,
                    attempt=attempt + 1,
                    status_code=getattr(last_error, "status_code", "connection"),
                )
                self._sleep(_jittered(_BACKOFF_BASE_S * 2**attempt, self._rng))

        self._register_failure()
        raise LLMRequestError(
            f"шлюз не ответил после {_BACKOFF_ATTEMPTS} попыток на роли {role}"
        ) from last_error

    def embed(self, texts: Sequence[str]) -> list[Embedding]:
        # Сигнатура порта зафиксирована в S03; реализация эмбеддера — S06 (model-card).
        raise NotImplementedError("embed: реализация эмбеддера — S06")

    # --- внутреннее ---

    @staticmethod
    def _is_retryable(exc: openai.APIStatusError) -> bool:
        return exc.status_code == 429 or exc.status_code >= 500

    def _register_failure(self) -> None:
        if self._probe_pending:
            # Пробный запрос после cooldown не удался — переоткрываем немедленно (Minor-7)
            self._breaker_open_until = self._clock() + _BREAKER_COOLDOWN_S
            self._emit("llm.breaker_open", reason="half-open probe failed")
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= _BREAKER_THRESHOLD:
            self._breaker_open_until = self._clock() + _BREAKER_COOLDOWN_S
            self._consecutive_failures = 0
            self._probe_pending = True  # после паузы — один пробный запрос
            self._emit("llm.breaker_open", failures=_BREAKER_THRESHOLD)

    def _on_success(self, role: str, response: Any, *, latency_s: float) -> LLMResponse:
        raw_usage = getattr(response, "usage", None)
        usage = Usage(
            tokens_in=getattr(raw_usage, "prompt_tokens", 0) or 0,
            tokens_out=getattr(raw_usage, "completion_tokens", 0) or 0,
        )
        price_in, price_out = self._prices[role]
        cost_rub = usage.tokens_in / 1e6 * price_in + usage.tokens_out / 1e6 * price_out
        self._budget_used_rub += cost_rub
        self._consecutive_failures = 0
        self._probe_pending = False

        choice = response.choices[0]
        message = choice.message
        tool_calls = [
            ToolCall(id=call.id, name=call.function.name, arguments=call.function.arguments)
            for call in (message.tool_calls or [])
        ]
        llm_response = LLMResponse(
            text=message.content,
            tool_calls=tool_calls,
            usage=usage,
            model=response.model,
        )
        self._emit(
            "llm.complete",
            role=role,
            model=response.model,
            tokens_in=usage.tokens_in,
            tokens_out=usage.tokens_out,
            usage_missing=raw_usage is None,  # шлюз без usage — стоимость 0 не маскируется
            cost_rub=round(cost_rub, 6),
            latency_ms=round(latency_s * 1000),
            budget_used_rub=round(self._budget_used_rub, 6),
        )
        return llm_response

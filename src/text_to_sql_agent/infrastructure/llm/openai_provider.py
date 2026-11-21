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

Стрим (S19, `complete_stream`) — тот же словарь событий: llm.complete несёт
`stream=true` + `ttft_ms` (NFR-1, время до первого чанка); usage — из
терминального чанка шлюза (шлют не все — `usage_missing=true`, стоимость 0 не
маскируется, finding ARCH-GATE S19). Reasoning-дельты буферизуются, наружу —
фолбэком одним чанком по guard'у `NO_TEXT_FALLBACK_REASONS` из порта (D-021-
аналог, ruling W4 ARCH-GATE S19). Tool_calls-дельты накапливаются в полные
`ToolCall` и выходят списком в `StreamFinal.tool_calls` (S25 feat-020 —
зеркало буферного пути); в stdout они не печатаются.
"""

import json
import random
import time
from collections.abc import Callable, Iterator, Sequence
from typing import Any

import httpx
import openai

from text_to_sql_agent.application.ports.llm_provider import (
    NO_TEXT_FALLBACK_REASONS,
    BudgetExceededError,
    CircuitOpenError,
    Embedding,
    LLMProviderError,
    LLMRequestError,
    LLMResponse,
    Messages,
    Role,
    StreamChunk,
    StreamFinal,
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
        embedding_client: openai.OpenAI | None = None,
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
        # Второй клиент шлюза (эмбеддер, S06/D-025) — лениво при первом embed:
        # контуры без retrieval не строят соединение вовсе
        self._embed_client: openai.OpenAI | None = embedding_client
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
        model = self._resolve_model(role)
        request_kwargs = self._request_kwargs(messages, tools, model)
        started = self._clock()
        response = self._create_with_backoff(request_kwargs, role=role, started=started)
        return self._on_success(role, response, latency_s=self._clock() - started)

    def complete_stream(
        self,
        role: Role,
        messages: Messages,
        *,
        tools: Sequence[ToolSpec] | None = None,
    ) -> Iterator[StreamChunk | StreamFinal]:
        """Стрим той же роли (S19, стык 2 — см. контракт в порте).

        Guard'ы бюджета/breaker и backoff-цикл — общие с `complete()` (стрим
        расходует бюджет как обычный вызов, Task 2 плана); ретраи — только ДО
        первого чанка (backoff вокруг SDK-create), mid-stream сбой — без ретрая
        (частично напечатанный ответ ретраем дублируется, Ловушки-1).
        `stream_options={"include_usage": True}` — запрос usage у шлюза (ревью
        I1: без него OpenAI-совместимые шлюзы usage не шлют и кап бюджета в
        стрим-пути инертен; шлюз, не знающий параметр, может отвечать без
        usage — тогда usage_missing=true, стоимость 0 не маскируется).
        """
        self._guard_budget()
        self._guard_breaker()
        model = self._resolve_model(role)
        request_kwargs = self._request_kwargs(messages, tools, model)
        request_kwargs["stream"] = True
        request_kwargs["stream_options"] = {"include_usage": True}
        started = self._clock()
        stream = self._create_with_backoff(request_kwargs, role=role, started=started)
        return self._stream_response(role, stream, model=model, started=started)

    def _resolve_model(self, role: Role) -> str:
        model = self._models.get(role)
        if model is None:
            raise LLMProviderError(
                f"неизвестная роль модели: {role!r} (ожидаются cheap/mid/strong)"
            )
        return model

    def _request_kwargs(
        self,
        messages: Messages,
        tools: Sequence[ToolSpec] | None,
        model: str,
    ) -> dict[str, Any]:
        payload: list[dict[str, Any]] = [dict(m) for m in messages]
        request_kwargs: dict[str, Any] = {
            "model": model,
            "messages": payload,
            "temperature": 0.0,  # ADR-008: структурированные выходы — детерминизм
        }
        if tools:
            request_kwargs["tools"] = [dict(t) for t in tools]
        return request_kwargs

    def _create_with_backoff(
        self, request_kwargs: dict[str, Any], *, role: str, started: float
    ) -> Any:
        """SDK-create в backoff-цикле (ADR-007 §4.3) — общий путь буферного
        `complete()` и стрима (S19): 4 попытки, ретраятся только 429/5xx/сеть;
        таймаут — отказ без ретрая и без инкремента breaker (W2 S03); прочие 4xx —
        конфиг-ошибка без ретрая; возвращает ответ шлюза (буферный объект или
        стрим-итератор — S19; ошибки ИТЕРАЦИИ стрима мапит `_stream_response`).
        """
        last_error: Exception | None = None
        for attempt in range(_BACKOFF_ATTEMPTS):
            try:
                return self._client.chat.completions.create(**request_kwargs)
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

    def _stream_response(
        self, role: str, stream: Any, *, model: str, started: float
    ) -> Iterator[StreamChunk | StreamFinal]:
        """Итерация стрима шлюза (S19): контент-дельты наружу, reasoning — буфер
        с фолбэком одним куском по guard'у finish_reason (D-021-аналог, ruling
        W4 ARCH-GATE S19 — константа из порта). Тул-дельты накапливаются в
        полные `ToolCall` (склейка по index) и выходят списком в `StreamFinal.
        tool_calls` — зеркало буферного `complete()` (S25 feat-020), в stdout
        не печатаются. Mid-stream сбой — ретрая нет:
        `_register_failure` (breaker считает обрыв стрима сбоем шлюза) +
        `LLMRequestError`. usage — из терминального чанка шлюза, если он его
        шлёт; отсутствие фиксируется `usage_missing=true` в llm.complete
        (finding ARCH-GATE S19 — стоимость 0 не маскируется).

        ВАЖНО (ревью C1): при ошибке ИТЕРАЦИИ SDK пробрасывает СЫРЫЕ httpx-
        исключения (ReadError/ReadTimeout/RemoteProtocolError — openai оборачивает
        только SSE-error payload и ошибки первоначального send), поэтому транспорт
        ловится здесь — httpx.HTTPError + openai.APIError. Таймаут карвется
        ДО generic-ветки: W2 S03 — «breaker считает ошибки, не медлительность»
        (таймаут между чанками не инкрементит breaker)."""
        ttft_ms: int | None = None  # NFR-1: время до первого чанка
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_call_parts: dict[int, dict[str, str]] = {}  # index → {id, name, arguments}
        finish_reason: str | None = None
        raw_usage: Any = None
        actual_model = ""
        try:
            for event in stream:
                event_usage = getattr(event, "usage", None)
                if event_usage is not None:
                    raw_usage = event_usage
                choices = getattr(event, "choices", None) or []
                if not choices:
                    continue  # терминальный usage-чанк без выбора / служебные события
                if ttft_ms is None:
                    ttft_ms = round((self._clock() - started) * 1000)
                actual_model = getattr(event, "model", "") or actual_model
                delta = getattr(choices[0], "delta", None)
                text = getattr(delta, "content", None) or ""
                if text:
                    content_parts.append(text)
                    yield StreamChunk(text_delta=text)
                reasoning = getattr(delta, "reasoning_content", None) or ""
                if reasoning:
                    reasoning_parts.append(reasoning)  # буфер: в stdout не печатается
                for part in getattr(delta, "tool_calls", None) or []:
                    # Дельты тула (S25 feat-020): id/name — на первой дельте
                    # вызова, arguments — конкатенацией, склейка по index;
                    # в stdout не печатаются — StreamChunk остаётся текстовой
                    # дельтой (StreamToolCallDelta отвергнут canon-check)
                    index = getattr(part, "index", 0)
                    slot = tool_call_parts.setdefault(
                        index,
                        # Шлюз без id в дельтах (M7 ревью S25): синтез — строгие
                        # OpenAI-совместимые шлюзы отбивают пустой id в истории
                        # следующим ходом (HTTP 400); реальный id перезапишет
                        {"id": f"call-stream-{index}", "name": "", "arguments": ""},
                    )
                    if getattr(part, "id", None):
                        slot["id"] = part.id
                    fn = getattr(part, "function", None)
                    if fn is not None:
                        if getattr(fn, "name", None):
                            slot["name"] = fn.name
                        if getattr(fn, "arguments", None):
                            slot["arguments"] += fn.arguments
                if getattr(choices[0], "finish_reason", None):
                    finish_reason = choices[0].finish_reason
        except (httpx.TimeoutException, openai.APITimeoutError) as exc:
            # W2-паритет: таймаут ПОСЛЕ начала стрима (модель молчит между чанками
            # дольше llm_request_timeout_s) — медлительность, не сбой breaker. Оба
            # слоя: SDK может отдать и сырой httpx.ReadTimeout, и обёрнутый
            # APITimeoutError («Request timed out.») — регресс-тест фиксирует фактическую
            # форму (обёртку) и ловит обе (ревью C1 fix-волна)
            self._emit(
                "llm.timeout",
                role=role,
                elapsed_ms=round((self._clock() - started) * 1000),
                stream=True,
            )
            raise LLMRequestError(
                f"таймаут шлюза посреди стрима ({self._settings.llm_request_timeout_s}s)"
            ) from exc
        except (
            httpx.HTTPError,
            openai.APIError,
            json.JSONDecodeError,
            UnicodeDecodeError,
        ) as exc:
            # Обрыв соединения/чтения посреди стрима — СБОЙ шлюза: breaker считает
            # (W3); наружу типизированная ошибка — деградацию решает вызывающий.
            # Ревью-2 m1: SDK не оборачивает malformed SSE-кадр (json.JSONDecodeError
            # из sse.json()) — прокси, вставивший HTML в SSE-поток, идёт тем же путём;
            # UnicodeDecodeError — битые байты кадра. pydantic ValidationError (тоже
            # ValueError) сознательно НЕ ловим — это наш контракт-баг, не шлюз.
            self._register_failure()
            self._emit(
                "llm.reject",
                role=role,
                status_code=None,  # не HTTP-ответ — статус-кода нет (ревью-2 m5)
                reason="stream_abort",
                stream=True,
            )
            raise LLMRequestError(f"стрим оборван шлюзом: {exc}") from exc
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                close()

        text_from_reasoning = False
        # Паритет с буферным complete() (ревью M2): whitespace-only content —
        # «текста нет», reasoning-буфер становится ответом. Тул-ход без
        # finish_reason — исключение (M8 ревью S25): reasoning преамбулой на
        # экран не идёт, он вообще не ответ тул-хода (поле-расширение шлюза
        # без finish_reason — не повод обещанию StreamChunk «reasoning-дельты
        # в потоке не появляются» измениться); guard-константа не тронута
        if (
            not "".join(content_parts).strip()
            and not tool_call_parts
            and finish_reason not in NO_TEXT_FALLBACK_REASONS
        ):
            fallback = "".join(reasoning_parts).strip()
            if fallback:
                # Пустой content при живом reasoning — фолбэк одним чанком;
                # вызывающий печатает как обычную дельту (W4)
                text_from_reasoning = True
                yield StreamChunk(text_delta=fallback)
        usage, cost_rub = self._register_success_usage(role, raw_usage)
        self._emit(
            "llm.complete",
            role=role,
            model=actual_model or model,
            tokens_in=usage.tokens_in,
            tokens_out=usage.tokens_out,
            usage_missing=raw_usage is None,
            text_from_reasoning=text_from_reasoning,
            finish_reason=finish_reason,
            cost_rub=round(cost_rub, 6),
            latency_ms=round((self._clock() - started) * 1000),
            budget_used_rub=round(self._budget_used_rub, 6),
            stream=True,
            ttft_ms=ttft_ms,
        )
        tool_calls = [
            ToolCall(id=slot["id"], name=slot["name"], arguments=slot["arguments"])
            for _, slot in sorted(tool_call_parts.items())
        ]
        yield StreamFinal(
            usage=usage,
            finish_reason=finish_reason,
            model=actual_model or model,
            text_from_reasoning=text_from_reasoning,
            tool_calls=tool_calls,
        )

    def embed(self, texts: Sequence[str]) -> list[Embedding]:
        """Эмбеддинг батча текстов одним запросом (S06, model-card; D-025).

        Клиент эмбеддера — второй шлюз по `settings.embedding_base_url`, авторизация
        — общий `LLM_API_KEY` (D-025: отдельный секрет не вводится). Порядок векторов
        = порядок входа (данные сортируются по index).
        """
        if not texts:
            return []
        client = self._embedding_client()
        try:
            response = client.embeddings.create(
                model=self._settings.embedding_model,
                input=list(texts),
            )
        except openai.APIError as exc:
            raise LLMRequestError(f"шлюз эмбеддера отклонил запрос: {exc}") from exc
        data = sorted(response.data, key=lambda item: item.index)
        # Парность «текст ↔ вектор» — контракт стыка: неполный ответ шлюза
        # (пустой/урезанный data) — ошибка, а не молчаливая потеря (ревью M6;
        # иначе llm.embed([q])[0] в retrieval-контуре падал IndexError'ом)
        if len(data) != len(texts):
            raise LLMRequestError(
                f"шлюз эмбеддера вернул {len(data)} векторов на {len(texts)} текстов"
            )
        return [list(item.embedding) for item in data]

    # --- внутреннее ---

    def _embedding_client(self) -> openai.OpenAI:
        # Ленивая сборка второго клиента (см. __init__): те же правила, что у
        # основного — SDK-ретраи выключены, таймаут обязателен (S00 правило 9)
        if self._embed_client is None:
            self._embed_client = openai.OpenAI(
                base_url=self._settings.embedding_base_url,
                api_key=self._settings.llm_api_key.get_secret_value(),  # D-025
                max_retries=0,
                timeout=self._settings.llm_request_timeout_s,
            )
        return self._embed_client

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

    def _register_success_usage(self, role: str, raw_usage: Any) -> tuple[Usage, float]:
        """Учёт успешного вызова: usage → ₽ в копилку запроса + сброс счётчика
        сбоев (общий для буферного `complete()` и стрима, S19: стрим расходует
        бюджет как обычный вызов — Task 2 плана)."""
        usage = Usage(
            tokens_in=getattr(raw_usage, "prompt_tokens", 0) or 0,
            tokens_out=getattr(raw_usage, "completion_tokens", 0) or 0,
        )
        price_in, price_out = self._prices[role]
        cost_rub = usage.tokens_in / 1e6 * price_in + usage.tokens_out / 1e6 * price_out
        self._budget_used_rub += cost_rub
        self._consecutive_failures = 0
        self._probe_pending = False
        return usage, cost_rub

    def _on_success(self, role: str, response: Any, *, latency_s: float) -> LLMResponse:
        raw_usage = getattr(response, "usage", None)
        usage, cost_rub = self._register_success_usage(role, raw_usage)

        choice = response.choices[0]
        message = choice.message
        finish_reason = getattr(choice, "finish_reason", None)
        tool_calls = [
            ToolCall(id=call.id, name=call.function.name, arguments=call.function.arguments)
            for call in (message.tool_calls or [])
        ]
        # Пустой/whitespace-only content = «текста нет» (порт: str | None) —
        # иначе CLI печатает визуально пустой ответ вместо ретрая (ревью Minor-4).
        text = (message.content or "").strip() or None
        text_from_reasoning = False
        if text is None and not tool_calls and finish_reason not in NO_TEXT_FALLBACK_REASONS:
            # Любой шлюз может отдать финальный ответ в reasoning_content — ответ
            # внутри think-блока при пустом content (наблюдение 2026-11-19,
            # локальный qwen3.8-9b; облачные reasoning-модели — тот же риск).
            # Без фолбэка цикл видит «ни текста, ни тулов» и уходит в деградацию
            # с живым ответом в руке. Поле — расширение шлюза, читается getattr:
            # шлюз без него ведёт себя как прежде (text остаётся None). Guard по
            # finish_reason: обрезанный/отфильтрованный reasoning — не ответ
            # (ревью Important-1); provenance виден в трейсе (text_from_reasoning).
            text = (getattr(message, "reasoning_content", None) or "").strip() or None
            text_from_reasoning = text is not None
        llm_response = LLMResponse(
            text=text,
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
            text_from_reasoning=text_from_reasoning,  # ответ из think-поля, не из content
            finish_reason=finish_reason,
            cost_rub=round(cost_rub, 6),
            latency_ms=round(latency_s * 1000),
            budget_used_rub=round(self._budget_used_rub, 6),
        )
        return llm_response

# S19 — Стриминг объяснения в CLI (веха V2, ДЗ-03)

Этап: Веха V2 (ДЗ-03: стриминг) | Владелец: агент | Зависимости: S03, S06 | Оценка: 0.5 | Статус: ✅ 2026-11-20 (feat-019 passing, REVIEW-GATE «Ready to merge: Yes»)
Фичи: feat-019 — Стриминг объяснения (ДЗ-03); зависимости feat-003 и feat-006 уже в passing
Роль по фиче: закрывает feat-019 — финальная сессия, через make verify-feature

## ⚠️ Прочитай перед началом

Исполнитель: ИИ-агент. К моменту старта: S03 закрыта (провайдер: backoff, breaker, бюджет,
фолбэк reasoning_content — D-021), S06 закрыта (retrieval-контур: порт `VectorStore`,
bge-m3, фильтр прав). Сессия добавляет потоковый вывод ответа модели в CLI — контур ДЗ-03
«вопрос → retrieval → LLM → потоковый вывод» (hw-plan §3; стриминг interpret_result-класса
ответа БЕЗ полного interpret-узла — он появляется в FSM вех V4/V5).

**Граница скоупа (решение владельца 2026-11-20): CLI-only.** Streamlit-демо остаётся «после
базового контура» (канон S00, D-005) — для критерия ДЗ-03 (стриминг виден на видео/скрине)
терминала достаточно. Не расширять: без FastAPI/SSE (N1 — целевой контур, не сейчас), без FSM
(V4), без ретрай-логики поверх частично напечатанного ответа (см. Ловушки-1).

### Правила для исполнителя

1. Ветка `feat-019-<slug>` от `milestone/v2`, НЕ от `dev` (D-024); merge рабочей ветки —
   в `milestone/v2`.
2. Существующий `LLMProvider.complete()` не менять — тул-контур и будущие узлы остаются на
   нём; стриминг — новый метод порта, старые вызовы не трогает.
3. stdout — только ответ (D-022): чанки ответа печатаются в stdout, служебные этапы — в
   stderr через ConsoleTracer; `--quiet` глушит stderr, но НЕ ответ.
4. Бюджет и circuit breaker действуют и в стрим-пути — переиспользовать guard'ы провайдера,
   не обходить (иначе стрим — дыра в экономике ADR-007).
5. TTFT (время до первого чанка) — в трейс llm.*-события (NFR-1 ≤ 3s; FR-11: основа
   eval-сета). Имена новых событий — константы в порте `tracer` (как `agent.*`/`llm.*`).

## Предусловия

- [x] Сессия СТАРТОВАЛА (2026-11-20): статус в шапке переведён в «◐ в работе» и ARCH-GATE резолвлен ДО кода (canon-check architect ✅ либо обоснованный N/A) — старт-гейт (`guard-subagents.sh --phase start`, D-027) проверяет только стартованные (◐) сессии; «[ ] не начата» — план, гейт её скипает
- [ ] S03 закрыта: `uv run pytest src/tests/unit/test_llm_client.py -q` → green
- [ ] S06 закрыта: в `feature_list.json` у feat-006 state=passing с evidence (retrieval-контур работает)
- [ ] `make check` зелёный (baseline репозитория)
- [ ] feat-003 и feat-006 в state=passing (`feature_list.json`)
- [ ] feat-019 активирована в state=active до старта работ (verify-feature требует active — Ловушки-5)
- [x] LM Studio с загруженной моделью доступен (для layer-3 прогона сценария ДЗ-03) — qwen3.8-9b + bge-m3, прогон 2026-11-20 (hw-03/)

## Задачи

### Task 1: Порт — complete_stream

`src/text_to_sql_agent/application/ports/llm_provider.py`: новый метод `complete_stream(role,
messages, *, tools=None)` — итератор чанков + финальные метаданные (usage, finish_reason,
model). Форма возврата — на усмотрение исполнителя (генератор объектов-чанков с
терминальным элементом или итератор + финальный результат); контракт зафиксировать в
docstring. Роль — как в `complete()` (таблица ролей из Settings, ADR-007). Деградация
провайдера — те же классы ошибок (`LLMProviderError`), новых категорий не вводить.

**✅ Самопроверка:**

```bash
uv run python -c "from text_to_sql_agent.application.ports.llm_provider import LLMProvider" && uv run ruff check src
```

---

### Task 2: Адаптер — стриминг OpenAI SDK

`src/text_to_sql_agent/infrastructure/llm/openai_provider.py`: `stream=True` в SDK-вызове
(тот же клиент, что у `complete()`); бюджетный guard ДО вызова — тот же, что в `complete()`
(стрим расходует бюджет как обычный вызов); breaker-guard и учёт успеха/провала — те же
счётчики. TTFT: время до первого чанка — в трейс-событие. Reasoning-дельты — аналог D-021
для стрима: пустой `content` при живом reasoning в чанках — печатать как ответ только по
guard'у finish_reason; reasoning, оборванный `length`/`content_filter`, ответом не становится.

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_llm_client.py src/tests/unit/test_streaming.py -q   # green
```

---

### Task 3: CLI — потоковая печать

`src/apps/cli.py`: финальный ответ печатается чанками по мере генерации
(`print(chunk, end="", flush=True)`, после завершения — `\n`); до первого чанка служебные
этапы идут в stderr как раньше. Ошибка в середине стрима → деградация (S00 правило 8:
валидный исход с сообщением, не traceback). `--quiet` глушит ConsoleTracer (stderr), ответ
в stdout остаётся потоковым. Ретраи пустых ответов — до первого напечатанного чанка
(см. Ловушки-1).

**✅ Самопроверка:**

```bash
uv run python -m apps.cli "Тест стрима" | cat   # stdout — только ответ (целиком); этапы — в stderr
uv run python -m apps.cli --quiet "Тест стрима"  # ответ потоковый, служебных строк нет
```

---

### Task 4: Сценарий ДЗ-03 (layer-3, ручной прогон)

«Сколько новых клиентов из Германии пришло из рекламных кампаний в октябре?» — вопрос с
retrieval-контекстом из векторной памяти (сидованные данные S02) → стриминг ответа.
Прогон на локальном LM Studio (бесплатен, D-020). Фиксация для сдачи ДЗ-03 (hw-plan §3 —
«код + видео/скрин»): команда прогона + скрин/видео стрима — в `hw-03/` (конвенция ДЗ,
AGENTS.md шапка) или по указанию куратора; reproducbility: фиксированный вопрос + сиды.

**✅ Самопроверка:**

```bash
uv run python -m apps.cli "Сколько новых клиентов из Германии пришло из рекламных кампаний в октябре?"   # ответ печатается по мере генерации
```

---

### Task 5: Тесты + закрытие фичи

`src/tests/unit/test_streaming.py` (LLM мокается со `stream=True`, S00 правило 7): (a)
последовательность чанков собирается в полный ответ; (b) TTFT фиксируется в событии; (c)
ошибка mid-stream → деградация без traceback; (d) BudgetExceededError до вызова — стрим не
начинается; (e) reasoning-чанки не печатаются как ответ (guard по finish_reason); (f)
usage отсутствует в StreamFinal → событие llm.complete несёт `usage_missing=true`
(ARCH-GATE: finding canon-check — паритет «стоимость 0 не маскируется», S03 I4). Закрытие:
`make verify-feature F=feat-019`.

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_streaming.py -q   # green
make verify-feature F=feat-019                      # state=passing + evidence
```

## Критерии приёмки

- [x] Ответ модели печатается в терминал по мере генерации, chunk'ами (behavior feat-019) — `run_retrieval_flow` printer-цикл; live: 335 чанков за 14.4с (hw-03/stream-timeline.txt)
- [x] Сценарий ДЗ-03 («клиенты из Германии из рекламных кампаний») воспроизводится с сидами и retrieval-контекстом (behavior feat-019) — live-прогон u_logist: SQL с 'DE' из глоссария и NULL campaign_id из few-shot (hw-03/stream-stdout.txt, README — команда+сиды)
- [x] stdout чист: пайпинг `| cat` даёт только ответ; `--quiet` глушит stderr-этапы, не ответ (D-022) — live: stdout 1538 байт, stderr 0 байт при --quiet; регресс-тесты main (capsys)
- [x] TTFT стрим-вызова виден в трейсе (NFR-1) — llm.complete ttft_ms + stream=true (live: «первый чанк 5662мс» в stderr-рендере; JSONL-трейс)
- [x] Бюджет/breaker переиспользованы, new guard-кода в обход провайдера нет — guard'ы общие (_guard_budget/_guard_breaker), usage запрошен (stream_options), live usage 647→491; тест капа стримом
- [x] Фича закрыта: `make verify-feature F=feat-019` → state=passing с evidence — PASS (34 теста test_streaming.py)

## Ловушки

1. **Ретрай после печати:** повторная попытка после частично напечатанного ответа задублирует
   текст — ретраить (empty-retry, backoff) только ДО первого напечатанного чанка; после —
   только деградация с сообщением.
2. **reasoning_content в стриме** (D-021-аналог): qwen может слать think-дельты при пустом
   content — не печатать reasoning в stdout; финальный guard по finish_reason.
3. **Бюджет в стрим-пути:** стрим без guard'а — бесплатный обход капа 2 ₽; guard до вызова,
   CircuitOpenError/BudgetExceededError → деградация.
4. **stdout vs stderr:** ConsoleTracer пишет в stderr (D-022), но проверь `| cat`: если чанки
   дублируются в консоль-трейс — событие agent.answer шлёт текст ответа в payload; в консоль
   рендерить факт («✓ ответ (stream)»), не текст.
5. Strict-режим verify-feature отвергает причину N/A «сессия не начата» — гейты заполнить по
   факту работ.
6. Ветка от `milestone/v2`, не от `dev` (D-024).
7. Активируй feat-019 в первый же день (Предусловия-5), не перед коммитом.

## Гейты субагентов (заполняется по факту, до закрытия сессии)

- ARCH-GATE: ✅ canon-check «compliant» 2026-11-20 (architect; план соответствует канону: стык 2/S00, ADR-007 §4, ADR-002, D-021/D-022, observability §2.4) — rulings: W1 стриминг только в retrieval-контуре ДЗ-03 (`--retrieve`, один вызов, ответ — текст; тул-контур ДЗ-02 остаётся буферизованным — шапка плана + hw-plan §3, инвариант ADR-002 не затронут); W2 контракт `complete_stream` = генератор чанков (StreamChunk: text_delta) + терминальный StreamFinal (usage/finish_reason/model), TTFT — полем `ttft_ms` (+ `stream: true`) в СУЩЕСТВУЮЩЕМ событии llm.complete, НЕ новым именем (словарь observability §2.4 зафиксирован; прецедент D-021 — расширение полями; llm-имена не заводятся в tracer.py); W3 ретраи (backoff SDK-create, CLI empty-retry) только ДО первого чанка, mid-stream сбой → `_register_failure` + LLMRequestError → деградация (ADR-007 M1, S00 правило 8, Ловушки-1); W4 reasoning-дельты буферизуются в адаптере, stdout не попадают, фолбэк одним куском при пустом content и живом reasoning по guard'у finish_reason — константа переезжает в порт (NO_TEXT_FALLBACK_REASONS, единый источник, без дубля), provenance text_from_reasoning в payload; W5 синхрон S00 (строка порта + стык 2 + Правки) и observability §2.4 (поля ttft_ms/stream) в том же коммите; находка canon-check: паритет usage_missing в стриме (usage шлют не все шлюзы) → тест (f) в Task 5
- REVIEW-GATE: ✅ «Ready to merge: Yes» — итеративно: ревью-1 «Требует правок» (1C/3I/9M, d11d161..15aa770) → fix-волна (ec0fa76; C1 — httpx-транспорт итерации стрима мимо APIError: регресс на НАСТОЯЩЕМ SDK-клиенте с рвущимся транспортом; I1 stream_options include_usage; I2 ruling (b); I3 main-wiring тесты; 7 minors) → re-review-1 **«Ready to merge: Yes» (0C/0I; 6 новых minors)** → minors-волна (81be81a: m1 malformed SSE-кадр → JSONDecodeError/UnicodeDecodeError в generic-ветку + real-SDK регресс; m2 raw-httpx guard на _ScriptedStream — SDK-независимый; m5 status_code=None + reason=stream_abort; m3/m4 docstring-честность; m6 stream_options подтверждён live-прогоном — usage 647→491 терминальным чанком LM Studio, hw-03/stream-timeline.txt) — «остались только новые Minor-находки качества, ни одна не регрессирует поведение»; all minors закрыты в сессии; 234 passed

## Отчёт по сессии (заполняется по факту)

- Сделано:
  - feat-019 закрыта (verify-feature PASS): порт `LLMProvider.complete_stream` (StreamChunk/StreamFinal, `NO_TEXT_FALLBACK_REASONS` в порту), адаптер (общий backoff-цикл с `complete()`, ретраи только до первого чанка, TTFT `ttft_ms`+`stream=true` в существующем llm.complete, reasoning-фолбэк по finish_reason, `stream_options include_usage`), CLI — потоковая печать retrieval-контура (printer-инъекция, `_stream_outcome`, empty-retry до первой дельты, D-022); ConsoleTracer — «первый чанк Nмс»; коммиты d11d161 (старт) → 15aa770 (реализация) → ec0fa76 (fix-волна ревью-1) → 98be1d0 (hw-03 layer-3) → 81be81a (minors ревью-2)
  - ARCH-GATE ✅ canon-check «compliant» (rulings W1–W5 + находка usage-паритета → тест (f)); REVIEW-GATE ✅ «Ready to merge: Yes» (2 итерации: 1C/3I/9M → 0C/0I/6m, все minors закрыты в сессии)
  - Ревью-1 Critical (реальный): SDK при ошибке ИТЕРАЦИИ стрима пробрасывает сырые httpx-исключения мимо `openai.APIError` — до фикса traceback в CLI и breaker не считал; закрыто регрессом на НАСТОЯЩЕМ SDK-клиенте с рвущимся транспортом; попутная находка fix-волны — ReadTimeout SDK оборачивает в APITimeoutError (таймаут-караул по W2 ловит оба слоя)
  - 234 passed (34 unit в test_streaming.py); make check EXIT=0; layer-3: сценарий ДЗ-03 на LM Studio (stdout/stderr/timeline/--quiet артефакты в hw-03/, usage 647→491 — stream_options принят шлюзом)
  - Синхрон в коммитах кода: S00 (порт-таблица, стык 2, Правки 2026-11-20), observability.md §2.4 (retrieval_stream, ttft_ms/stream, stream_abort), ARCHITECTURE.md (порт-строка)
- Отклонения от плана:
  - httpx переведён из dev-группы в runtime-dependencies (pyproject) — import httpx в адаптере для транспорта-караула C1; отклонение осознанное (ревью-1 C1: без httpx-ловли правило 8 нарушалось на реальном пути; SDK уже тянет httpx транзитивно)
  - Форма возврата `complete_stream` зафиксирована решением W2 (генератор + StreamFinal) — план оставлял «на усмотрение», canon-check сузил до конкретики с полем ttft_ms в существующем событии (не новое имя события)
  - Hermes-тег в стрим-пути печатается на экран до распознания (решение по ревью I2, вариант (b): ретрейл-буфер отвергнут как усложнение печати; S13-канон соблюдается в agent.answer/трейсе; в retrieval тулов нет — риск умеренный) — задокументировано в docstring `_stream_outcome` + тест фактической последовательности
  - Тесты test_retrieval_flow.py переведены на стрим-контракт (фейк отдаёт дельты) — контракт флоу изменился осознанно (W1); проверки-интенты сохранены
- Перенесено в другие сессии:
  - feat-015/interpret (V4): `finish_reason="length"` с непустым текстом принимается как ответ (усечённость не помечается) — тот же слепой угол буферного пути; StreamFinal уже несёт finish_reason — interpret-узел решит incomplete-политику (ревью-2 M8)
  - feat-015/interpret (V4): response.text — whitespace-only content теряет фолбэк-различимость; в стриме согласовано с M2-паритетом
  - S15/BudgetGuard: в стрим-пути учёт бюджета завершается ПОСЛЕ конца стрима — отказ от генератора (abandon/Ctrl+C) не учитывает потраченные токены; задокументировано в порту (ревью-2 M7-решение: «вызов не завершён»)
  - BL (без файла — вес мал): NFR-1 TTFT 5.7с на локальной модели > порога 3с — SLO-калибровка порогов по замерам (выпускная сессия, observability §4)

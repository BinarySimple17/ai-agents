# ДЗ-03 «Подключение контекста и streaming» — RAG + потоковый ответ

Решение ДЗ-03 (конвенция: `hw-NN/` — артефакты сдачи; полная историческая
спецификация — `.docs/hw-03/` вне гита, план выполнения — `.docs/hw-plan.md` §3).
Веха V2, фичи feat-006 (векторная память, сессия S06) + feat-019 (стриминг, сессия S19).

## Что сдаём → где лежит

| Артефакт hw-plan §3 | Реализация |
|---|---|
| Источник контекста | `docs/glossary.md`, `docs/few_shot.md` (кураторские) + DDL из миграции 0001 и связи из `agent_relationships_active` → индексация `make index-knowledge` (`src/apps/index_knowledge.py`) в pgvector за портом `VectorStore` (`infrastructure/memory/pgvector_store.py`) |
| Подключение к агенту | retrieval в промпт вызова (≈2К токенов, кап по границе документа) — `run_retrieval_flow` в `src/apps/cli.py` (контур «вопрос → retrieval → LLM → стрим», без FSM — он в V4) |
| Streaming | `LLMProvider.complete_stream` (порт + адаптер, `stream=True` + `stream_options.include_usage`) → потоковая печать дельт в CLI по мере генерации |
| Сценарий | «Сколько новых клиентов из Германии пришло из рекламных кампаний в октябре?» — ниже, воспроизводим командой + сидами |
| Фиксация архитектуры | ADR-003 (pgvector — истина данных), ADR-005 (фильтр прав в SQL поиска — deny-by-default), выжимка ниже |

**Видео/скрин стриминга:** файлы `stream-timeline.txt` (таймлайн прибытия чанков
с метками времени), `stream-stdout.txt`/`stream-stderr.txt` (чистое разделение
stdout/stderr — D-022). Терминальная запись владельца — по месту сдачи
(`--retrieve` прогон визуально печатает ответ по мере генерации).

## Воспроизводимость (фиксированный вопрос + сиды)

```bash
make infra-up && make infra-migrate && make index-knowledge   # сиды контекста
uv run --env-file .env python -m apps.cli --retrieve --user u_logist \
  "Сколько новых клиентов из Германии пришло из рекламных кампаний в октябре?"
```

Локальный шлюз LM Studio (D-020, бесплатный прогон), модель `qwen3.8-9b`,
эмбеддер `text-embedding-bge-m3` (dim 1024).

## Доказательства прогона 2026-11-20 (Layer 3)

- **stdout** (`stream-stdout.txt`) — только ответ: SQL + пояснение (1538 байт);
  пайпинг `| cat` отдаёт ровно ответ (D-022).
- **stderr** (`stream-stderr.txt`) — служебные этапы: вопрос, `agent.retrieve`
  (5 документов: глоссарий «клиент из РФ», few-shot «выручка-по-кампаниям»,
  «заказы-из-РФ», «заказ вне кампании», ребро customers↔orders), вызов модели
  `647→491 ток · 14.7с · 0.00₽ · первый чанк 5662мс`, `✓ ответ (retrieval_stream)`.
- **Таймлайн чанков** (`stream-timeline.txt`) — 335 чанков за 14.4с: стрим виден
  по меткам `t=8.23s … t=14.44s`, терминальный `[FINAL] finish_reason=stop
  usage=647->491 model=qwen3.8-9b`.
- **`--quiet`** (`stream-quiet-stdout.txt`) — stderr 0 байт, stdout = ответ
  (потоковая печать не глушится).
- **usage в стриме** — `stream_options={"include_usage": true}`: LM Studio шлёт
  usage терминальным чанком → бюджет стрима учитывается (ADR-007, не «обход капа»);
  шлюз без поддержки параметра фиксируется `usage_missing=true` в трейсе.

## Качество ответа (retrieval-контекст работает)

Модель использовала знания из векторной памяти, а не общие представления:
код страны `'DE'` (глоссарий: `'Germany'`/`'RU'` ≠ кода в БД), семантика
`campaign_id IS NOT NULL` (few-shot «заказ вне кампании»: 33 заказа с NULL
campaign_id в демо-данных — INNER JOIN по campaigns молча отбросил бы их),
структура ответа «SQL + пояснение» из few-shot.

TTFT (первый чанк 5.7с) — время промпт-обработки локальной модели на скромном
железе; порог NFR-1 (≤ 3с) — SLO-цель (observability §4), замер транслируется
в `llm.complete.ttft_ms` — основа eval-набора (FR-11), калибровка порогов —
выпускная сессия.

## Архитектура (выжимка)

- **Данные знаний** — pgvector (`agent_knowledge`: embedding vector(1024), HNSW
  cosine, unique `source_ref`); четыре вида документов: DDL, связи, глоссарий,
  few-shot; `referenced_tables[]` — права проверяются по ВСЕМ таблицам документа
  (кросс-табличный документ скрыт целиком, ADR-005 deny-by-default).
- **Права** — фильтр джойном с `agent_user_roles`/`agent_permissions` в самом
  SQL поиска (не пост-фильтрация); чтение — роль `agent_planner`, запись —
  только офлайн-индексатор под сервисным DSN.
- **Стриминг** — порт `LLMProvider.complete_stream` (генератор
  `StreamChunk(text_delta)` + терминальный `StreamFinal(usage, finish_reason,
  model)`); guard'ы бюджета/circuit-breaker — те же, что у буферного `complete()`;
  ретраи — только до первого чанка (частично напечатанный ответ ретраем
  дублируется — дальше деградация-исход, S00 правило 8); reasoning-дельты модели
  (`<think>`) в stdout не попадают — фолбэком по guard'у finish_reason.
- **Маршрут не от LLM** — контур целиком код (`cli.py`), LLM наполняет содержимое
  (инвариант ADR-002); полный interpret-узел FSM — веха V4.
- Верификация фичи: `make verify-feature F=feat-019` (23 unit-теста
  `test_streaming.py`); полный набор — 231 passed.

# ДЗ-03. Подключение контекста и streaming: RAG + потоковый ответ

Веха V2, две фичи: [feat-006](../feature_list.json) на векторную память pgvector (сессия S06)
и feat-019 на стриминг ответа (сессия S19). Артефакты сдачи по плану выполнения (`hw-plan.md` §3);
полная историческая спецификация ДЗ лежит в `.docs/hw-03/`, вне гита.

## Что сдаём

| Артефакт hw-plan §3 | Где лежит |
|---|---|
| Источник контекста | [docs/glossary.md](../docs/glossary.md) и [docs/few_shot.md](../docs/few_shot.md) (кураторские) плюс DDL из миграции 0001 и связи из вьюхи `agent_relationships_active`. Индексация в pgvector: `make index-knowledge` ([src/apps/index_knowledge.py](../src/apps/index_knowledge.py)), хранилище за портом `VectorStore` ([pgvector_store.py](../src/text_to_sql_agent/infrastructure/memory/pgvector_store.py)) |
| Подключение к агенту | retrieval-блок в системном промпте, около 2К токенов с резкой только по границе документа: [run_retrieval_flow в src/apps/cli.py](../src/apps/cli.py) |
| Streaming | [complete_stream в порте LLMProvider](../src/text_to_sql_agent/application/ports/llm_provider.py), адаптер ходит в шлюз с `stream=True` и запрашивает usage; CLI печатает дельты по мере генерации |
| Сценарий | вопрос «Сколько новых клиентов из Германии пришло из рекламных кампаний в октябре?», воспроизводится командой ниже (фиксированный вопрос + сиды) |
| Фиксация архитектуры | [ADR-003](../docs/adr/ADR-003-single-postgres.md) (pgvector как истина данных), [ADR-005](../docs/adr/ADR-005-rbac-as-data.md) (права проверяются в SQL поиска), выжимка внизу страницы |

Запись стриминга для сдачи: [stream-timeline.txt](stream-timeline.txt) показывает
прибытие чанков с метками времени, [stream-stdout.txt](stream-stdout.txt) и
[stream-stderr.txt](stream-stderr.txt) демонстрируют разделение ответа и служебных
этапов по потокам (D-022). Терминальную запись живого прогона (видео или скрин)
снимаем по месту сдачи: при запуске с `--retrieve` ответ виден на экране по мере генерации.

## Как воспроизвести

```bash
make infra-up && make infra-migrate && make index-knowledge   # сиды контекста
uv run --env-file .env python -m apps.cli --retrieve --user u_logist \
  "Сколько новых клиентов из Германии пришло из рекламных кампаний в октябре?"
```

Шлюз локальный, LM Studio (D-020, прогон бесплатный): модель `qwen3.8-9b`,
эмбеддер `text-embedding-bge-m3` (1024).

## Доказательства прогона 2026-11-20

- [stream-stdout.txt](stream-stdout.txt) содержит только ответ: SQL с пояснением,
  1538 байт. Через пайп `| cat` наружу выходит ровно ответ и ничего больше (D-022).
- [stream-stderr.txt](stream-stderr.txt) показывает служебные этапы в stderr:
  вопрос, retrieval (5 документов: глоссарий «клиент из РФ», few-shot
  «выручка-по-кампаниям» и «заказы-из-РФ», глоссарий «заказ вне кампании»,
  ребро customers↔orders), итог вызова `647→491 ток · 14.7с · 0.00₽ · первый
  чанк 5662мс` и строка `✓ ответ (retrieval_stream)`.
- [stream-timeline.txt](stream-timeline.txt): 335 чанков за 14.4с с метками от
  `t=8.23s` до `t=14.44s`, в конце `[FINAL] finish_reason=stop usage=647->491
  model=qwen3.8-9b`.
- [stream-quiet-stdout.txt](stream-quiet-stdout.txt): при `--quiet` stderr пуст
  (0 байт), ответ печатается как обычно.

Usage в стриме запрошен параметром `stream_options={"include_usage": true}`:
LM Studio отдал его терминальным чанком, поэтому бюджет стрима считается как у
обычного вызова ([ADR-007](../docs/adr/ADR-007-model-cascade.md), «обхода капа» нет).
Шлюз без поддержки параметра был бы помечен `usage_missing=true` в трейсе,
нулевая стоимость не маскируется.

## Качество ответа: retrieval-контекст работает

Ответ собран из знаний векторной памяти, а не из общих представлений модели.
Код страны `'DE'` взят из глоссария (в БД коды, а не названия вроде `'Germany'` или
`'RU'`). Условие `campaign_id IS NOT NULL` пришло из few-shot «заказ вне кампании»:
в демо-данных 33 заказа с пустым `campaign_id`, и INNER JOIN по campaigns отбросил бы
их молча. Структура «SQL + пояснение» тоже из few-shot.

Первый чанк пришёл через 5,7с: это промпт-обработка локальной модели на скромном
железе. Порог NFR-1 (3с) — SLO-цель ([observability §4](../docs/observability.md));
замер пишется в `llm.complete.ttft_ms` и станет основой eval-набора (FR-11),
калибровка порогов после реализации и не на dev стенде.

## Архитектура (выжимка)

- Знания лежат в pgvector, таблица `agent_knowledge`: вектор 1024, HNSW по cosine,
  уникальный `source_ref`, четыре вида документов (DDL, связи, глоссарий, few-shot).
  У каждого документа есть `referenced_tables[]`, и права проверяются по всем
  таблицам сразу: кросс-табличный документ скрыт целиком
  ([ADR-005](../docs/adr/ADR-005-rbac-as-data.md), deny-by-default).
- Фильтр прав встроен джойном с `agent_user_roles`/`agent_permissions` в сам SQL
  поиска, без пост-фильтрации выдачи. Читает роль `agent_planner`, пишет только
  офлайн-индексатор под сервисным DSN.
- Стриминг: [complete_stream](../src/text_to_sql_agent/application/ports/llm_provider.py)
  отдаёт дельты и в конце терминальный StreamFinal с usage и finish_reason.
  Guard'ы бюджета и circuit-breaker те же, что у буферного `complete()`
  ([ADR-007](../docs/adr/ADR-007-model-cascade.md)). Ретраи возможны только до
  первого чанка: частично напечатанный ответ ретраем дублируется, дальше только
  деградация (правило 8 канона S00). Reasoning-дельты модели (`<think>`) в stdout
  не попадают; если content пуст, а reasoning жив, фолбэк по guard'у finish_reason
  отдаёт его как ответ одним чанком.
- Маршрут строит код ([src/apps/cli.py](../src/apps/cli.py)), модель наполняет
  содержимое (инвариант [ADR-002](../docs/adr/ADR-002-fsm-on-code.md)); полный
  interpret-узел FSM появится в вехе V4.
- Верификация: `make verify-feature F=feat-019`, 34 unit-теста в
  [src/tests/unit/test_streaming.py](../src/tests/unit/test_streaming.py);
  полный набор проекта: 234 passed.

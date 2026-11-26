# ДЗ-03. Подключение контекста и streaming: RAG + потоковый ответ

Веха V2. Основа — [feat-006](../feature_list.json) на векторную память pgvector
(сессия S06) и feat-019 на стриминг ответа (сессия S19); директива владельца
(S24, вариант B) объединила их с тул-контуром ДЗ-02 в **объединённый контур**
(feat-020, сессия S25): вопрос → retrieval-контекст → цикл ходов на
`complete_stream` → тул `run_sql` исполняет SQL → финальный ответ стримится.
Артефакты сдачи по плану выполнения (`hw-plan.md` §3); полная историческая
спецификация ДЗ лежит в `.docs/hw-03/`, вне гита.

## Что где лежит

| Артефакт hw-plan §3 | Где лежит |
|---|---|
| ВИДЕО | [видео консоли](https://disk.yandex.ru/i/xOO_zyT_XcJstQ) |
| Источник контекста | [docs/glossary.md](../docs/glossary.md) и [docs/few_shot.md](../docs/few_shot.md) (кураторские) плюс DDL из миграции 0001 и связи из вьюхи `agent_relationships_active`. Индексация в pgvector: `make index-knowledge` ([src/apps/index_knowledge.py](../src/apps/index_knowledge.py), 24 документа), хранилище за портом `VectorStore` ([pgvector_store.py](../src/text_to_sql_agent/infrastructure/memory/pgvector_store.py)) |
| Подключение к агенту | retrieval-блок в системный промпт тул-контура, ≈2К токенов с резкой только по границе документа: [run_agent_flow в src/apps/cli.py](../src/apps/cli.py) (retrieval недоступен → деградация контекста, прогон продолжается) |
| Streaming | [complete_stream в порте LLMProvider](../src/text_to_sql_agent/application/ports/llm_provider.py): дельты текста — в stdout по мере генерации, тул-вызовы модели собираются из дельт стрима в `StreamFinal.tool_calls` и исполняются ([RunSqlTool](../src/text_to_sql_agent/application/services/run_sql_tool.py), канал agent_ro с LIMIT/timeout/RLS — паритет ДЗ-02), Observation возвращается в историю, следующий ход |
| Сценарий | вопрос «Сколько клиентов из РФ пришло из рекламных кампаний всего?» — воспроизводится командой ниже (фиксированный вопрос + сиды; правильный ответ: 5). Вариант из исходного плана «новые клиенты из Германии в октябре» даёт честный 0: в сиде клиенты созданы в ноябре 2026 |
| Фиксация архитектуры | [ADR-003](../docs/adr/ADR-003-single-postgres.md) (pgvector как истина данных), [ADR-005](../docs/adr/ADR-005-rbac-as-data.md) (права проверяются в SQL поиска), [ADR-008](../docs/adr/ADR-008-structured-outputs-tools.md) (тулы параметром), выжимка внизу страницы |

Запись стриминга для сдачи: [stream-timeline.txt](stream-timeline.txt) показывает
прибытие чанков с метками времени (71 чанк ответа) и структуру ходов
(agent.retrieve → tool_call → observation → answer), [stream-stdout.txt](stream-stdout.txt)
и [stream-stderr.txt](stream-stderr.txt) демонстрируют разделение ответа и
служебных этапов по потокам (D-022). Терминальную запись живого прогона (видео
или скрин) снимаем по месту сдачи: ответ виден на экране по мере генерации.

## Как воспроизвести

```bash
make infra-up && make infra-migrate && make index-knowledge   # сиды контекста
uv run --env-file .env python -m apps.cli --user u_logist \
  "Сколько клиентов из РФ пришло из рекламных кампаний всего?"
```

Шлюз локальный, LM Studio (D-020, прогон бесплатный): модель `qwen3.8-9b`,
эмбеддер `text-embedding-bge-m3` (1024). Для потокового вывода в `.env` задан
`LLM_REASONING_EFFORT=none` — reasoning-модель с тула́ми кладёт финальный ответ
в `reasoning_content` (стрим деградировал бы до одного фолбэк-чанка, D-021/W4),
гашение thinking возвращает контент-стрим чанками; цена — небрежный SQL без
самопроверки — компенсирована guardrail'ом в системном промпте (считай
сущности `COUNT(DISTINCT <id>)`, каждый фильтр из вопроса — в WHERE).

## Доказательства прогона 2026-11-21

- [stream-stdout.txt](stream-stdout.txt) содержит только ответ: «**Ответ: 5
  клиентов из РФ…**» с SQL и результатом — реальное число из БД (323 байта).
  Через пайп `| cat` наружу выходит ровно ответ и ничего больше (D-022).
- [stream-stderr.txt](stream-stderr.txt) показывает структуру ходов в stderr:
  retrieval (список 8 документов — в [stream-timeline.txt](stream-timeline.txt)),
  ход 1 — вызов модели (1769→73 ток · первый чанк 252мс) → `run_sql: SELECT
  COUNT(DISTINCT c.customer_id)…` → Observation «5», ход 2 — стрим объяснения
  (первый чанк 173мс), `✓ ответ (agent_stream)`.
- [stream-timeline.txt](stream-timeline.txt): 71 чанк за 2.5с с метками
  времени, события этапов (agent.start/retrieve/tool_call/observation/answer),
  полный SQL с обоими фильтрами (stderr режет его на 100 символах — лимит
  консольного рендера S21).
- [stream-quiet-stdout.txt](stream-quiet-stdout.txt): при `--quiet` stderr пуст
  (0 байт), ответ печатается как обычно.

Usage в стриме запрошен параметром `stream_options={"include_usage": true}`:
LM Studio отдал его терминальным чанком, поэтому бюджет стрима считается как у
обычного вызова ([ADR-007](../docs/adr/ADR-007-model-cascade.md), «обхода капа»
нет); шлюз без поддержки параметра был бы помечен `usage_missing=true`.

## Качество ответа: retrieval-контекст работает

Выполненный SQL (полная версия — в [stream-timeline.txt](stream-timeline.txt)):
`SELECT COUNT(DISTINCT c.customer_id) … WHERE c.country = 'RU' AND
o.campaign_id IS NOT NULL`. Код страны — из глоссария, фильтр кампании —
из few-shot «заказ вне кампании» (INNER JOIN-ловушка: без него заказы вне
кампаний учитывались бы молча), счёт уникальных сущностей — guardrail промпта
(S25). Число 5 независимо подтверждено журналом S24 (верификация находки #2,
тул-контур без retrieval).

Reasoning-канал: с гашением thinking модель стримит финальный ответ чанками;
без него (reasoning on) тот же вопрос отвечает верно, но финал приходит
одним фолбэк-чанком из `reasoning_content` — see `LLM_REASONING_EFFORT` в
[.env.example](../.env.example).

## Архитектура

```text
вопрос
  │
  ▼
retrieval: embed → pgvector-поиск с фильтром прав (ADR-005, deny-by-default)
  │         top_k=8 · кап ≈2К токенов · резка только по границе документа
  ▼
system prompt = база тул-контура (схема · справочники · guardrail) + контекст знаний
  │
  ▼
┌─ цикл ходов — кап 3 тул-хода (ruling ARCH-GATE S13 Q5) ────────────────────────┐
│                                                                                │
│ complete_stream ──┬─► дельты текста ──────► stdout по мере генерации (D-022)   │
│                   │                                                            │
│                   └─► StreamFinal.tool_calls (склейка тул-дельт)               │
│                             │                                                  │
│                             ▼                                                  │
│                   RunSqlTool — agent_ro (LIMIT 1000 · timeout 30s · RLS)       │
│                             │                                                  │
│                             ▼                                                  │
│                   Observation (ДАННЫЕ) ──► история ──► следующий ход           │
│                                                                                │
│ преамбула тул-хода остаётся на экране (ruling 3); кап исчерпан → финальный     │
│ стрим-вызов БЕЗ тулов (final_no_tools)                                         │
└────────────────────────────────────────────────────────────────────────────────┘
  │
  ▼
финальный ответ стримом (agent_stream); деградации — поверх напечатанного (правило 8)
```

- Знания лежат в pgvector, таблица `agent_knowledge`: вектор 1024, HNSW по
  cosine, уникальный `source_ref`, четыре вида документов (DDL, связи,
  глоссарий, few-shot). У каждого документа есть `referenced_tables[]`, и права
  проверяются по всем таблицам сразу: кросс-табличный документ скрыт целиком
  ([ADR-005](../docs/adr/ADR-005-rbac-as-data.md), deny-by-default). Фильтр прав
  встроен джойном с `agent_user_roles`/`agent_permissions` в сам SQL поиска.
- Объединённый контур ([run_agent_flow](../src/apps/cli.py)): retrieval-блок в
  системный промпт тул-контура → цикл ходов на `complete_stream` — дельты →
  stdout, `StreamFinal.tool_calls` → исполнение `RunSqlTool` (тот же канал
  agent_ro: LIMIT 1000, timeout 30s, RLS) → Observation в историю → следующий
  ход; кап 3 тул-хода → финальный вызов без тулов. Ход «преамбула + тул-вызов»
  в одном финале — преамбула остаётся на экране, ход исполняется (ruling S25).
  Guard'ы бюджета/breaker — те же, что у буферного `complete()`
  ([ADR-007](../docs/adr/ADR-007-model-cascade.md)); ретраи возможны только до
  первого чанка (правило 8 канона S00). Reasoning-дельты в stdout не попадают;
  если content пуст, а reasoning жив, фолбэк по guard'у finish_reason отдаёт
  его как ответ одним чанком.
- Маршрут строит код ([src/apps/cli.py](../src/apps/cli.py)), модель наполняет
  содержимое (инвариант [ADR-002](../docs/adr/ADR-002-fsm-on-code.md): модель
  выбирает тул, не следующий узел); полный interpret-узел FSM — в вехе V4.
- Верификация: `make verify-feature F=feat-020`, 24 unit-теста контура в
  [src/tests/unit/test_agent_flow.py](../src/tests/unit/test_agent_flow.py);
  полный набор проекта: 259 passed.

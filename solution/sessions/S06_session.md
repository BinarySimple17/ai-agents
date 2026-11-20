# S06 — Векторная память pgvector + фильтр прав (веха V2, ДЗ-03)

Этап: Веха V2 (ДЗ-03: RAG) | Владелец: агент | Зависимости: S02 | Оценка: 1.5 | Статус: ✅ 2026-11-20
Фичи: feat-006 — Векторная память (pgvector); её зависимость feat-002 уже в passing
Роль по фиче: закрывает feat-006 — финальная сессия, через make verify-feature

## ⚠️ Прочитай перед началом

Исполнитель: ИИ-агент. К моменту старта: S02 закрыта — инфра поднята (postgres
`pgvector/pgvector:0.8.0-pg17`, миграции схемы v1.5, роли; расширение `vector` уже создаётся
миграцией 0001), CLI-контур ДЗ-02 работает (S03/S13: тул run_sql, ConsoleTracer). Эта сессия
строит RAG-контур ДЗ-03 (hw-plan §3): индексация знаний в pgvector за портом `VectorStore`
(ADR-003) + retrieval с фильтром прав (ADR-005) + подключение контекста в промпт (~2К токенов).
Поиск — **dense-only** (решение владельца 2026-11-20: «простой источник контекста — dense из
7 табличек»): корпус курируемый (~30 документов), dense этого достаточно; вся механика
качества (гибрид FTS+RRF, шумовой корпус, нормализация, hit@5) — веха V3, BL-007.
Интерфейс — CLI (D-005).

Границы скоупа: БЕЗ Neo4j (S07, веха V3), БЕЗ FSM-узлов (V4), БЕЗ Streamlit (после базового
контура, канон S00). Retrieval — **dense-only**: БЕЗ гибридной FTS-ноги, БЕЗ шумового
корпуса, БЕЗ замерной обвязки (hit@5), БЕЗ реранка top-k — вся эта механика перенесена во
веху V3 «Гибридная память» (BL-007; D-026 с дополнениями остаётся её каноном). Эмбеддер — локальный bge-m3 через LM Studio (решение владельца
2026-11-20; MiniLM отвергнут — англоязычный-only при русском корпусе); endpoint эмбеддера —
по паттерну D-020/D-025: `EMBEDDING_BASE_URL` пустой → `http://127.0.0.1:1234/v1`.

### Правила для исполнителя

1. Ветка `feat-006-<slug>` от `milestone/v2`, НЕ от `dev` (D-024: веха V2 изолирована до
   приёмки V1); merge рабочей ветки — в `milestone/v2`.
2. Retrieval deny-by-default (ADR-005 «граница знаний = граница прав»; NFR-4 ACL-leak = 0):
   фильтр прав — в SQL запроса поиска (джойн с правами), не пост-фильтрация выдачи.
3. Схема БД — только новой миграцией в `src/migrations/versions/` (правка применённой
   запрещена, S00 правило 5); sync `docs/diagrams/er-data-model.md` в том же коммите
   (AGENTS.md § Escalation, Data changes).
4. few-shot и глоссарий — кураторские данные (ADR-003 §4): автонаполнение «успешным по
   трейсу» запрещено; содержимое — файлы `docs/glossary.md`, `docs/few_shot.md` (hw-plan §3).
5. Эмбеддер: bge-m3, размерность **1024** — именованная константа с комментарием «смена
   эмбеддера = переиндексация» (model-card); id модели сверить с живым `GET /v1/models`
   (фактический id в LM Studio — `gpustack/bge-m3-GGUF`, см. Ловушки-1).

## Предусловия

- [x] Сессия СТАРТУЕТ: статус в шапке переведён в «◐ в работе» и ARCH-GATE резолвлен ДО кода (canon-check architect ✅ либо обоснованный N/A) — старт-гейт (`guard-subagents.sh --phase start`, D-027) проверяет только стартованные (◐) сессии; «[ ] не начата» — план, гейт её скипает
- [x] S02 закрыта: `make infra-up && make infra-migrate` → схема v1.5 поднята; `uv run pytest src/tests/integration/test_infra_schema.py -q` → passed (включая test_vector_extension_installed)
- [x] `make check` зелёный (baseline репозитория)
- [x] feat-002 в state=passing (`feature_list.json`)
- [x] LM Studio отвечает, эмбеддер доступен: `curl -s http://127.0.0.1:1234/v1/models` → в списке есть bge-m3 (хост 127.0.0.1, не localhost — IPv6-ловушка, D-020)
- [x] feat-006 активирована в state=active до старта работ (verify-feature требует active — Ловушки-2)

## Задачи

### Task 1: Settings — endpoint эмбеддера по паттерну D-020 (D-025)

`src/text_to_sql_agent/config/settings.py`: поле `embedding_base_url: str = ""` — пустое →
локальный `http://127.0.0.1:1234/v1`; валидатор тот же, что у `llm_base_url` (хвостовой «/»
срезается, хост обязателен). Авторизация эмбеддера — существующий `LLM_API_KEY`
(отдельную секретную переменную не вводить — D-025). `.env.example` += `EMBEDDING_BASE_URL=`
с комментарием (пусто = локальный LM Studio; облако routerai — `qwen/qwen3-embedding-8b`).
Тесты в `src/tests/unit/test_settings.py` по образцу D-020-тестов: пустой → дефолт; хвост «/»;
пустой хост → ошибка валидации.

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_settings.py -q   # green, новые тесты endpoint'а эмбеддера в наборе
```

---

### Task 2: Порт VectorStore

`src/text_to_sql_agent/application/ports/vector_store.py`: Protocol `VectorStore` (runtime
checkable, по образцу соседних портов) — поиск `search(query_embedding, *, ctx, top_k)` и
индексация/upsert документов. Константа `EMBEDDING_DIM = 1024` (bge-m3; комментарий «смена
эмбеддера = переиндексация», model-card §1). Типы документов — ровно 4 из feat-006: DDL с
комментариями колонок, связи с семантикой (meaning/usage_notes), бизнес-глоссарий, few-shot
«вопрос → SQL`. Scope берётся из `RunContext` (user_id → роль → права) — как в остальных
портах. Ruling N1 (ARCH-GATE): сигнатура `search(query_embedding, *, ctx, top_k)` —
синк строки порта в S00 тем же коммитом (анти-дрейф, S00 — источник истины №1).

**✅ Самопроверка:**

```bash
uv run python -c "from text_to_sql_agent.application.ports.vector_store import VectorStore, EMBEDDING_DIM; assert EMBEDDING_DIM == 1024"
uv run ruff check src
```

---

### Task 3: Миграция таблицы векторной памяти

Новый файл `src/migrations/versions/20261120_0005_vector_memory.py` (дата/номер — по факту;
down_revision = текущий head, `0004_rls_sensitive_tables`): таблица знаний (имя на усмотрение
исполнителя, напр. `agent_knowledge`) — `id`, `doc_kind` (ddl | relationship | glossary |
few_shot), `table_name` (nullable: null = общий документ, не привязан к таблице), `content`,
`embedding vector(1024)`, `model_version`, `source_ref` (для идемпотентного upsert),
timestamps. HNSW-индекс по embedding (cosine ops). Комментарии колонок — по образцу миграции
0001. Sync `docs/diagrams/er-data-model.md` в том же коммите. Применение — только
`make infra-migrate`.

**✅ Самопроверка:**

```bash
make infra-migrate && uv run pytest src/tests/integration/test_infra_schema.py -q   # миграция применена, повторный апгрейд — no-op
```

---

### Task 4: Эмбеддер — реализация OpenAIProvider.embed

`src/text_to_sql_agent/infrastructure/llm/openai_provider.py`: заменить заглушку `embed()`
(NotImplementedError, ~строка 233) на реализацию: второй OpenAI-клиент на
`settings.embedding_base_url` (валидация ленивая — клиент строится при первом вызове, чтобы
не ломать контуры без эмбеддера), endpoint `/v1/embeddings`, модель `settings.embedding_model`,
Authorization — `LLM_API_KEY`. Батч-вызов списком текстов одним запросом. Тесты с моком SDK
(S00 правило 7): форма ответа → list[float]; пустой список на входе; ошибка шлюза →
`LLMRequestError`. Пин-тест `test_embed_declared_but_not_implemented_yet` в
`test_llm_client.py` заменить (он фиксирует старое поведение).

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_llm_client.py -q   # green без старого пин-теста, с новыми embed-тестами
```

---

### Task 5: Адаптер pgvector_store — поиск с фильтром прав

`src/text_to_sql_agent/infrastructure/memory/pgvector_store.py` (пакет `memory/` пуст и ждёт
именно этого): `PgVectorStore` — реализация порта. Поиск: cosine distance `<=>` — dense-only
(решение владельца 2026-11-20, D-026 дополнение 2; расширение до гибрида — V3, BL-007: порт
уже абстрагирует способ поиска), фильтр прав — ДЖОЙН с
`agent_user_roles`/`agent_permissions` в том же SQL (ADR-003 §4: «метадата-фильтр по scope
можно делать джойном с правами»), deny-by-default: нет прав на таблицу документа → документ не
участвует в выдаче. Подключение — **DSN роли `agent_planner`** (ruling W1, ARCH-GATE: c4-l2
v1.8 «пул agent_planner: retrieval, настройки, EXPLAIN»; Settings += property
`agent_planner_dsn` из POSTGRES_DSN + AGENT_PLANNER_PASSWORD; миграция 0005 += GRANT SELECT
на agent_knowledge этой роли — гранты на права-таблицы уже есть из 0002). Unit-тесты на моках
курсора: SQL содержит джойн прав; документы без прав не возвращаются.

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit -q   # green: unit на PgVectorStore в наборе
uv run ruff check src
```

---

### Task 6: Индексация сидов — 4 типа документов

Источники (hw-plan §3 + ADR-003): (1) DDL 7 бизнес-таблиц с комментариями колонок — из
миграции 0001 / `docs/diagrams/er-data-model.md`; (2) связи с семантикой (meaning/usage_notes)
— из `agent_relationships` (active-рёбра); (3) бизнес-глоссарий — создать
`docs/glossary.md` («выручка» = `SUM(orders.total)`, FR-3; термины демо-схемы); (4) few-shot
«вопрос → SQL» — создать `docs/few_shot.md` (3–5 примеров, кураторские, ADR-003 §4).
Каждый чанк начинается префиксом-заголовком («[таблица: orders] …», «[термин: выручка] …») —
сигнал для эмбеддера и якорь имени таблицы/термина (ACCURACY §6.2); единственный «рычаг
качества», оставленный в V2 — он копеечный и dense-родной. Шумовой корпус, нормализация
U+0301 и hit@5-обвязка — перенесены в V3 (BL-007).
Модуль индексации — в infrastructure/memory (метод upsert адаптера) + точка запуска
(ruling N2, ARCH-GATE): `src/apps/index_knowledge.py` + make-цель `make index-knowledge`;
идемпотентно по `source_ref`; `model_version` — из Settings. Внимание: правки в `docs/` —
той же сессией, в том же коммите (владение «docs/** — через согласование с куратором» —
содержимое согласовано hw-plan §3).

**✅ Самопроверка:**

```bash
make infra-up && make index-knowledge   # отчёт: сколько документов каждого типа проиндексировано
```

---

### Task 7: Retrieval в контур CLI + интеграционные тесты

CLI-контур ДЗ-03 «вопрос → retrieval → LLM → ответ» (hw-plan §3): перед вызовом модели —
эмбеддинг вопроса + `search()` + сборка retrieval-блока в системный промпт (~2К токенов —
именованная константа с комментарием «почему такое значение»; AGENTS.md § Working Rules);
wiring — поле в `Container` (`composition/container.py`). Identity — ruling W2 (ARCH-GATE):
CLI-параметр `--user` (дефолт cli-dev — контур ДЗ-02 не меняется; retrieval-прогон ДЗ-03 —
`--user u_analyst` и др. сидированные); интеграционные тесты — сидированные
u_logist/u_hr/u_analyst. Интеграционные тесты
`src/tests/integration/test_vector_memory.py` (команда верификации фичи):
(a) поиск возвращает только документы разрешённых таблиц; (b) в выдаче есть глоссарий и
few-shot релевантных таблиц; (c) недоступная таблица не попадает в выдачу даже при близком
совпадении эмбеддинга (ACL-leak = 0).

**✅ Самопроверка:**

```bash
make infra-up && uv run pytest src/tests/integration/test_vector_memory.py -q   # green (верификация фичи)
```

## Критерии приёмки

- [x] Поиск по эмбеддингу возвращает только документы разрешённых таблиц (behavior feat-006; интеграционные тесты + live u_analyst/u_hr)
- [x] В выдачу входят глоссарий-определения и few-shot для релевантных таблиц (behavior feat-006; test_glossary_and_few_shot_surface_for_relevant_tables + live прогон)
- [x] Недоступные таблицы не попадают в контекст даже при близком совпадении (behavior feat-006; NFR-4 ACL-leak = 0; test_denied_table_never_surfaces_even_with_exact_match + кросс-табличный регресс C1 + живой корпус)
- [x] `EMBEDDING_BASE_URL` пустой → локальный LM Studio (D-025); тесты Settings green (22 passed)
- [x] Миграции 0005/0006/0007 + er-data-model v1.6→v1.7 синхронизированы в тех же коммитах
- [x] Фича закрыта: `make verify-feature F=feat-006` → PASS, state=passing (8 integration tests, evidence в feature_list.json)

## Ловушки

1. **id модели:** в LM Studio фактический id — `gpustack/bge-m3-GGUF` (publisher/name), а в
   `.env.example` — `EMBEDDING_MODEL=text-embedding-bge-m3`. Перед индексацией сверить с
   `GET /v1/models` живого шлюза и выставить фактический id в `.env` — иначе 404 или молча
   другая модель.
2. verify-feature требует state=active: активируй feat-006 в первый же день (Предусловия-5),
   не перед коммитом.
3. Strict-режим verify-feature отвергает причину N/A «сессия не начата» (guard-subagents
   GATE_STRICT) — к закрытию заменить обе строки гейтов на evidence/фактическую причину.
4. Гвард auto-pick (start-фаза make check) смотрит **S19** (sort -V), пока оба файла сессий
   в корне: ARCH-GATE S19 заполняется при старте S19, не «чинить» его из S06.
5. Хост 127.0.0.1, не localhost (Windows: `::1` резолвится первым и молча дропается —
   D-020, S02 DSN-правило).
6. БД-тесты скипаются без `make infra-up` — `make check` зелёный без Docker, но
   verify-feature feat-006 требует поднятую инфру (layer-2 команды фичи).
7. Правка применённой миграции запрещена — таблица знаний = новый файл миграции (S00
   правило 5).
8. Вся «прокачка retrieval» — НЕ в этой сессии и уже записана в backlog (**BL-007**: гибрид
   dense+FTS со взвешенным RRF α_dense=0.9, шумовой корпус-дистрактор, нормализация U+0301,
   hit@5-базлайн, реранкер Qwen3-Reranker-0.6B) — веха V3 «Гибридная память», решение
   владельца 2026-11-20 «dense из 7 табличек на V2». Не тянуть в S06 ни один из пунктов.

## Гейты субагентов (заполняется по факту, до закрытия сессии)

- RULING (ревью-2 I4, 2026-11-20): comment-only правка ПРИМЕНЁННОЙ миграции 0005 (длина строки комментария, E501) — допустимое исключение: SQL-комментарии PG не сохраняет, схема на чистой БД идемпотентна (намерение правила S00-5 — пересборка из цепочки — не нарушено); впредь длину строки чинить ДО make infra-migrate. Дальнейшие изменения миграций 0005/0006 — только новыми файлами (0007). Функциональные правки применённых миграций по-прежнему запрещены.
- ARCH-GATE: ✅ canon-check architect 2026-11-20 (needs-decision → rulings до кода, agent_7fe43161): **W1 канал retrieval** — по канону c4-l2 v1.8 («пул agent_planner: retrieval»), не сервисный DSN: Settings += property `agent_planner_dsn` (host/port/db из POSTGRES_DSN + AGENT_PLANNER_PASSWORD), миграция 0005 += `GRANT SELECT ON agent_knowledge TO agent_planner` (план грант не заявлял вовсе — обязателен при любом не-суперюзерском раскладе), PgVectorStore подключается planner-DSN; **W2 identity CLI** — параметр `--user` (дефолт cli-dev — поведение контура ДЗ-02 не меняется), интеграционные тесты — сидированные u_logist/u_hr/u_analyst; статический DDL всех 7 таблиц в SYSTEM_PROMPT остаётся в dev-контуре осознанно (граница прав в CLI — SQL-фильтр retrieval + валидатор S13; промпт-осознанность — V4 resolve_access); **N1** сигнатура порта `search(query_embedding, *, ctx, top_k)` (по образцу ADR-011-ctx, точнее канона) — синк строки порта в S00 тем же коммитом, что Task 2; **N2** точка входа индексации — `src/apps/index_knowledge.py` + make-цель (прецедент cli.py, раскладка S00). Не блокеры: имя agent_knowledge под agent_* — deny-list ADR-005 про исполняемый LLM-SQL, retrieval-SQL пишет код; размерность облачного qwen3-embedding ≠ 1024 — покрыто D-025 (смена эмбеддера = переиндексация)
- REVIEW-GATE: ✅ «Принято» — итеративно: ревью-1 «Требует правок» (1C/7I/9M, d96163d..e86bb4f) → fix-волна (d5db296: C1 referenced_tables/0006, I1-I6, minors, BL-009); re-review-1 «Требует правок» (0C/4I/7m) → fix-волна-2 (c3d2ef2: миграция 0007 content-backfill, test_knowledge_sources, преамбулы md, ruling D-028, minors); re-review-2 **«Ready to merge: Yes» (0C/0I/9M)**, все 9 minors закрыты в той же сессии (c3d2ef2..HEAD: негативный тест валидатора, guard-call тест, content-scan BUSINESS_TABLES, pre/post backfill, D-028-зеркало, er/tracer синки). Полный диапазон сессии: d96163d..HEAD; make check 197 passed (161 unit + 36 integration)

## Отчёт по сессии (заполняется по факту)

- Сделано:
  - [x] feat-006 закрыта (state=passing): Task 1 — Settings.embedding_base_url (D-020/D-025, общий валидатор DRY; 6f44ba3); Task 2 — порт VectorStore + EMBEDDING_DIM=1024 + синк S00/N1 (81b2793); Task 3 — миграция 0005 agent_knowledge (vector(1024), HNSW cosine, unique source_ref, GRANT SELECT agent_planner — ruling W1) + er-data-model v1.6 (440c69b); Task 4 — OpenAIProvider.embed (ленивый второй клиент, батч) + Settings.agent_planner_dsn (fa4e02f); Task 5 — PgVectorStore: dense cosine с джойном прав В SQL (deny-by-default), 10 unit на моках курсора (9be9024); Task 6 — сиды 4 типов (glossary.md/few_shot.md кураторские), knowledge_sources.py, apps/index_knowledge.py + make index-knowledge, vector_store в Container; 22 док-а, идемпотентно (f3bee82); Task 7 — retrieval в CLI (--retrieve/--user — ruling W2), блок ≈2К токенов по границе документа, agent.retrieve, интеграционные behavior-тесты (e86bb4f). Живые прогоны: index-knowledge 22 док-а; CLI u_logist — сценарий ДЗ-03 («клиенты из Германии из кампаний»: модель использует глоссарий 'DE'≠'Germany' и семантику LEFT JOIN); u_hr — честный отказ без чужой схемы; u_analyst после C1 — ответ без таблицы campaigns (фикс подтверждён end-to-end)
  - [x] Fix-волны ревью: ревью-1 (1C/7I/9M) → C1 — права по ВСЕМ таблицам документа: миграции 0006 (referenced_tables) + 0007 (content-backfill), guard индексатора, санитизация FK в DDL-контенте, md-заголовки «таблицы A, B»; I2 — write_dsn только офлайн-индексатору (knowledge_writer); I3 — VectorStoreError; I4 — data-marking; I5-I7 — observability-каталог, канонные таймауты, test_knowledge_sources; minors M2-M8; BL-009 (M1/M3); D-028 (ruling comment-only правки применённой миграции); регресс-тесты C1 + живой корпус; итог 197 passed (161 unit + 36 integration)
- Отклонения от плана:
  - Канал retrieval — ruling W1 (ARCH-GATE): не сервисный POSTGRES_DSN, а роль agent_planner (c4-l2 v1.8 «пул agent_planner: retrieval»); запись — сервисный DSN офлайн-индексатором (флаг knowledge_writer контейнера — ревью I2)
  - Кросс-табличные документы (ревью C1): план предполагал фильтр по table_name-якорю; фактически инвариант ACL-leak=0 потребовал referenced_tables (все таблицы документа) — миграция 0006+0007, guard, санитизация DDL; relationship-доки несут обе стороны ребра
  - Идентификация индексатора: relationship-доки привязаны к left_table как якорю (фильтр — по обеим); точка входа — src/apps/index_knowledge.py (ruling N2); DDL-контент читается из файла миграции 0001 (PG-каталог inline-комментарии не хранит), REFERENCES-фрагменты вырезаются
  - Фактический id эмбеддера живого шлюза — text-embedding-bge-m3 (ловушка-1 предполагала gpustack/bge-m3-GGUF — сверка с GET /v1/models обязательна)
- Перенесено в другие сессии:
  - BL-009 (solution/backlog/) — гигиена индексации: SoT списка бизнес-таблиц (M1), транзакционное удаление устаревших source_ref / смена эмбеддера (M3) — веха V3, BL-007
  - Гибрид dense+FTS/RRF/шумовой корпус/hit@5/реранкер — BL-007 (веха V3, D-026 доп.2)

# S26 — Гибридная память: dense+FTS (взвешенный RRF) + гигиена индексации (веха V3, BL-007/BL-009)

Этап: Веха V3 (ДЗ-04: усиление векторной половины памяти — решение владельца, D-026 доп.2) | Владелец: агент | Зависимости: S06 | Оценка: 1.5 | Статус: ✅ 2026-11-21
Фичи: feat-021 — Гибридная память: dense+FTS (взвешенный RRF) + гигиена индексации; её зависимость feat-006 уже в passing
Роль по фиче: закрывает feat-021 — финальная сессия, через make verify-feature

## ⚠️ Прочитай перед началом

Исполнитель: ИИ-агент. К моменту старта: S06 закрыта — retrieval dense-only по ~30
курируемым документам (bge-m3 1024, префикс-заголовки чанков уже есть), S24 подняла
top_k до 8. Эта сессия переносит в код весь «набор рычагов качества», отложенный из V2
решением владельца 2026-11-20 (D-026 дополнение 2 → BL-007): ключевая FTS-нога,
взвешенный RRF, шумовой корпус, нормализация, hit@5-базлайн; сюда же — гигиена
индексации BL-009 (Minors ревью S06). Канон с числами-якорями — D-026 с двумя
дополнениями в `DECISIONS.md` (читать ДО дизайна: BEIR-якорь «невзвешенный RRF
проигрывает dense», α*=0.90; U+0301 режет термы пополам — nDCG 0.3634→0.4306).
Артефакт ДЗ-04 «векторный поиск» после этой сессии — гибридный.

Границы скоупа: реранкер — НЕ базовый скоуп (кандидат при недостаточном baseline,
D-026; решение — с владельцем по факту замера); БЕЗ Neo4j/plan_joins (S07/S09 — узел
S09 работает через тот же порт, гибрид подменит реализацию без правки сервиса); БЕЗ
переноса справочников из промпта тул-контура (BL-010 — V4, решение владельца
«сейчас достаточно промпта»); БЕЗ FSM (V4).

### Правила для исполнителя

1. Ветка `feat-021-<slug>` от `milestone/v3` (D-029); merge — в `milestone/v3`.
2. Фильтр прав — в ОБЕИХ ногах ДО слияния (NFR-4: слияние с нелегальными документами
   + пост-фильтр = дыра; джойн с agent_user_roles/agent_permissions в SQL каждой ноги).
3. Константы — именованные с комментарием «почему» (D-026): `RRF_ALPHA_DENSE = 0.9`,
   `RRF_K = 60`, `MAX_LEG_DEPTH` (кап глубины ноги), размер шумового корпуса.
4. `content_tsv` — generated-колонка `to_tsvector('russian', content)` STORED + GIN;
   конфиг — литералом (generated immutable); новая миграция, правка применённой
   запрещена (S00-5, D-028); sync `docs/diagrams/er-data-model.md` тем же коммитом.
5. Шумовой корпус: `doc_kind='noise'`, `table_name NULL`, генератор — только stdlib,
   фиксированный сид, самопроверка двойной генерацией (побитово тот же результат);
   дистрактор ДЕЛИТ общий признак с целью (иначе задача вырождается и hit@5 обманчиво
   высок); участвует в ранжировании, НИКОГДА не попадает в промпт (фильтр при сборке
   контекста — отдельный слой от поиска). Ruling N2: шум несёт непустой
   `referenced_tables` (= упомянутые таблицы) — guard универсален, NFR-4 без
   исключений; CHECK-константа doc_kind расширяется миграцией 0010 (ruling blocker).
6. Нормализация — один общий хелпер на upsert и на запрос (снятие комбинирующего
   акута U+0301, ё→е; регистр не трогаем — to_tsvector приводит сам).
7. Порт: FTS-ноге нужен текст запроса, а сигнатура S06 — `search(query_embedding, *,
   ctx, top_k)` — расширить текстом запроса (`search(query, query_embedding, *, ctx,
   top_k)`), синк строки порта в S00 тем же коммитом (прецедент N1/S06; canon-check
   при старте подтверждает).

## Предусловия

- [x] Сессия СТАРТУЕТ: статус в шапке переведён в «◐ в работе» и ARCH-GATE резолвлен ДО кода (canon-check architect ✅ либо обоснованный N/A) — старт-гейт (`guard-subagents.sh --phase start`, D-027) проверяет только стартованные (◐) сессии; «[ ] не начата» — план, гейт её скипает
- [x] S06 закрыта: feat-006 passing (пререквизит проверки — integration test_vector_memory в общем прогоне сессии)
- [x] `make check` зелёный (baseline репозитория — 329 passed на HEAD d0c78c9 закрытия S09; ветка чистая)
- [x] LM Studio отвечает, bge-m3 доступен: `curl -s http://127.0.0.1:1234/v1/models` → `text-embedding-bge-m3` в списке
- [x] feat-006 в state=passing; feat-021 активирована в state=active до старта работ

## Задачи

### Task 1: Нормализация текста — общий хелпер

`src/text_to_sql_agent/infrastructure/memory/text_normalizer.py` (или рядом с
адаптером): снятие U+0301, ё→е; применяется и при upsert (content → tsv-колонку
нормализует конфиг миграции? — нет, generated-колонка берёт content как есть:
нормализовать контент ДО записи), и к тексту запроса FTS-ноги. Тесты: «Кана́рские»
не режет термы; ё/е эквивалентны; латиница (имена таблиц) не трогается.

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_text_normalizer.py -q   # green
```

---

### Task 2: Миграция — content_tsv + GIN

Новый файл `src/migrations/versions/` (down_revision = текущий head): generated-
колонка `content_tsv tsvector GENERATED ALWAYS AS (to_tsvector('russian', <content>))
STORED` + GIN-индекс. Вопрос нормализации в generated-колонке: конфиг литералом
(immutable) — если хелпер нужен в SQL, проверить ограничение immutable для PG
(Ловушки-3); при невозможности — generated по сырому content + нормализация в
upsert-контенте (решение в задаче, отражить комментарием). Sync er-data-model тем же
коммитом.

**✅ Самопроверка:**

```bash
make infra-migrate && make index-knowledge && uv run python -c "<SELECT to_tsvector из agent_knowledge LIMIT 1>"   # tsv заполнен, повторный апгрейд — no-op
```

---

### Task 3: FTS-нога + взвешенный RRF в PgVectorStore

`src/text_to_sql_agent/infrastructure/memory/pgvector_store.py`: нога FTS —
ts_rank по content_tsv (websearch/tsq по нормализованному запросу), фильтр прав —
тот же джойн, что у dense (вынести общий SQL-фрагмент — DRY). Слияние — взвешенный
RRF: `score = α_dense/(RRF_K + rank_dense) + (1−α_dense)/(RRF_K + rank_fts)`,
кап глубины каждой ноги (`MAX_LEG_DEPTH`). Расширение порта текстом запроса (Правила-7)
+ синк S00 + правка вызовов (контур S25: сборка retrieval-блока уже имеет текст
вопроса — прокинуть). Unit-тесты на моках курсора: SQL обеих ног содержит джойн прав;
RRF-порядок (при α=0.9 документ dense-ранга-1 бьёт FTS-only-ранг-1 — якорь BEIR из
D-026 воспроизведён тестом формулы, без БД).

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit -q -k "vector or hybrid"   # green: RRF-формула и фильтр прав на моках
```

---

### Task 4: Шумовой корпус-дистрактор

Генератор `src/text_to_sql_agent/infrastructure/memory/noise_corpus.py`: ~300
дистракторов, fixed seed, stdlib, самопроверка двойной генерацией; каждый дистрактор
делит общий признак с реальной целью (тематика/таблица-сосед/термин, но не точная
цель); `doc_kind='noise'`, `table_name NULL`. Подключение в `make index-knowledge`
(позицией после курируемых); сборка промпта (контур S25) фильтрует noise — тест:
дистрактор в top-k поиска допустим, в retrieval-блоке промпта — никогда.

**✅ Самопроверка:**

```bash
make index-knowledge && uv run python -c "<COUNT doc_kind='noise'>"   # ~300, повторная генерация — побитово та же
```

---

### Task 5: Гигиена индексации (BL-009)

M1 — SoT списка бизнес-таблиц: каталог `agent_data_sources` (ADR-011): индексатор
читает список таблиц из каталога (v1 — все таблицы pg/sql), тест и SYSTEM_PROMPT
cli.py сверяются с ним же (дубль BUSINESS_TABLES в knowledge_sources/test_infra_schema/
SYSTEM_PROMPT/сидах устранить или сверять генерацией). M3 — транзакционное удаление
устаревших `source_ref`: сначала собрать фактические ref'ы источников, затем DELETE
недостающих в той же транзакции с upsert; смена эмбеддера — полная переиндексация
(фильтр поиска по `model_version` до пересборки — решение исполнителя, отразить в
model-card §1).

**✅ Самопроверка:**

```bash
make index-knowledge && uv run python -c "<COUNT agent_knowledge WHERE source_ref LIKE '<удалённый источник>'>"   # 0 — устаревшие чистятся транзакционно
```

Ruling N1: замена upsert() на replace-транзакцию полной коллекции (кураторские + шум
одним вызовом, DELETE отсутствующих в той же транзакции, контракт `-> int`) — синк
строки порта в S00 (search И upsert) тем же коммитом.

---

### Task 6: hit@5-бенч — baseline dense vs гибрид

Точка входа по образцу `index_knowledge` (`src/apps/bench_retrieval.py` + make-цель):
10–20 русских вопросов с известными целевыми документами, метрика hit@5; режимы
dense-only / гибрид. Отчёт — числа в сессию + model-card (baseline для будущих
апгрейдов, D-026). Если hit@5(гибрид) < hit@5(dense) — стоп: разбор промахов, тюнинг
α_dense по канону D-026; реранкер — только по решению владельца (отдельная задача/
бэклог, не молча).

**✅ Самопроверка:**

```bash
make infra-up && make bench-retrieval   # отчёт: hit@5 dense vs гибрид; гибрид ≥ dense
```

Ruling N4: `src/apps/bench_retrieval.py` регистрируется в S00 «Точки входа» тем же
коммитом (прецедент Q8/S09 — plan_joins_demo.py).

---

### Task 7: Интеграционные тесты (верификация фичи)

`src/tests/integration/test_hybrid_memory.py`: (a) обе ноги фильтруют права — точный
текстовый матч по недоступной таблице не возвращается (ACL-leak = 0 на FTS-ноге);
(b) дистракторы ранжируются вместе с реальными, но не попадают в сборку промпта;
(c) гибрид ≥ dense на замерном множестве (фиксированные вопросы из бенча);
(d) переиндексация после удаления источника чистит устаревшие source_ref;
(e) нормализация: вопрос с «ё»/акутом находит документ без них.

**✅ Самопроверка:**

```bash
make infra-up && make infra-migrate && make index-knowledge && uv run pytest src/tests/integration/test_hybrid_memory.py -q   # green (верификация фичи)
```

## Критерии приёмки

- [x] Фильтр прав в обеих ногах ДО слияния: недоступные документы не возвращаются ни dense-, ни FTS-ногой (ACL-leak = 0, behavior feat-021)
- [x] hit@5 гибрида не ниже dense-only на замерном корпусе; baseline зафиксирован в отчёте сессии и model-card
- [x] Дистракторы (~300, fixed seed, делят признак с целью) участвуют в ранжировании и не попадают в промпт (behavior feat-021)
- [x] Переиндексация транзакционно чистит устаревшие source_ref; SoT бизнес-таблиц — каталог agent_data_sources (BL-009 M1/M3)
- [x] Нормализация U+0301/ё→е общим хелпером на upsert и запрос (BL-007)
- [x] BL-007 и BL-009 закрыты: статус «закрыт (дата, S26)» в обоих файлах + строки реестра backlog/README.md
- [x] Раздел hw-04 «векторный поиск» дополнен гибридом (замер hit@5); итоговая сборка README — последняя закрывающаяся сессия вехи
- [x] Фича закрыта: `make verify-feature F=feat-021` → PASS, state=passing с evidence

## Ловушки

1. **Невзвешенный RRF — ловушка №1** (D-026, BEIR: ArguAna 0.560 vs 0.642): плоский
   1/(K+rank) даёт слабой ноге диктовать порядок; вес α_dense=0.9 — старт без тюнинга,
   тест формулы обязателен.
2. Пост-фильтрация после слияния — дыра (NFR-4): документ из недоступной таблицы,
   слияный в top-k, уже утёк; джойн прав в SQL обеих ног.
3. Generated-колонка immutable: функция/конфиг с параметрами не пройдёт — конфиг
   литералом; если нормализация не влезает в generated — нормализовать контент при
   upsert (хелпер Task 1), в колонке — сырое правило.
4. Дистрактор «про вообще другое» — вырожденная задача: hit@5 обманчиво высок, замер
   ничего не меряет; общий признак с целью обязателен (D-026 доп.1-3).
5. Акут U+0301 — не буква: to_tsvector режет «Кана́рские» на «кан»+«рские»; хелпер —
   до tsv и до tsq, одна реализация на оба места (DRY).
6. Смена α/K/глубины — константы с комментарием «почему», не магические числа в SQL.
7. top_k=8 (S24): бенч меряет hit@5 по 8 выданным — целевые документы в top-8; не
   «удлиняйте» выдачу ради метрики.
8. verify-feature strict: N/A «сессия не начата» не проходит; слой-2 фичи требует
   поднятую инфру и выполненную индексацию (БД-тесты скипаются без Docker — Ловушки-6
   S06).
9. S09 параллельна: не менять сигнатуру вызова retrieval в её сервисе — только
   прокинуть текст запроса через порт; контур S25 собирает блок тем же вызовом.

## Гейты субагентов (заполняется по факту, до закрытия сессии)

- ARCH-GATE: ✅ canon-check архитектора 2026-11-21 — verdict violations → rulings N1–N4 ДО кода: (blocker) миграция 0010 включает DROP/ADD CONSTRAINT doc_kind CHECK (+ 'noise') рядом с content_tsv+GIN — CHECK 0005 (20261120_0005_vector_memory.py:29) не знает 'noise', индексация шума упала бы на constraint; (N1) upsert-семантика — вариант A: replace-транзакция полной коллекции (кураторские + шум одним вызовом, DELETE отсутствующих source_ref в той же транзакции, контракт `-> int` сохранён; отдельный prune-метод отвергнут — второй способ той же операции), синк строки порта в S00 — search И upsert тем же коммитом; (N2) шум несёт непустой referenced_tables (= упомянутые таблицы — дистрактор и так делит признак с целью) — guard check_declared_references универсален, NFR-4 без исключений, table_name остаётся NULL (D-026), бенч — пользователь с широкими правами; вариант B «шум вне guard» отвергнут (исключение из инварианта C1); (N3) каждая нога берёт LIMIT MAX_LEG_DEPTH, срез top_k — после RRF; (N4) bench_retrieval.py регистрируется в S00 «Точки входа» тем же коммитом (прецедент Q8/S09). Compliant: порт-расширение search(query, …) прецедентом N1/S06 (потребители cli.py:506, plan_joins_service.py:242, фейки), миграция новым файлом (S00-5/D-028), фильтр промпта в presentation-слое (cli.py format_retrieval_block, doc_kind — поле DTO порта), SoT — каталог agent_data_sources (ADR-011, засеян 0003), константы RRF/нормализация в infrastructure/memory (адаптер реализует порт, D-026), реранкер/BL-010 — вне скоупа (D-026, решение владельца)
- REVIEW-GATE: ✅ Ready to merge: Yes — ревью-1 «Ready to merge: No» (1C/4I/12m, диапазон d0c78c9..ece1e04) → fix-волна 3fdf014: **C1** DOWNGRADE_SQL удаляет noise-строки ДО ADD CONSTRAINT (CHECK без 'noise' валидирует существующие строки — downgrade на живом корпусе падал бы CheckViolation) с транзакционным регрессом test_migration_0010_downgrade_survives_noise_rows (importlib + ROLLBACK; stash-прогон: на до-фиксовом коде CheckViolation — кусает); **I2** фолбэк-лексика генератора шума (обещание BL-009 M1 «новая таблица без правки кода») + тест; **I3** bench — build_container(rrf_alpha_dense=1.0), сборка адаптера только в composition + wiring-тест; **I4a** integration test_pure_fts_leg_works_and_filters_rights (α=0: score>0 только через content_tsv + ACL-leak=0 негатив), **I4b** unit test_search_normalizes_query_text_for_fts_leg (fts_params[0]); **I5** quality-document memory/ под feat-021; minors m6–m17 → scoped re-review «Ready to merge: Yes» (5/5 ключевых закрыто, диапазон d0c78c9..3fdf014) → доводка миноров re-review (коммит закрытия): m9 re.escape guard-regex, m17 top_k ≤ 2×MAX_LEG_DEPTH в порте, общий shorten_error_message (errors.py — дубль m8), docstring build_container, graph_store.close() dense-контура bench, тест неймспейс-гварда, неймспейсы тестов it-hybrid-norm:/it-hybrid-fts:; BL-012 (P3) — датасет бенча сверх hit@5 (set aside re-review); make check EXIT=0 (**370 passed**)

## Отчёт по сессии (заполняется по факту)

- Сделано:
  - Task 1 — `infrastructure/memory/text_normalizer.py`: снятие U+0301, ё→е (оба регистра),
    регистр не трогается, идемпотентность; 7 unit (test_text_normalizer.py).
  - Task 2 — миграция `20261121_0010_hybrid_memory_fts.py` (down_revision s09_0009):
    content_tsv GENERATED to_tsvector('russian') STORED + GIN + CHECK doc_kind + 'noise'
    (blocker ruling canon-check); применена живьём (column/index/checks/tsv-sample
    верифицированы), er-data-model v1.9 → v1.10 тем же коммитом (92141e1).
  - Task 3 — порт `search(query, query_embedding, *, ctx, top_k)` (прецедент N1/S06) +
    DocKind.NOISE; PgVectorStore: две ноги на одном соединении, фильтр прав в SQL ОБЕИХ
    ног ДО слияния (общий _RIGHTS_FILTER_SQL, FTS-нога — k-квалификация), взвешенный RRF
    в чистой функции _rrf_merge (α_dense=0.9, RRF_K=60, MAX_LEG_DEPTH=50 — числа-якоря
    D-026 с «почему»; ранги 1-based, tie-break source_ref, срез top_k после слияния —
    ruling N3), plainto_tsquery по нормализованному тексту, model_version-фильтр (M3),
    контейнером прокидывается expected_model_version; синк S00-строки порта (c9f12f2).
  - Task 4 — `noise_corpus.py`: 300 дистракторов, stdlib + сид 20261121 (побитовая
    воспроизводимость), 2/3 кросс-табличных, делят признак с целью, referenced_tables по
    упоминаниям (ruling N2 — guard универсален); подключены в index-knowledge
    (кураторские + шум одной replace-коллекцией); фильтр noise в format_retrieval_block
    (presentation) и в _candidate_tables plan_joins.
  - Task 5 — BL-009: M1 — business_tables_from_catalog (SoT agent_data_sources), сборщики
    knowledge_sources параметризованы, константа BUSINESS_TABLES удалена, test_infra_schema
    сверяет каталог↔миграция 0001 (test_catalog_matches_schema_migration), SYSTEM_PROMPT —
    unit-сверкой генерацией; M3 — upsert replace-семантики (DELETE устаревших неймспейсов
    в транзакции upsert) + фильтр model_version (решение исполнителя — model-card §1 v1.5).
  - Task 6 — `apps/bench_retrieval.py` + make bench-retrieval (ruling N4 — S00 «Точки
    входа»): 14 фиксированных вопросов, hit@5 по top_k=8 (S24), dense-only = тот же
    контур с α=1.0; live-прогон (корпус 324 = 24 кураторских + 300 шумовых):
    **hit@5 dense 14/14 = гибрид 14/14** — инвариант D-026 выполнен, baseline в
    model-card §1; hw-04/README.md — итоговая сборка вехи (раздел «Гибридная векторная
    память» с живым выводом).
  - Task 7 — `test_hybrid_memory.py` (integration, синтетические векторы — S00 правило 7):
    (a) ACL-leak=0, (b) шум ранжируется/не в промпте, (c) гибрид ≥ dense, (d) replace-чистка
    source_ref, (e) нормализация ё/акут + test_vector_memory адаптирован под гибрид (8 passed).
  - Гейты: ARCH-GATE ✅ canon-check (violations → rulings N1–N4 + blocker до кода);
    REVIEW-GATE — ревью-1 «Ready to merge: No» (1C/4I/12m) → fix-волна 3fdf014 (C1 downgrade
    миграции — DELETE noise ДО ADD CONSTRAINT — с транзакционным регресс-тестом, проверен
    stash-прогоном: на до-фиксовом коде CheckViolation; I2 фолбэк-лексика шума + тест;
    I3 bench через build_container(rrf_alpha_dense) + wiring-тест; I4a чистая FTS-нога
    α=0 + ACL-leak-негатив, I4b unit-тест нормализации запроса; I5 quality-document;
    minors m6–m17 точечными правками) → scoped re-review. make check EXIT=0 (369 passed).
- Отклонения от плана:
  - Ruling N1 уточнён при реализации: DELETE устаревших source_ref скоупится
    НЕЙМСПЕЙСАМИ коллекции (префикс до ':'), а не «всем, чего нет в батче» — живой
    корпус и integration-сиды сосуществуют в одной таблице; безусловный delete сделал
    бы тестовый сид очисткой живого корпуса (test_real_corpus остался без данных).
    Зафиксировано в докстринге адаптера и файла сессии (Правило-5/Task 5).
  - FTS-запрос — plainto_tsquery (AND-семантика), не websearch: канон выбор не фиксирует
    (architect — set aside); точные латинские идентификаторы работают термом.
  - «Dense-only» в bench — тот же гибридный контур с α_dense=1.0 (вклад FTS-ноги
    нулевой, порядок = чистому dense): отдельного метода/ноги в порт не вводилось.
  - Тест (c) «гибрид ≥ dense» — на синтетическом корпусе с синтетическими векторами
    (реальный шлюз в тестах не вызывается, S00 правило 7); настоящий замер — bench
    (layer-3) + baseline в model-card.
  - Нормализация НЕ применяется к входу эмбеддера (контент эмбеддируется сырым,
    запрос — сырым): она адресована FTS-ноге (лексический разбор), bge-m3 к ё/акуту
    устойчив (осознанный дизайн; reviewer — set aside, не находка).
  - Helper сверка «миграция ↔ таблицы» дублируется в двух тест-файлах (unit — SYSTEM_PROMPT,
    integration — каталог): разные поверхности проверки, дубль осознанный (m16).
- Перенесено в другие сессии:
  - BL-012 (P3, создан по set-aside re-review): датасет bench — вопросы, где FTS
    обыгрывает dense, + MRR/recall@8 (насыщенный baseline 14/14 не различает режимы) —
    под будущие апгрейды retrieval.
  - Реранкер — остаётся решением владельца при недостаточном baseline (D-026): baseline
    14/14 достаточен, не потребовался (BL-007 закрыт с этой формулировкой).

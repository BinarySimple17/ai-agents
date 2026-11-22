# FIXES — журнал устранения противоречий канона

Формат записи: противоречие → решение → где исправлено. Журнал ведётся с первой рабочей сессии; запись создаётся при обнаружении противоречия между документами (канон S00 ↔ ADR ↔ свёртки docs/) или между документом и кодом. Номер — FIX-001, FIX-002, … (сквозной).

Правила:

- Противоречие не правится тихо: сначала запись здесь, затем решение владельца, затем синхронная правка **всех** затронутых мест одним PR.
- Файлы закрытых сессий (`solution/sessions/*.md`) не переписываются — это исторические записи; правятся только канон S00, `docs/`, код.
- Запись в журнале — не замена ADR: если «решение противоречия» — новое архитектурное решение, параллельно заводится ADR (`docs/adr/`), а FIX ссылается на него.

---

## FIX-001 — Канон S00 описывал раскладку кода, противоречащую принятому пакету (c4-l2 → code-architecture-draft)

**Дата:** 2026-11-15 · **Сессия:** harness-сессия (перенос архпакета v1.7 в docs/) · **Затрагивает:** S00 («Архитектура кода (канон)»), AGENTS.md («Architecture Boundaries»), ARCHITECTURE.md, feature_list.json (feat-001/004/017), скелет `src/`

**Противоречие.** Канон S00 (v от 2026-11-15 утро) задавал раскладку «пакет `text2sql`, слои cli/graph/services/domain» и порты `LLMClient/VectorStore/JoinGraph/Cache/ExecConn`. После переноса пакета v1.7 в docs/ (D-007) выяснилось: [c4-l2-container.md](../docs/diagrams/c4-l2-container.md) ссылается на `code-architecture-draft.md` (§2) как на канон слоёв и портов — там «пакет `text_to_sql_agent`», слои domain / application (ports/services/use_cases) / infrastructure / orchestration / config / composition, презентационный слой `apps/` (cli + streamlit), `sql/`, тесты unit/integration/e2e; порты — LLMProvider / VectorStore / SchemaGraphStore / SQLExecutor (c `ctx`, ADR-011) / SemanticCache / Tracer / BudgetGuard. Канон harness противоречил бы принятому пакету с первой же рабочей сессии.

**Решение (владелец, 2026-11-15):** код-архитектура — по draft §2 (D-008). S00 и AGENTS.md описывают draft-раскладку; скелет выровнен (пакет переименован, папки созданы, uv.lock пересобран, init.sh зелёный); порты стыков S00 = порты draft.

**Где исправлено:**

- `solution/sessions/S00_session.md` — «Архитектура кода (канон)» и «Контракты стыка» переписаны под draft/ADR
- `AGENTS.md` — Architecture Boundaries, Required Artifacts, intro
- `pyproject.toml` (name text-to-sql-agent, packages), `init.sh`, `Makefile` (dev/seed/lint/e2e пути), `feature_list.json` (описание feat-001, команды feat-004/017 → `apps.cli`)
- `src/` — переименован пакет, созданы под-пакеты по draft, тесты — tests/unit|integration|e2e
- `docs/ARCHITECTURE.md` — Components/Boundaries под draft

**Следствие:** feat-001 (passing) остаётся валидной — verification (`./init.sh`) перепроверен после реструктуризации (exit 0); verification-команды фич S04/S17 используют `apps.cli`; до старта S04 раскладка не меняется без новой правки S00.

---

## FIX-002 — Draft §2 предлагал apps/, sql/ и tests/ в корне репо; решение владельца — весь код в src/, схема через Alembic

**Дата:** 2026-11-15 · **Сессия:** harness-сессия (правила размещения кода) · **Затрагивает:** S00 («Архитектура кода», правило 5, контракт 4), D-008, AGENTS.md, ARCHITECTURE.md, README.md, feature_list.json (все verification-команды), скелет `src/`, Makefile, init.sh

**Противоречие.** [code-architecture-draft §2](`.drafts/code-architecture-draft.md`) и унаследовавший его канон S00 (D-008) размещали презентационный слой `apps/`, схему `sql/` (init.sql + seed.py) и тесты `tests/` в корне репозитория. Владелец решил иначе: **весь код — в `src/`** (включая тесты и миграции), а БД инициализируется **миграциями Alembic**, что упраздняет `sql/` и seed-скрипты.

**Решение (владелец, 2026-11-15):** D-009 — все кодовые артефакты в `src/` (`src/apps/`, `src/migrations/`, `src/tests/`); схема и демо-данные — только миграции Alembic (правило 5 S00 переписано: seed-дисциплина переведена на миграционную — правка применённой миграции запрещена, новая правка = новый файл).

**Где исправлено:**

- `src/` — `apps/` и `tests/` (unit/integration/e2e + conftest) перенесены внутрь, `sql/` удалён, `src/migrations/` скаффолдирован (env.py fail-fast по POSTGRES_DSN, script.py.mako, versions/)
- `alembic.ini` (script_location = src/migrations), `pyproject.toml` (+alembic/sqlalchemy, packages, testpaths), `uv.lock` пересобран
- `Makefile` — `infra-seed` → `infra-migrate` (alembic upgrade head); lint/e2e — пути src; `init.sh` — ruff/compileall по src/
- `feature_list.json` — все verification/layer-команды переписаны на `src/tests/{unit,integration}/…`; feat-002 переформулирована на миграции Alembic
- `S00` — раскладка, правило 5, контракт 4 («Миграции ↔ схема»); `AGENTS.md`, `docs/ARCHITECTURE.md`, `README.md` — границы и структура

**Следствие:** feat-001 (passing) перепроверена после переноса (`./init.sh` exit 0); feat-002 (not_started) закрыть через `make verify-feature` можно только после первой миграции; verification-команды БД-фич требуют `make infra-migrate` вместо infra-seed.

---

## FIX-003 — Вехи harness (M1–M4) не совпадали с вехами ДЗ-плана (ДЗ-02…06): другой порядок, нет дат и веток

**Дата:** 2026-11-15 · **Сессия:** harness-сессия (сверка с планом выполнения ДЗ) · **Затрагивает:** [solution/sessions.md](sessions.md) (вехи, граф, сессии S18/S19), feature_list.json (feat-009, feat-013 deps; +feat-018, +feat-019), DECISIONS D-012

**Противоречие.** План выполнения ДЗ ([`.docs/hw-plan.md`](../../.docs/hw-plan.md) v1.0, READ ONLY) задаёт вехи проекта = ДЗ-02…06 с датами (внутренние 30.11/07.12/21.12/28.12/16.01; сдачи 01.12/08.12/27.12/12.01/17.01) и ветками `hw-0N-*`. Вехи harness (M1–M4: Каркас/Данные/Полный цикл/Оптимизации) группировали объём иначе, без дат и без привязки к ДЗ. Кроме того, зависимости фич не позволяли достичь ДЗ-вех в порядке плана: feat-013 (run_sql — тул ДЗ-02) зависела от feat-012 (check_cost — не входит в ДЗ-02), feat-009 (plan_joins — ДЗ-04) — от feat-008 (clarify — собирается в ДЗ-05). Объёмы ДЗ-03 (стриминг) и ДЗ-06 (live-стек ADR-010) не имели фич вовсе.

**Решение (владелец, 2026-11-15):** вехи harness выровнены под hw-plan — таблица V1–V6 с датами, ветками, маппингом фич/сессий (sessions.md «Критические зависимости и график»). Зависимости скорректированы: feat-009 → [feat-006, feat-007] (ДЗ-04 без clarify — допущения фиксирует код), feat-013 → [feat-002] (тул run_sql сразу после БД; узел в FSM — ДЗ-05). Базовый FSM ДЗ-05 — без check_cost (транзитная заглушка, план Б hw-01 §3.4); feat-012 + feat-017 — выпускная веха V6. Добавлены feat-018 (Observability Plane live, ADR-010 — ДЗ-06) и feat-019 (стриминг — ДЗ-03), сессии S18/S19.

**Где исправлено:**

- [solution/sessions.md](sessions.md) — таблица вех V1–V6 с датами/ветками/маппингом, граф зависимостей, строки S18/S19
- `feature_list.json` — deps feat-009/feat-013, примечания feat-004/feat-012, новые feat-018/feat-019
- [DECISIONS.md](../../DECISIONS.md) D-012 — решение о выравнивании

**Следствие:** исполнение фич продолжается по зависимостям feature_list; ДЗ-вехи закрываются по набору объёма (сессии не перенумеровываются). ДЗ-02 становится ближайшей целью (дедлайн 30.11.2026): S02 → S03 → S13. Слияние каждой вехи в `main` после приёмки (hw-plan §8).

---

## FIX-004 — Таблица сессий и граф в sessions.md не были синхронизированы с FIX-003: строки S09/S13 блокировали вехи более поздними ДЗ

**Дата:** 2026-11-15 · **Сессия:** harness-сессия (аудит реализуемости план-сессий) · **Затрагивает:** [solution/sessions.md](sessions.md) (строки S09/S13/S19, примечание к таблице, граф, ячейки V4/V6), `feature_list.json` (feat-004 behavior, feat-013 описание + layer-3), [.docs/hw-plan.md](../../.docs/hw-plan.md) §2/§7 (READ ONLY — источник объёмов)

**Противоречие.** После правки deps фич (FIX-003) таблица сессий в sessions.md осталась на старых зависимостях: S13 зависела от S12 (check_cost — веха V6), хотя feat-013 по FIX-003 зависит только от feat-002 и закрывается в V1 → веха V1 (ДЗ-02, дедлайн 30.11) была бы заблокирована сессией V6; S09 зависела от S08 (clarify — V4), хотя feat-009 идёт без clarify в V3 (ДЗ-04) → веха V3 блокировалась вехой V4. ASCII-граф повторял нарушение (S14 изображён после линии S11→S12) и содержал артефакты (дубль S16). Ячейка V4 «S10–S15» затягивала в веху сессию S12 (V6); ячейка V6 не содержала S12 и была неполна против hw-plan §7 (экономика ADR-007, завершение ADR-008). Приоритет S19 (P1) противоречил её дедлайну (ДЗ-03, 07.12 — раньше P0-сессий V4). Объём feat-013 не покрывал артефакты сдачи ДЗ-02 (JSON-схема тулa, function-calling цикл, SOP — hw-plan §2), behavior feat-004 упоминал несуществующую команду `uv run text2sql`.

**Решение:** синхронизировать план-сессии с FIX-003/D-012 — порядок исполнения задаётся вехами V1–V6 (порядок ДЗ), а не нумерацией S-NN; артефакты ДЗ-02 ввести в объём feat-013 (+ layer-3 ручной прогон tool-контура). Решение следует из уже принятых D-012/FIX-003 (hw-plan первичен), нового владельческого решения не требует.

**Где исправлено:**

- [solution/sessions.md](sessions.md) — deps S09 → «S06, S07»; S13 → «S02» (+ описание «тул run_sql (ДЗ-02) + узел execute_sql»); S19 → P0; примечание «порядок исполнения = вехи V1–V6, приоритет P0 = ближайшая веха»; ASCII-граф заменён на порядок исполнения по вехам + полный список зависимостей сессий; ячейки вех: V4 — сессии без S12/S13 (+подключение узла feat-013), V6 — += S12, экономика (ADR-007), двухслойные форматы (ADR-008)
- `feature_list.json` — feat-013: описание дополнено артефактами ДЗ-02 (JSON-схема, function-calling цикл в apps.cli, SOP), добавлен layer-3-e2e (ручной CLI-прогон с LLM_API_KEY); feat-004: behavior `text2sql` → `python -m apps.cli`
- `solution/sessions/S00_session.md` — самопроверки «Запрещено» переведены с пакета `src/text2sql` (до FIX-001) на `src/text_to_sql_agent` (orchestration/application/domain) и порт `LLMClient` → `LLMProvider`: grep по несуществующему пути всегда пуст — проверка была фиктивна

**Следствие:** порядок исполнения согласован с ДЗ (скрипт-проверка: deps каждой фичи и каждой строки таблицы сессий лежат в её вехе или раньше — межвеховых блокировок нет; граф ацикличен). S13 закрывает feat-013 целиком в V1 (подключение узла к FSM — задачи V4). Ближайшая цель прежняя — ДЗ-02 (30.11.2026): S02 → S03 → S13; в S13 теперь входят артефакты сдачи ДЗ-02.

---

## FIX-005 — Пакет v1.7 опережал контракты стыков S00 и инфраструктуру: tools/usage/embed/AuthProvider, AGE без shortestPath, ER без agent_data_sources, compose против security §5

**Дата:** 2026-11-15 · **Сессия:** harness-сессия (red-team аудит архпакета, скилл design-red-team-audit) · **Затрагивает:** S00 (таблица портов, контракт 2), `.env.example`, `docker-compose.yml`, [er-data-model](../docs/diagrams/er-data-model.md) v1.3, [security](../docs/security.md) v1.4, [ADR-003](../docs/adr/ADR-003-single-postgres.md) v1.1, [arch-package](../docs/arch-package.md) (реестр/история), `solution/backlog/BL-001`

**Противоречие.** Аудит пакета против контрактов стыков и уже написанной инфры — 11 находок: (1) контракт `LLMProvider.complete(role, messages) -> str` не покрывал function calling (тулы ADR-008 — ядро ДЗ-02) и не возвращал `usage` → кап 2 ₽ (FR-14) и `node_timings` (FR-11) не вычислимы; (2) эмбеддер (model-card: qwen3-embedding-8b; draft зовёт `embedder.embed_query`) отсутствовал в портах — неясно, кто эмбеддит для `VectorStore.search` (ДЗ-03 = RAG); (3) цены моделей и `EMBEDDING_MODEL` отсутствовали в `.env.example`; (4) в openCypher/AGE **нет** функции `shortestPath` (Neo4j-специфична; подтверждено документацией AGE) — ADR-003 §4 формулировал механику несуществующим вызовом; (5) er-data-model v1.2 не содержал `agent_data_sources` (добавлена ADR-011 v1.7; feat-002 её создаёт — канон схемы отставал от миграции); (6) compose публиковал 5432/6379 на все интерфейсы вопреки security §5/S00 «только localhost» и держал в шапке ссылку на упразднённый `make infra-seed` (D-009); (7) `GRANT SELECT ON ALL TABLES` давал `agent_ro` чтение `query_trace` (вопросы всех пользователей) — рубеж 3 обязан быть defense-in-depth; (8) порт `AuthProvider` (ADR-005, c4-l2, FR-6) отсутствовал в таблице портов S00/draft; (9) guardrails I1/I2/rate-limit (security §3) не покрыты фичами; (10) у запиненного `valkey/valkey` нет KNN — «семантический кэш Valkey (vector search)» (ADR-009) нереализуем как написано; (11) `/metrics` (ADR-010: «потребляется compose healthcheck'ом») несовместим с CLI-процессом на хосте (D-005 — HTTP API нет).

**Решение.** Пункты 1–9 — механика, исполняющая уже принятые решения (ADR-005/007/008/011, D-009, security §5): исправлены синхронно одним заходом. Пункты 10–11 — архитектурный выбор владельца (варианты с рекомендацией: близость через pgvector при Valkey-значениях; источник `/metrics` — Streamlit-демо); **до решения владельца ADR-009/010 не переписываются**, фиксация — progress.md Blockers. Пункт 9 — backlog BL-001.

**Где исправлено:**

- S00 — `LLMProvider.complete(role, messages, *, tools=None) -> LLMResponse(text, tool_calls, usage)` + `embed(texts)`; +порт `AuthProvider.verify(token) -> identity` (S05); инвариант «usage → ₽ (BudgetGuard) и node_timings; тулы — параметром tools» в контракте 2
- `.env.example` — `MODEL_*_PRICE_IN/OUT` (₽/1М по ролям, model-card) + `EMBEDDING_MODEL`
- `docker-compose.yml` — порты `127.0.0.1:` (security §5), шапка: схема через `make infra-migrate` (D-009)
- er-data-model v1.3 — +`AGENT_DATA_SOURCES` (ds_id, protocol `sql|spec`); arch-package — реестр и история
- security v1.4 — `REVOKE SELECT ON query_trace FROM agent_ro` в §2.1
- ADR-003 v1.1 — механика path-поиска: var-length `MATCH p=…-[*..N]->… ORDER BY length(p) LIMIT 1`; «shortestPath» в остальных документах = имя задачи
- `solution/backlog/BL-001-guardrails-runtime.md` + строка в реестре беклога

**Следствие:** S03 (V1) получает контракт, покрывающий ДЗ-02 (тулы) и бюджет; S05 — порт AuthProvider; S06 — embed; S07 — реализуемую механику графа; feat-002 пишет миграцию по ER v1.3 (с `agent_data_sources`). Открытые решения (кэш-близость, `/metrics`) не блокируют V1–V4;feat-016 (V6) и feat-018 (V5) — первые затронутые.

**Решение владельца (2026-11-15) — по пунктам 10–11:**

1. **Кэш — без семантической близости:** точное совпадение нормализованного вопроса + TTL-справочные шаги (вариант «точный ключ» из рекомендаций; pgvector-механизм близости отклонён на v1). ADR-009 → v1.2: заголовок «кэш вопросов… (точное совпадение)», решение §4 переписано (Valkey-KNN исключён), риск R-Q5 снят по построению (остаток — вопросные допущения при точном повторе, правило 6), триггер возврата близости — только через pgvector и новый ADR. Провода: prd FR-13 (v1.5), RISKS R-Q5 + реестр пересмотра, observability §4, c4-l2 (VK-бокс), ARCHITECTURE (Overview), feat-016 (name/description/behavior; заодно исправлен устаревший «хит мимо clarify» — lookup по ADR-009 v1.1 идёт после clarify), S16.
2. **Источник `/metrics` — Streamlit-демо:** долгоживущий процесс поднимает HTTP-сервер OTel (/metrics, /livez, /readyz), Prometheus из compose скрейбит `host.docker.internal:<port>`; CLI-прогоны — OTLP-спаны + query_trace. ADR-010 → v1.2 (п. 1/п. 3; заодно заголовок Tempo → Jaeger — редакционный остаток). Провода: observability §1/§2.3/§5 (v1.3), c4-l2 v1.6 (ребро скрейпа + буллеты), feat-018.

---

## FIX-006 — Ревью-03 ДЗ-1 против канона docs/: AGE vs Neo4j (D-013), ретраи D-014, двухисточниковые FK (ADR-006), HOW-детали вне артефакта сдачи

**Дата:** 2026-11-18 · **Сессия:** apply-review-03 (ревизия `hw-01/README.md` по внешнему брифу `.docs/review-03.md` + патчу автора: Python-стек) · **Затрагивает:** `hw-01/README.md`, [DECISIONS](../DECISIONS.md) (D-015…D-017), [BL-002](../solution/backlog/BL-002-docs-sync-review-03.md)

**Противоречие.** Внешний бриф review-03 (~50 пунктов A/S/R/M/E/N/C/F/Q/V) нацелен на артефакт сдачи `hw-01/README.md`, но минимум четыре группы пунктов конфликтуют с каноном `docs/`: (a) §F/N5 датированно проверяют факты AGE и обосновывают граф-хранилище, а канон — Neo4j (D-013, ADR-003 v2.0), и D-013 объявил hw-01 «не переписывается»; (b) M1 (попытка 2 — та же модель + текст ошибки, попытка 3 — уровень вверх) противоречит D-014/ADR-007 v1.1 «повторная попытка идёт на уровень вверх», а экономика брифа (E1: retry по цене qwen) сходится только при новой политике; (c) принцип 1 «реестр — единственный SoT, FK — только генератор кандидатов» противоречит двухисточниковой модели ADR-006 v1.2 / prd FR-7 («FK-рёбра вычитываются из каталога автоматически»); (d) бриф требует HOW-детали (полный DDL ребра R1, манифесты развёртывания N1, развёрнутый AgentState N3, red-team-детали N4), не помещающиеся в артефакт сдачи без раздувания.

**Решение (владелец, 2026-11-18).** (a) hw-01 остаётся на AGE как ревизируемый артефакт сдачи + мост «граф — производный снапшот реестра, хранилище — заменяемая деталь за `find_join_paths()`» (принцип 4/N5): оба утверждения истинны одновременно, канон Neo4j не меняется; оговорка «hw-01 не переписывается» из D-013 снимается владельцем (артефакт снова живой — пришло внешнее ревью; прецедент точечной правки уже был в D-014). (b) принята политика M1 → D-016, в hw-01 §4.3 она уже действует; ADR-007 → v1.2 в сессии синхронизации. (c) реестр-SoT принят для hw-01 (§3.3/§3.5); ADR-006 → v1.3 + prd FR-7 — там же. (d) hw-01 — норматив (гейты, узлы, политики, цифры, схемы), HOW-детали — в `docs/`; полный груз синхронизации перечислен в BL-002.

**Где исправлено:** `hw-01/README.md` — полная ревизия (§1–§9, приложение `AgentState`, «История ревизий» с changelog по ID — статус-строку в `.docs/review-03.md` ставить нельзя, READ ONLY D-011; «Открытые вопросы» Q1–Q4/Q6–Q7 с TODO(confirm), Q5 снят); `DECISIONS.md` (D-015 граф-мост, D-016 ретраи M1, D-017 Python-стек + дефолты Q + фиксация тарифов); `solution/backlog/BL-002` + строка в реестре беклога.

**Следствие.** Канон `docs/` временно расходится с hw-01 по: граф-хранилищу (Neo4j vs AGE+мост), ретраям (ADR-007 v1.1 vs M1), FK-модели (ADR-006 v1.2 vs реестр-SoT), экономике (3,31 ₽/81% vs 2,57 ₽/76%), составу узлов (нет `build_skeleton`/`compact_result`/probe/`generate_step_sql`-имени vs 15 узлов), новым разделам (нет §8 развёртывания, §9 eval-плана, `AgentState`, `agent_edge_proposals`, `graph_version`). Все расхождения — в BL-002 с точным списком файлов; закрывать сессией синхронизации **до S02** (feat-002 пишет миграции по ER — контракт ребра и `graph_version` должны попасть в er-data-model раньше первой миграции).

---

## FIX-007 — Canon-check S02: RLS-политики раньше механизма `SET LOCAL`, гранты ALL+REVOKE vs deny-by-default, отставание feature_list и метки v1.4

**Дата:** 2026-11-18 · **Сессия:** S02 (старт, гейт ARCH-GATE — architect canon-check, verdict needs-decision) · **Затрагивает:** `solution/sessions/S02_session.md`, `solution/sessions/S13_session.md`, `docs/security.md` §2.1, `feature_list.json` (feat-002), миграции `src/migrations/versions/`

**Противоречие (архитектор).** (a) Контракт стыка 4 S00 требует «`agent_ro` (read-only, RLS)» в миграциях S02, но конкретных RLS-политик канон не определяет, в демо-схеме нет user/tenant-колонок, а механизм `SET LOCAL app.user_id` появляется только в S13 (канал исполнения) — политики в S02 мертвы и непроверяемы. (b) security.md §2.1 задавал «GRANT SELECT ON ALL TABLES + REVOKE query_trace» — противоположно принципу deny-by-default (рубеж 3: `agent_*` «не существуют для агента»; ALTER DEFAULT PRIVILEGES делает будущие служебные таблицы читаемыми). (c) feat-002 (description/behavior, метка «схема v1.4») отстал от контракта 4 ред. 2026-11-18 (v1.5: `agent_edge_proposals`, `agent_audit`, роли `agent_planner`/`registry_writer`, вьюха, триггеры счётчиков). (d) Нумерация рубежей расходится между security.md (сжатая) и ADR-005 (пять рубежей): S02/S13 ссылались «(рубеж 3)» на отказ INSERT.

**Решение (куратор — ruling по рекомендации архитектора, 2026-11-18).** (a) RLS-политики переносятся в S13 (Task 4 — там же механизм `SET LOCAL app.user_id`); в S02 — роли, whitelist-гранты, hardening; контракт стыка 4 трактуется «роли создаются в S02, RLS-политики — S13», зафиксировано здесь. (b) Гранты — whitelist (SELECT только на 7 бизнес-таблиц); security.md §2.1 синхронизирован в том же коммите; `ALTER DEFAULT PRIVILEGES` исключён (дефолт = отсутствие гранта). (c) feat-002 синхронизирован с v1.5 в том же PR (S00 правило 3). (d) Ссылки в сессиях заменены на «рубеж 5» по ADR-005; полная унификация нумерации рубежей в security.md — отдельная док-сессия (кандидат в backlog).

**Где исправлено:** `solution/sessions/S02_session.md` (v1.5; Task 2 — `bump_settings_version()` + 3 триггера + вьюха `agent_relationships_active`; Task 3 — whitelist-гранты + hardening + страховочный триггер `query_trace`; Task 4 — фиксированные объёмы и ловушки демо-данных), `solution/sessions/S13_session.md` (Task 4 + RLS-перенос), `docs/security.md` §2.1, `feature_list.json` feat-002, миграции S02.

---

## FIX-008 — Canon-check S04: диаграмма state-agent v1.4 не рисует входящие рёбра терминалов overflow/short_circuit и шаговый цикл merge_results

**Дата:** 2026-11-21 · **Сессия:** S04 (старт, гейт ARCH-GATE — architect canon-check, verdict needs-decision) · **Затрагивает:** [docs/diagrams/state-agent.md](../docs/diagrams/state-agent.md), `solution/sessions/S00_session.md` (стык 1), orchestration/ (каркас FSM, feat-004)

**Противоречие (архитектор).** (a) Терминалы `overflow` и `short_circuit` в канонической диаграмме FSM — без входящих рёбер (`overflow --> [*]`, `short_circuit --> done` рисуют только выход): тест достижимости «каждый терминал достижим» (критерий приёмки S04/feat-004) упирается в выбор источника ребра. (b) Шаговый цикл декомпозиции (hw-01 §3.5.4: `next_step` — код: «есть неисполненные шаги?» → `generate_step_sql`, иначе `merge_results`) в диаграмме нарисован как два ребра `execute_sql --> merge_results` с аннотацией «есть ещё шаги → следующий generate_step_sql» — цикл обратно в `generate_step_sql` ребром не показан; дословная реализация дала бы ветку, где после шага цикл уходит в `compact_result` (конец прогона) вместо следующего шага.

**Решение (по нормативному источнику канона — hw-01 §3.5.4, решение владельца по итогам clock-out S04).** (a) Источники обоих терминалов — шаговый цикл `execute_sql` (канонный текст: «Short-circuit в коде executor'а: зависимые шаги не выполняются»; «вернул max_keys+1 строк — остановка + уточняющий вопрос»): рёбра `execute_sql --> short_circuit` (пустой key-set) и `execute_sql --> overflow` (key-set > капа 50 000) — флаги `step_empty_keyset`/`step_keyset_overflow`, активируются S14/S17. (b) Шаговый цикл: ребро `merge_results --> generate_step_sql` по флагу `steps_pending` (есть ещё шаги), `merge_results --> compact_result` — все шаги готовы; оба нарисованы. В каркасе S04 флаги шагового цикла заглушками не пишутся (одноразовые переходы гасятся биндером на ре-входе — защита от зацикливания на стейл-флагах).

**Где исправлено:** [docs/diagrams/state-agent.md](../docs/diagrams/state-agent.md) → v1.5 (входящие рёбра overflow/short_circuit + ребро шагового цикла merge_results → generate_step_sql + примечание «Чтение схемы»), `solution/sessions/S00_session.md` стык 1 (обвязочные поля state, состав терминалов, гвард капов, перехват GraphRecursionError в orchestration — ruling R2), orchestration/routing.py (константы флагов — единственный источник имён).

**Статус:** ЗАКРЫТ — подтверждение владельца 2026-11-21 (сразу после clock-out S04): правка диаграммы v1.5 и wiring feat-004 остаются каноном (диаграмма v1.4 → v1.5 не откатывается).

---

## FIX-009 — Canon-check S08: ребро plan_joins → clarify не существует ни в диаграмме state-agent v1.5, ни в routing.py — при этом hw-01 §4.2 и план S08 утверждают обратное

**Дата:** 2026-11-22 · **Сессия:** S08 (старт, гейт ARCH-GATE — architect canon-check, verdict needs-decision) · **Затрагивает:** [docs/diagrams/state-agent.md](../docs/diagrams/state-agent.md), `solution/sessions/S08_session.md`, orchestration/routing.py (feat-008)

**Противоречие (архитектор).** (a) План S08 (Task 3) утверждает «ребро plan_joins → clarify существует в FSM v1.4 — проверить, что заглушка S04 его уже несёт» — фактически ребра нет нигде: routing.py (route_plan_joins: fail/retry/T1/build_skeleton) clarify-ветки не имеет, диаграмма state-agent v1.5 ребро не рисует («равные кандидаты пути» свернуты в метку ребра clarify → clarification). (b) Канонный текст hw-01/README.md §4.2 при этом говорит «на равных кандидатах — clarify» — канон расходится сам с собой (текст против диаграммы). (c) S09-сервис отдаёт PlanOutcome.CLARIFY с clarifying_question («уточнение контентом узла, interrupt — V4/S08») — механике подъема до interrupt некуда идти: узел plan_joins в FSM остаётся заглушкой до S10, ветки его исхода «уточнение» не существует.

**Решение (ruling R1 ARCH-GATE S08 по рекомендации архитектора, 2026-11-22).** Ребро plan_joins → clarify закладывается в S08 по прецеденту S04 Ловушка-1 («ветки в каркасе, активируют будущие сессии»): новый флаг `FLAG_PLAN_JOINS_CLARIFY` (владение — планер; переиспользование `FLAG_CLARIFICATION_NEEDED` запрещено — флаг узла clarify, карта владения state), ветка route_plan_joins → clarify; диаграмма state-agent → v1.6 (ребро нарисовано, метка clarify → clarification уточнена) в том же коммите; активирует S10 (FSM-подключение plan_joins). Текст hw-01/README.md не переписывается (артефакт сданного ДЗ-1) — противоречие фиксируется этой записью; диаграмма v1.6 — канонная схема.

**Где исправлено:** [docs/diagrams/state-agent.md](../docs/diagrams/state-agent.md) → v1.6, orchestration/routing.py (флаг + ветка), `solution/sessions/S08_session.md` (Task 3 переформулирован «заложить», граница скоупа R4).

**Статус:** ОТКРЫТ — закрывается в S08 диаграммой v1.6 + ребром в routing.py (тот же коммит); подтверждение владельца — при clock-out S08.

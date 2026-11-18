# Session Progress Log

<!-- ЗАПОЛНЯЕТСЯ ПО ХОДУ РАБОТЫ: это журнал сессий агента. Правило из AGENTS.md:
     обновлять Current State и Next Steps в конце КАЖДОЙ сессии. Заголовки секций
     не переименовывать — их ищут скрипты проверки. -->

## Current State

**Last Updated:** 2026-11-18
**Session ID:** harness-setup (S00 + S01) → arch-package-import → plan-audit → arch-red-team (FIX-005) → escalation-rule (D-014) → apply-review-03 (ревизия hw-01/README.md по внешнему ревью + патч автора, FIX-006, D-015…D-017, BL-002)
**Active Feature:** — (feat-001 → passing; следующая — сессия синхронизации docs/ по BL-002, затем feat-002/feat-003)
**Last commit:** `hw-01/README.md` (ревизия ревью-03) → `hw-01-agent-architecture` (1022a45, запушен); весь harness/пакет/журналы → ветка `project-harness` (отпочкована 2026-11-18 для сохранения рабочего дерева; слить после приёмки ДЗ-1) — `make check`: passing

## Status

### What's Done

- [x] Шаблон harness развёрнут, AGENTS.md/feature_list (17 фич)/docs/solution заполнены, feat-001 закрыта гейтом (VCR 1/1)
- [x] Архитектурный пакет v1.7 разложен из `.docs/arch/` в tracked docs/ (D-007): 11 ADR → docs/adr/, C4 L1–L3 + FSM + sequence + ER → docs/diagrams/, prd/security/observability/model-card/RISKS → docs/, индекс пакета — docs/arch-package.md
- [x] Беклог переведён на папку `solution/backlog/` (BL-NN-файлы + archive/, вместо backlog.md); скрипт `scripts/archive.sh` (`make archive`): закрытые сессии → sessions/archive/, закрытые беклоги → backlog/archive/, «Текущее состояние» sessions.md подрезается до ОДНОЙ строки статуса последней закрытой сессии (анти-раздувание, решено владельцем); End of Session (AGENTS.md) дополнен шагом make archive; проверено e2e на тестовых файлах (перенос + идемпотентность)
- [x] Весь код собран в `src/` (D-009, FIX-002): `src/apps/`, `src/tests/` (unit/integration/e2e), `sql/` упразднён; БД инициализируется миграциями Alembic — скаффолд `src/migrations/` + `alembic.ini`, таргет `make infra-migrate` (alembic upgrade head), alembic+sqlalchemy в deps; правило 5 S00 переписано с seed-дисциплины на миграционную; feat-002 переформулирована на миграции, все verification-команды — на src/tests/{unit,integration}
- [x] Конвенция ДЗ внесена в харнесс (D-010): папки `hw-NN/` — решения ДЗ NN (артефакты сдачи), полные исторические спецификации — `.docs/hw-NN/` (вне гита); для ДЗ-1: полная спецификация — `.docs/hw-01/README.md` (≈89 КБ), `hw-01/README.md` — решение (≈30 КБ, ужат); все индексы (AGENTS, docs/README, ARCHITECTURE, arch-package, prd, S00, root README) переименованы; трассировка «Канон: hw-01/README.md §N» объяснена (публичные номера разделов, полные подразделы — .docs)
- [x] `.docs/` объявлен историческими документами READ ONLY (D-011, уточняет D-007/D-010): вся работа — в трекаемом пакете docs/ (prd.md — живые требования); MUST NOT в AGENTS.md Constraints, карта владения (`.docs/**` READ ONLY), канон в docs/README + arch-package + prd переписан на «пакет = канон, .docs = история»
- [x] Вехи выровнены под план выполнения ДЗ (D-012, FIX-003): таблица V1–V6 с датами из .docs/hw-plan.md (ДЗ-02: 30.11/01.12 … ДЗ-06: 16.01/17.01), ветками hw-0N-* и маппингом фич/сессий; deps скорректированы (feat-009 без clarify-зависимости, feat-013 — тул run_sql сразу после БД); check_cost в FSM ДЗ-05 — транзитная заглушка (план Б), feat-012+feat-017 — выпускная; добавлены feat-018 (Observability live, ДЗ-06) и feat-019 (стриминг, ДЗ-03), сессии S18/S19; ближайшая цель — ДЗ-02 (30.11.2026)
- [x] Аудит реализуемости план-сессий (FIX-004): deps 19 фич ацикличны, все лежат в своей вехе V1–V6 или раньше (скрипт-проверка); том, что таблица сессий не была синхронизирована с FIX-003 — S13←S12 блокировала V1 сессией V6, S09←S08 блокировала V3 вехой V4 — исправлено (S13←S02, S09←S06,S07); S19 → P0 (дедлайн ДЗ-03 раньше P0-сессий V4); ASCII-граф (дубль S16, S14 после S12) заменён порядком исполнения по вехам + полным списком deps; V4-ячейка без S12/S13, V6 += S12/экономика (ADR-007)/ADR-008; feat-013 дополнен артефактами сдачи ДЗ-02 (JSON-схема тулa, function-calling цикл, SOP, layer-3), feat-004 — apps.cli вместо text2sql
- [x] Red-team аудит архпакета v1.7 (скилл design-red-team-audit; FIX-005, 11 находок): контракты стыков S00 догнали пакет — `LLMProvider.complete(..., tools) -> LLMResponse(text, tool_calls, usage)` + `embed(texts)`, +порт `AuthProvider` (S05); `.env.example` — цены ролей + `EMBEDDING_MODEL`; ER → v1.3 (+`agent_data_sources`, догоняет ADR-011); security → v1.4 (`REVOKE SELECT ON query_trace FROM agent_ro`); ADR-003 → v1.1 (в AGE нет функции shortestPath — var-length MATCH, «shortestPath» = имя задачи); compose — localhost-bind портов + шапка на миграции; BL-001 (guardrails I1/I2/rate-limit вне фич). Два решения владельца открыты: механизм кэш-близости (Valkey без KNN; рекомендация — pgvector + Valkey-значения) и источник `/metrics` (CLI не скрейбится; рекомендация — Streamlit-демо)
- [x] Решения владельца по FIX-005: (1) кэш — точное совпадение, без семантической близости (ADR-009 v1.2, prd FR-13 v1.5, RISKS R-Q5 снят, feat-016, S16; заодно в feat-016 исправлен устаревший «хит мимо clarify»); (2) `/metrics` — Streamlit-демо как источник (ADR-010 v1.2, observability v1.3, c4-l2 v1.6, feat-018)
- [x] **D-013: граф — Neo4j вместо AGE (ADR-003 → v2.0)** — решение владельца по находке №4: PG — истина (данные+pgvector+настройки+query_trace), Neo4j — только топология (нативный shortestPath); официальные образы `pgvector/pgvector:0.8.0-pg17` + `neo4j:5.26.0-community` вместо кастомной сборки AGE+pgvector (инфра/`infra/postgres/Dockerfile` удалён — риск сборки на Windows снят); +neo4j-драйвер, NEO4J_* в .env; контракт синка ADR-006 §4 не меняется (v1.2); провода: prd v1.6, c4-l1 v1.1/c4-l2 v1.7 (Neo4j-контейнер)/c4-l3 v1.2/sequence v1.2/er v1.4, security v1.5, S00 (стек/порты/адаптер neo4j_graph_store), feat-002/feat-007, S02/S07, AGENTS/README/ARCHITECTURE; hw-01/README.md не переписывается (артефакт сданного ДЗ-1)
- [x] **D-014: эскалация — единое правило генерирующих узлов (ADR-007 → v1.1)** — решение владельца по план_joins/generate_sql: провал → уровень вверх (cheap→mid→strong), эскалацию назначает код (retry_count + route_flags), эскалации в общем retry-бюджете ≤ 3. **plan_joins**: добавлены структурная валидация JoinPlan при парсинге (таблицы ∈ scope, джойны ∈ рёбрам графа, ≤ 4 таблиц — как инварианты plan_query ADR-004) + retry с эскалацией + fail-ветка — раньше у узла не было ни эскалации, ни ветки провала в FSM. **generate_sql**: эскалация mid → strong (уже была в §4/FSM) проговорена в матрице. Провода: ADR-002 v1.1 (карта решений: tier назначает код), state-agent v1.2 (рёбра plan_joins), c4-l3 v1.3 (узел plan_joins + правило роутинга), prd v1.7 (FR-8/FR-10), S00 (контракты 1/2), feat-004/009/010, S09/S10, реестры; заодно снят stale-остаток «в AGE» в S00-контракте 4 и stale «Ревизии: v1.4» в prd-трассировке
- D-014 в ДЗ-1 (указание владельца, 2026-11-17): `hw-01/README.md` — новый пункт **§4.3 «Эскалация: не получилось - пробует модель поумнее»** (человеческий текст + mermaid-лесенка qwen→deepseek→glm, «только вверх», общий retry-лимит, цена худшего случая ~0,9 ₽ против капа 2 ₽); синхронные правки: §4.1 (эскалация — правило всех генерирующих узлов, ссылка на §4.3), §4.2 (строки plan_joins/generate_sql в матрице, убран дублирующий fallback-абзац), §3.1 (циклы самокоррекции), §6 (строка 1: эскалации тратят общий лимит); лимиты провайдера §4.3→§4.4, ссылки ADR-007/prd/ADR-008 выправлены; ARCHITECTURE.md/кластеры не тронуты (эскалация уже описана в ADR-007 v1.1)
- [x] **apply-review-03 (2026-11-18): ревизия `hw-01/README.md` по внешнему брифу `.docs/review-03.md` + патчу автора (Python-стек)** — все ~50 пунктов отработаны в артефакте сдачи (588 строк / 49,4 КБ, проза ужата в два захода): A1–A10 (замкнут обход валидатора веткой `plan_query → generate_step_sql → validate_sql → …`, key-set без `LIMIT` + cap 50 000 + `approximate`, fan-out guard, `compact_result`, `clarify` через `interrupt()`+checkpointer, k путей вместо кратчайшего, probe `COUNT`, T2 → async/matview, T1 — основной путь, дедлайны 90s/30s/4), S1–S11 (хэш скелета + `mandatory_predicates`, ACL колонок соединения, deny-list схем, БД — второй рубеж, покрытие валидатора, инъекции 2-го порядка, кэш через гейт, аудит append-only, отказ без раскрытия, флаги усечения, реестр read-only), R1–R7 (новый §3.5: контракт ребра, `draft → CI → active`, `graph_version`, `agent_edge_proposals`, bootstrap, cold start, SAP), M1–M8 (ретраи D-016, ранжирование путей, наполнение скелета, Spider 2.0/BIRD, structured output pydantic, деградация breaker, модели датированы снимком каталога 18.11.2026, рычаг `interpret_result`), E1–E6 (0,61/0,62/0,67 ₽, 670 ₽/1000, базлайн 2,57 ₽ и 76%, 6,6К токенов, кап с резервом 0,6 ₽), N1–N7 (новые §8 развёртывание Python-native/minikube + §9 eval-план с ACL-leak = 0, `AgentState`-приложение, LangGraph-специфика), C1–C5 (узлы/инструменты разведены, workflow-framing, внешняя аналогия убрана, сверка имён/чисел, 12 строк отказов), §F (факты AGE датированы ссылками). Открытые вопросы Q1–Q4/Q6–Q7 — дефолты + TODO(confirm); Q5 снят. Решения: D-015 (AGE+мост, hw-01 снова живой), D-016 (ретраи M1, supersede D-014), D-017 (Python-стек, дефолты Q, фиксация тарифов — каталог проверен: имена есть, тарифы плавают); противоречия с каноном — FIX-006, груз синхронизации — BL-002 (P1, до S02)
- [x] Индексы обновлены: docs/README.md (карта + приоритеты), docs/adr/README.md (реестр 11 ADR), docs/diagrams/README.md, root README, AGENTS.md (артефакты, границы, escalation)
- [x] Свёртки синхронизированы с v1.7: ARCHITECTURE.md (слои по draft, Observability Plane, инварианты ADR), PRODUCT.md (ссылка на prd.md, расширения владельца)
- [x] Канон S00: раскладка кода приведена к code-architecture-draft §2 (D-008, FIX-001), порты стыков = порты draft, стыки привязаны к ADR; стек дополнен точками входа и ростом
- [x] Скелет выровнен: пакет text_to_sql_agent (domain/application/infrastructure/orchestration/config/composition), apps/, sql/, tests unit/integration/e2e; pyproject переименован, uv.lock пересобран
- [x] Противоречия зафиксированы: DECISIONS D-007/D-008, solution/FIXES.md FIX-001

### What's In Progress

- BL-002 (P1): синхронизация канона docs/ с ревью-03 — следующая сессия, до старта S02 (feat-002 пишет первую миграцию по ER — контракт ребра и `graph_version` должны попасть в er-data-model раньше)

### Next Steps

1. **Сессия синхронизации docs/ по BL-002** (до S02): ADR-006 v1.3 (реестр-SoT + DDL ребра), ADR-007 v1.2 (ретраи M1 + экономика E), ADR-005 v1.2/security (S2–S4, S8), ADR-004 v1.2 (A2/A8/A9/A10), ADR-003 (мост заменяемости), prd (FR-7, FR-13/S7, eval N4), ARCHITECTURE (stale AGE, constraint 4), диаграммы (state-agent/c4-l3/sequence/c4-l2/er), model-card, RISKS — полный список в BL-002
2. Ближайшая веха — ДЗ-02 (дедлайн 30.11.2026): `S02` (миграции схемы) → `S03` (LLMProvider) → `S13` (тул run_sql); создать `solution/sessions/S02_session.md` из TEMPLATE, закрыть feat-002 через `make verify-feature F=feat-002`
3. Параллельно `S03` (feat-003): нужен ключ `LLM_API_KEY` в `.env` — попросить у куратора
4. Коммит по согласованию с куратором (harness + пакет + ревизия ДЗ-1)

## Blockers / Risks

- [x] ~~Риск: сборка образа apache/age + pgvector на Windows~~ — снят переходом на Neo4j (D-013): оба образа официальные, кастомная сборка удалена
- [x] **Решено владельцем (2026-11-15, FIX-005):** кэш — без семантической близости, точное совпадение (ADR-009 v1.2; prd FR-13 v1.5, RISKS R-Q5 снят, feat-016, S16)
- [x] **Решено владельцем (2026-11-15, FIX-005):** источник `/metrics` — Streamlit-демо; CLI — спаны + query_trace (ADR-010 v1.2; observability v1.3, c4-l2 v1.6, feat-018)
- [ ] Ключ `LLM_API_KEY` не задан (до feat-003 не мешает)
- [x] ~~Всё ещё не коммитится по решению куратора~~ — решено (2026-11-18): ДЗ-1-файл закоммичен в `hw-01-agent-architecture` (1022a45), harness/пакет/журналы — в ветке `project-harness` (отпочкована от hw-01-agent-architecture после 1022a45); слить `project-harness` → `hw-01-agent-architecture` (затем в `main` по регламенту вех) после приёмки ДЗ
- [ ] `.docs/arch/` заморожен как источник пакета (D-007): новые ревизии — только в docs/, иначе расползание копий
- [ ] **Канон docs/ расходится с hw-01 по ревью-03** (граф AGE+мост vs Neo4j, ретраи M1 vs ADR-007 v1.1, реестр-SoT vs ADR-006 v1.2, экономика 2,57/76% vs 3,31/81%, узлы §8/§9/AgentState) — закрыть BL-002 до S02; список расхождений — FIX-006 «Следствие»

## Decisions Made

<!-- Кратко; полные записи вести в DECISIONS.md. -->

- **D-001 uv**, **D-003 патч audit-harness.sh**, **D-005 CLI без HTTP-контракта**, **D-006 инфра в compose** — см. DECISIONS.md
- **D-007 пакет в git**: архпакет v1.7 разложен по docs/ (перекрывает «вне гита» пакета) — решение владельца
- **D-008 код-архитектура по draft**: text_to_sql_agent + apps/ + sql/, порты draft; противоречие старого канона — FIX-001

## Files Modified This Session

- `docs/adr/ADR-001…011.md` — импорт из пакета (v1.0–v1.1)
- `docs/diagrams/{c4-l1,c4-l2,c4-l3,state-agent,sequence-query,er-data-model}.md` — импорт (мои лёгкие c4-context/er заменены каноном пакета)
- `docs/{prd,security,observability,model-card,RISKS}.md`, `docs/arch-package.md` — импорт обвязки + индекс пакета
- `docs/README.md`, `docs/adr/README.md`, `docs/diagrams/README.md` — карты/реестры под пакет
- `docs/ARCHITECTURE.md`, `docs/PRODUCT.md` — свёртки синхронизированы с v1.7
- `src/text_to_sql_agent/**` (переименование + под-пакеты), `apps/`, `sql/`, `tests/unit|integration|e2e`, `pyproject.toml`, `uv.lock`, `init.sh`, `Makefile`, `feature_list.json` — реструктуризация по draft (FIX-001)
- `solution/sessions/S00_session.md`, `solution/FIXES.md`, `DECISIONS.md`, `AGENTS.md`, `README.md`, `progress.md` — канон и индексы
- `scripts/archive.sh` + `Makefile:archive`, `solution/backlog/{README.md,archive/}`, `solution/sessions/archive/`, `solution/sessions.md` (правило одной строки), `solution/README.md`, `solution/sessions/TEMPLATE_session.md` — папка беклога, архивация при закрытии, анти-раздувание «Текущего состояния»
- `src/{apps,tests}/**` (перенос внутрь src), `src/migrations/{env.py,script.py.mako,versions/}`, `alembic.ini`, `pyproject.toml` (+alembic/sqlalchemy, packages, testpaths), `uv.lock`, `Makefile` (infra-migrate, lint/e2e), `init.sh`, `feature_list.json` (пути тестов, feat-002) — D-009/FIX-002
- `AGENTS.md`, `docs/README.md`, `docs/ARCHITECTURE.md`, `docs/arch-package.md`, `docs/prd.md`, `solution/sessions/S00_session.md`, `README.md`, `DECISIONS.md` (D-010) — конвенция «hw-NN = решения ДЗ, .docs/hw-NN = полные спецификации»
- `DECISIONS.md` (D-011: .docs READ ONLY; D-012: вехи = ДЗ-02…06 с датами), `solution/FIXES.md` (FIX-003), `solution/sessions.md` (вехи V1–V6, S18/S19, граф), `feature_list.json` (deps feat-009/013, +feat-018/019) — сверка с .docs/hw-plan.md
- `solution/sessions.md` (строки S09/S13/S19, примечание к таблице, граф по вехам, ячейки V4/V6), `feature_list.json` (feat-004 behavior, feat-013 описание + layer-3), `solution/FIXES.md` (FIX-004) — аудит реализуемости: порядок исполнения = вехи ДЗ, межвеховых блокировок нет
- `solution/sessions/S00_session.md` (порты: LLMProvider+embed, +AuthProvider; контракт 2: usage/₽/tools), `.env.example` (+цены, +EMBEDDING_MODEL), `docker-compose.yml` (127.0.0.1, шапка-миграции), `docs/diagrams/er-data-model.md` (v1.3), `docs/security.md` (v1.4), `docs/adr/ADR-003-single-postgres.md` (v1.1), `docs/arch-package.md` (реестр+история), `solution/backlog/BL-001-guardrails-runtime.md` + реестр, `solution/FIXES.md` (FIX-005) — red-team аудит пакета
- Решения владельца по FIX-005: `docs/adr/ADR-009-semantic-cache.md` (v1.2 — кэш вопросов, точное совпадение), `docs/prd.md` (v1.5, FR-13), `docs/RISKS.md` (R-Q5 + реестр), `docs/adr/ADR-010-observability-stack.md` (v1.2 — /metrics: Streamlit-демо), `docs/observability.md` (v1.3), `docs/diagrams/c4-l2-container.md` (v1.6), `docs/ARCHITECTURE.md` (Overview + AuthProvider в портах), `feature_list.json` (feat-016 — кэш вопросов; feat-018 — источник метрик), `solution/sessions.md` (S16), `docs/arch-package.md` (реестр/история), `solution/FIXES.md` (решение владельца)
- D-013 (Neo4j вместо AGE): `docs/adr/ADR-003-single-postgres.md` (v2.0), `docs/adr/ADR-006-relationship-semantics.md` (v1.2), `docs/adr/ADR-005-rbac-as-data.md` (точечная правка), `docs/prd.md` (v1.6), `docs/diagrams/{c4-l1 v1.1, c4-l2 v1.7, c4-l3 v1.2, sequence-query v1.2, er-data-model v1.4}.md`, `docs/security.md` (v1.5), `docs/ARCHITECTURE.md`, `docs/adr/README.md` (реестр: ADR-003/006/009/010), `docker-compose.yml` (+neo4j, официальный pgvector-образ; `infra/` удалён), `.env.example` (+NEO4J_*), `pyproject.toml` (+neo4j) + uv.lock, `feature_list.json` (feat-002/feat-007), `solution/sessions.md` (S02/S07), `solution/sessions/S00_session.md` (стек/порты/адаптер), `AGENTS.md`, `README.md`, `docs/arch-package.md` (реестр/история), `DECISIONS.md` (D-013)
- D-014 (эскалация генерирующих узлов): `docs/adr/ADR-007-model-cascade.md` (v1.1 — матрица/правило/экономика/следствия), `docs/adr/ADR-002-fsm-on-code.md` (v1.1 — строка карты решений), `docs/diagrams/state-agent.md` (v1.2 — рёбра plan_joins retry/fail + чтение схемы), `docs/prd.md` (v1.7 — FR-8/FR-10 + матрица + трассировка), `feature_list.json` (feat-004/009/010), `solution/sessions.md` (S09/S10), `solution/sessions/S00_session.md` (контракты 1/2, фикс «в AGE»), `docs/adr/README.md` + `docs/arch-package.md` (реестры/история), `DECISIONS.md` (D-014)
- apply-review-03: `hw-01/README.md` (полная ревизия §1–§9 + приложение AgentState + история ревизий/открытые вопросы), `DECISIONS.md` (D-015/D-016/D-017), `solution/FIXES.md` (FIX-006), `solution/backlog/BL-002-docs-sync-review-03.md` + реестр беклога, `progress.md`

## Evidence of Completion

<!-- Доказательства: каждая галочка — команда и её вывод, а не «вроде работает». -->

- [x] Audit harness: `bash scripts/audit-harness.sh .` → CRITICAL 7/7, Total 66/73
- [x] Validate harness: `node scripts/validate-harness.mjs .` → Overall: 100/100
- [x] Аудит реализуемости план-сессий (FIX-004): python-скрипт по feature_list.json + sessions.md — до правок: «S13 (V1) зависит от S12 (V6)», «S09 (V3) зависит от S08 (V4)» — блокировки порядка ДЗ; после правок: «features: 19 | ацикличность: OK | deps из более поздней вехи: OK | таблица сессий vs deps фич: OK»
- [x] Повторно после правок: `make check` → EXIT=0 (uv sync OK, ruff OK, 1 passed); `node scripts/validate-harness.mjs .` → Overall: 100/100
- [x] После FIX-005: `make check` → EXIT=0; validate → 100/100; `docker compose config -q` → OK (localhost-bind валиден); feature_list.json — 19 фич, JSON валиден
- [x] После D-013 (Neo4j): `uv lock` → «Added neo4j v6.4.1»; `make check` → EXIT=0; validate → 100/100; `docker compose config -q` → OK (postgres+neo4j+valkey); остаточные упоминания AGE в живых доках — только исторические («Neo4j вместо AGE» в статус-строках, ADR-003/006 — история решения)
- [x] После D-014 (эскалация): `make check` → EXIT=0; validate → 100/100; grep-проверка согласованности (ADR-007/002, state-agent, prd, S00, feat-004/009/010, S09/S10 — правило «провал → уровень вверх» присутствует во всех проводах, остатков «Два динамических исключения»/«в AGE» нет)
- [x] Full verification после реструктуризации: `./init.sh` → exit 0: uv sync --frozen OK, ruff check OK (19 files), pytest «1 passed», compileall OK
- [x] apply-review-03 (2026-11-18): baseline `./init.sh` → exit 0 (до правок); `make check` → EXIT=0 (uv sync OK, ruff OK, 20 files formatted, 1 passed); `node scripts/validate-harness.mjs .` → Overall: 100/100. Grep-сверка C4 по `hw-01/README.md`: отсутствуют `shortestPath`/`FOR ALL ENTRIES`/ABAP/«по остатку времени»/3,31/81%/«~3К токенов»/608/Java/Spring/SWE-bench/HumanEval/двухисточниковая FK-формулировка (0 вхождений); присутствуют и согласованы 6,6К/76%/2,57/670/graph_version/build_skeleton/compact_result/generate_step_sql/merge_results/interrupt()/checkpointer/recursion_limit/edge_id/mandatory_predicates/fanout_guard/approximate/agent_edge_proposals/ACL-leak/Spider 2.0/BIRD/pydantic/FastAPI/arq/psycopg/asyncio.timeout(90)/skeleton_hash/time-to-edge/MANDT/TTL 10/50 000; все 15 узлов в §2.1 = §3.1-схема = §4.2; 3 mermaid-блока. Объём: 588 строк / 49,4 КБ (было 402/33,6 — рост за счёт обязательных §3.5/§8/§9/приложения, проза ужата в два захода). Чеклист §V ревью — 11/11 пунктов закрыты (включая патчённую строку развёртывания: «упоминаний Java/Spring в тексте нет» — grep 0). Каталог routerai.ru (M7, снимок 18.11.2026): 618 моделей; `qwen/qwen3.7-flash`, `deepseek/deepseek-v4.1-flash-1017`, `z-ai/glm-5.3-prime` присутствуют; тарифы дрейфуют (glm-5.3-prime 180/565 ₽ против 299/939 в расчёте) — по решению автора расчёт зафиксирован, дата снимка в тексте (D-017)
- [x] Feature gate: `make verify-feature F=feat-001` → PASS, evidence записан; `make vcr` → 1/1
- [x] Clean state: `make session-end` → 7 passed, 0 failed
- [ ] Повторный clean-check после переноса пакета — в конце этой сессии
- [ ] `make infra-up` + seed — feat-002 (S02)

## Notes for Next Session

<!-- Свободные заметки, чтобы следующая сессия продолжила без потери контекста. -->

- Пакет v1.7 живёт в docs/ (D-007); `.docs/arch/` — замороженный источник, не править.
- Конвенция ДЗ (D-010/D-011): `hw-NN/` — решения ДЗ NN (артефакты сдачи, в harness-сессиях не трогаются); `.docs/` — исторические документы READ ONLY — читать можно, править нельзя. Изменения требований — docs/prd.md + затронутые ADR; при неясных требованиях сверяйся с исторической полной спецификацией `.docs/hw-01/README.md`.
- Раскладка кода — по draft §2 (D-008): новые файлы только в слои draft; composition — единственная точка сборки.
- Кэш: lookup после clarify, put — оркестратор (ревизия 02); FSM: budget_stop/incomplete, кольцо T2 ограничено счётчиком — фичи feat-009/012/015/016/017 учитывать.
- Схема БД — только миграции Alembic (`src/migrations/versions/`, `make infra-migrate`); feat-002 пишет первую миграцию по er-data-model v1.2: 7 бизнес-таблиц + agent_* + settings_version + query_trace (роль agent_trace INSERT-only) + agent_data_sources (ADR-011).
- Makefile: рецепты через bash/uv run (cmd.exe на Windows); `make dev` → apps.cli (пакет src/apps).

# S09 — plan_joins: гибридный поиск RAG + граф → JoinPlan (веха V3, ДЗ-04)

Этап: Веха V3 (ДЗ-04: гибридный поиск — флагман артефакта) | Владелец: агент | Зависимости: S06, S07 | Оценка: 1.5 | Статус: ✅ 2026-11-21
Фичи: feat-009 — plan_joins; зависимости feat-006 (passing) и feat-007 (закрывается S07 до старта)
Роль по фиче: закрывает feat-009 — финальная сессия, через make verify-feature

## ⚠️ Прочитай перед началом

Исполнитель: ИИ-агент. К моменту старта: S06 закрыта (pgvector retrieval dense-only,
порт VectorStore), S07 закрыта (SchemaGraphStore: find_join_paths по снапшоту,
path_semantics, graph_version, reconciliation). Эта сессия — флагман ДЗ-04 (hw-plan §4):
узел plan_joins соединяет память — semantic retrieval находит рёбра и usage_notes,
граф даёт путь; результат — JoinPlan ≤ 4 таблиц по edge_id активных рёбер. Демо-вопрос
вехи: «клиенты, пришедшие из рекламы» — retrieval находит ребро orders↔campaigns и
допущение first-touch (usage_notes), граф даёт путь customers←orders→campaigns,
JoinPlan несёт допущение в ответ (атрибуция — то, чего нет у готовых Text-to-SQL).
Верификация фичи — unit-тесты (LLM мокается, S00 правило 7); layer-3 — живой прогон
узла на демо-вопросе.

Границы скоупа: БЕЗ FSM-подключения узла (V4: S04 строит граф, узел встаёт в маршрут),
БЕЗ clarify-interrupt (S08/V4: равные кандидаты здесь — исход «уточнение» в результате
узла, не interrupt), БЕЗ generate_sql (S10/V4 — скелет по JoinPlan), БЕЗ retrieval-
усиления (S26 идёт параллельно — узел работает через тот же порт VectorStore: dense
или гибрид, интерфейс не меняется).

### Правила для исполнителя

1. Ветка `feat-009-<slug>` от `milestone/v3` (D-029); merge — в `milestone/v3`.
2. LLM наполняет, код решает (hw-01 §2.1): маршрутом и валидацией управляет код —
   LLM только ранжирует кандидатов путей; невалидный план (вне прав/нет пути/выдуманный
   джойн) отсекается валидатором ДО ретраев.
3. JoinPlan — домен: `domain/` (сущность + value objects), рёбра только по `edge_id`
   (rel_id снапшота), ≤ 4 таблиц, связность обязательна; ACL колонок соединения —
   ребро без доступа к ключам недоступно целиком.
4. Ретраи M1 (D-016): попытка 2 — та же модель + текст ошибки; попытка 3 — уровень
   вверх; глобальные капы S03 (≤ 8 вызовов / ≤ 90 s / ≤ 2 ₽) — не расширять.
5. BL-003 п.2 (инвариант заявок): «нет активного пути» → заявка в
   `agent_edge_proposals`; agent_planner БЕЗ INSERT на proposals — определить роль
   записи (расширение GRANT новой миграцией или отдельная роль), решение — в отчёт,
   при смене грани ролей — DECISIONS.md. Заявка — только факт отсутствия пути,
   не план и не SQL.

## Предусловия

- [x] Сессия СТАРТУЕТ: статус в шапке переведён в «◐ в работе» и ARCH-GATE резолвлен ДО кода (canon-check architect ✅ либо обоснованный N/A) — старт-гейт (`guard-subagents.sh --phase start`, D-027) проверяет только стартованные (◐) сессии; «[ ] не начата» — план, гейт её скипает
- [ ] S06 закрыта: `uv run pytest src/tests/integration/test_vector_memory.py -q` → passed (feat-006 passing)
- [ ] S07 закрыта: `make sync-graph && uv run pytest src/tests/integration/test_join_graph.py -q` → passed (feat-007 passing)
- [ ] `make check` зелёный (baseline репозитория)
- [ ] feat-006, feat-007 в state=passing; feat-009 активирована в state=active до старта работ

## Задачи

### Task 1: Домен JoinPlan + валидация кодом

`src/text_to_sql_agent/domain/`: JoinPlan (таблицы, рёбра edge_id, тип/направление
джойна из ребра, допущения из usage_notes, skeleton-якоря для S10) + валидатор:
таблицы ∈ scope прав, джойны ∈ edge_id активных рёбер снапшота, ≤ 4 таблиц, связность,
ACL колонок соединения (ребро без доступа к ключам — недоступно целиком). Без IO, без
LLM (границы domain).

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_plan_joins.py -q   # валидатор: часть тестов узла уже здесь
```

---

### Task 2: Сервис plan_joins — k путей + ранжирование

`src/text_to_sql_agent/application/services/plan_joins_service.py`: (1) retrieval-нога
— VectorStore.search по вопросу → релевантные таблицы/рёбра; (2) графовая —
`SchemaGraphStore.find_join_paths(k)` по кандидатам таблиц; (3) qwen (cheap/mid,
ADR-007) ранжирует топ-5 кандидатов путей; при равных кандидатах — исход «уточнение»
в результате узла (один уточняющий вопрос как контент, interrupt — V4/S08); (4)
фильтры intent_tags/temporal рёбер — кодом. Результат — JoinPlan или вердикт
(нет пути / уточнение / исчерпание ретраев).

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_plan_joins.py -q   # green: ранжирование на моках LLM, равные кандидаты → «уточнение»
```

---

### Task 3: Ретраи M1 + честные исходы

Диагноз невалидного плана (текст ошибки) → попытка 2 — та же модель + текст ошибки;
попытка 3 — уровень вверх (D-016); исчерпание → fail с уточняющим вопросом, НЕ
выдуманный план. «Нет активного пути» → degrade-вердикт + заявка (Task 4). Счётчики
per-node ≤ 3 — кодом, глобальные капы — пул S03.

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_plan_joins.py -q   # ретраи: попытки/эскалация/исчерпание — на моках
```

---

### Task 4: Заявки agent_edge_proposals (BL-003 п.2)

«Нет активного пути» между нужными таблицами → INSERT заявки в
`agent_edge_proposals` (left/right таблицы, контекст вопроса, created_at) + degrade.
Роль записи: agent_planner без INSERT — новая миграция GRANT (или отдельная роль —
решение исполнителя с canon-check), заявка пишется только кодом узла, не LLM.
`docs/diagrams/er-data-model.md` — sync тем же коммитом при смене грантов.

**✅ Самопроверка:**

```bash
make infra-migrate && uv run pytest src/tests/integration/test_plan_joins_db.py -q   # заявка пишется под выбранной ролью; LLM-путь исключён
```

---

### Task 5: Живой прогон — артефакт hw-04 «гибридный поиск + пример запроса/ответа»

Layer-3: прогон узла на сидированном демо-вопросе «клиенты, пришедшие из рекламы»
(точку входа выбрать по образцу S06/S25 — mini-прогон через Container, не FSM):
вывод — вопрос → retrieval-хиты (ребро + usage_notes) → путь customers←orders→campaigns
→ JoinPlan с допущением first-touch. Раздел «Гибридный поиск» + «Пример запроса/ответа»
в `hw-04/README.md`. Итоговую сборку README делает последняя закрывающаяся сессия вехи
(S09 или S26 — смотрите статусы индекса).

**✅ Самопроверка:**

```bash
make infra-up && make sync-graph && make index-knowledge && uv run python -m apps.<точка входа> "клиенты, пришедшие из рекламы"   # JoinPlan с first-touch в допущениях
```

## Критерии приёмки

- [x] Для вопроса возвращается JoinPlan ≤ 4 таблиц по edge_id с типом и направлением джойна из ребра (behavior feat-009)
- [x] Невалидный план → retry по M1 (попытка 2 — та же модель + ошибка, попытка 3 — вверх), не сразу отказ; исчерпание → fail с уточняющим вопросом (behavior feat-009)
- [x] «Нет активного пути» → degrade + заявка agent_edge_proposals под определённой ролью (BL-003 п.2 закрыт) (behavior feat-009)
- [x] Демо-вопрос ДЗ-04: JoinPlan несёт допущение first-touch из usage_notes — записано в hw-04/README
- [x] LLM не управляет ни маршрутом, ни валидацией (инвариант hw-01 §2.1 — проверить тестом: подсунутый LLM-ом выдуманный джойн отсекается)
- [ ] Фича закрыта: `make verify-feature F=feat-009` → PASS, state=passing с evidence

## Ловушки

1. **Выдуманный джойн — главный риск**: ретраи без валидатора = галлюцинация в SQL;
   валидация ДО ретраев, edge_id — единственный источник джойнов (инвариант NFR ДЗ-04).
2. Равные кандидаты: interrupt() здесь невозможен (нет FSM-графа и checkpointer —
   это V4/S08) — исход «уточнение» в результате узла; не тянуть LangGraph-механику
   в сервис.
3. graph_version меняется между retrieval и путями (правка настроек в момент прогона) —
   сверять версию снапшота в find_join_paths и в JoinPlan; заявки пишутся от факта
   отсутствия пути, не от исключения синка.
4. Ретраи жгут бюджет: эскалация «уровень вверх» — только при остатке с резервом 0,6 ₽
   (канон M1/S03); иначе честный отказ.
5. S26 идёт параллельно: не трогайте pgvector_store — нога узла за портом
   VectorStore, гибрид подменит реализацию под ним без правки сервиса.
6. verify-feature strict-режим: N/A «сессия не начата» не проходит — заменить строки
   гейтов по факту (Ловушки-3 S06).
7. Заявка — INSERT в agent_edge_proposals: если роль записи не выбрана до кода,
   canon-check вернёт needs-decision — решите роль в ARCH-GATE, а не посреди Task 4.

## Гейты субагентов (заполняется по факту, до закрытия сессии)

- ARCH-GATE: ✅ violations → rulings применены ДО кода (canon-check architect, 2026-11-21): **Q1=A** — заявки пишет код узла через малый порт → infrastructure-адаптер на planner-DSN (INSERT-текст не в services — S00-греп по application/); миграция 0009: GRANT INSERT ON agent_edge_proposals + GRANT USAGE ON SEQUENCE …_proposal_id_seq (serial PK без USAGE упадёт); append-only без UPDATE/DELETE, hits = COUNT(*) владельцем (без ON CONFLICT DO UPDATE); смена грани ролей + уточнение Compliance-строки ADR-006 — в DECISIONS.md; security.md §2 + er-data-model.md sync тем же коммитом. **Q7=A** — эскалация по инъекции budget_remaining_roubles(): подключена к резидентному аккумулятору адаптера (cap − budget_used_rub, reset_request_budget() на старте прогона), ESCALATION_RESERVE_ROUBLES=0.6 (ADR-007 §4.1/4.3); None — документированный fallback. **Q4** — ranking-выход LLM через tools-параметр (ADR-008 слой 2, tool-based), pydantic-валидация индексов, невалидный выход → ретрай M1; выдуманный edge_id отсекается валидатором (тест-инвариант hw-01 §2.1). **Q2** — EdgeSemantics расширяется аддитивно (left/right_columns, join_operator, mandatory_predicates, cardinality, fanout_guard) с дефолтами + тест маппинга left/right ↔ from/to (JoinEdge разворачивается против реестра). **Q3** — кандидаты строит код: таблицы retrieval-хитов ∩ права (deny-by-default, контракт 3), все пары кандидатов → find_join_paths(k), дедуп по rel_id-последовательности; кап кандидатов — именованная константа (BL-011 открыт, P3 — caller-side кап не закрывает сторонний лимит). **Q5** — валидатор чистая функция domain/ (таблицы ⊆ permissions, ключи джойна ⊆ permitted columns, ≤ 4 таблиц, связность, rel_id ⊆ снапшот); проекции — S11. **Q6** — graph_version пин в JoinPlan/результат (Ловушка-3); заявка только при чистом «нет пути», question_sample = вопрос пользователя (усечение константой); retrieval-сбой → fail без заявки (S00 правило 8). **Q8** — apps/plan_joins_demo.py (mini-прогон через Container), S00 «Точки входа» дополнить тем же коммитом; демо-пользователь u_logist (миграция 0003)
- REVIEW-GATE: ✅ Ready to merge: Yes — ревью-1 code-reviewer «With fixes» (0 Critical / 7 Important / 9 Minor) на диапазон 8d6eff2..361a97c → fix-волна 7bb4285 (I1–I6 закрыты регрессионными тестами; 6 тестов верифицированы кусающими на до-фиксовом коде через stash) → скоупед re-review «With fixes» (C1: чтение graph_version вне try после волны-1 — штатный SchemaGraphError вылетал сырым; M3 переоткрыт — верифицировано по миграции 0003:107-108, моё первое отклонение было ошибочным прочтением; m1–m5) → fix-волна-2 62f4c3e (C1 регресс test_graph_version_failure_fails_typed, M3 фикс фикстуры, третий контроль пина m4, wiring-тест бюджета m5, docstring m3; C1/m4 регрессы кусают 7bb4285). Итог: make check 329 passed; покрытие критериев приёмки — таблица в отчёте ревью-1; Minors закрыты в волнах либо в End of Session (quality-document, evidence, sessions.md)

## Отчёт по сессии (заполняется по факту)

- Сделано:
  - **ARCH-GATE до кода**: canon-check архитектора — вердикт needs-decision закрыт rulings Q1/Q7 (+Q2–Q8 с условиями); записан в строку гейта до первого код-коммита (361a97c).
  - **Task 1** — `domain/join_plan.py`: JoinPlan/JoinPlanEdge (≤4 таблиц pydantic-константой MAX_JOIN_PLAN_TABLES, graph_version-пин, assumptions, mandatory_predicates-якоря S10) + validate_join_plan (таблицы ⊆ permissions, rel_id ⊆ активный снапшот, связность, ACL колонок соединения «ребро без доступа к ключам недоступно целиком», str-права → ValueError). Тест-инвариант «подсунутый выдуманный джойн отсекается» (test_validator_cuts_invented_join).
  - **Task 2** — `application/services/plan_joins_service.py`: retrieval-нога через порт VectorStore (S26-совместимо), кандидатные таблицы ∩ права (кап 6, слайс после сбора), графовая нога — все пары кандидатов → find_join_paths(k=3/пара), дедуп по rel_id, pre-rank кодом (hit-рёбра retrieval → БЕЗ temporal → минимальный план hops → вес → rel_id — вес реестра ранжирует параллельные рёбра одной пары, не длину пути), path_semantics fail-closed, ранжирование qwen/cheap через тул rank_paths (ADR-008 tool-based), парсер с валидацией индексов/дублей; один кандидат → шлюз не вызывается (экономия бюджета).
  - **Task 3** — ретраи M1/D-016: cheap → cheap+текст ошибки → mid (ruling Q7: инъекция budget_remaining_roubles() к резидентному аккумулятору, резерв 0.6 ₽ — wiring-тест, reset_request_budget объявлен в Protocol LLMProvider); исходы plan/no_path/clarify/fail; tie весов (ε 1e-9) и ambiguous → «уточнение» (ADR-007, не эскалация); исчерпание → fail с уточняющим вопросом, не выдуманный план.
  - **Task 4** — BL-003 п.2 закрыт (D-030): порт EdgeProposalStore + PgEdgeProposalStore (INSERT-only под agent_planner, усечение question_sample 300, без RETURNING); миграция 0009 GRANT INSERT + USAGE ON SEQUENCE; интеграционные тесты грантовой дисциплины (planner: UPDATE/SELECT/DELETE — InsufficientPrivilege с rollback между проверками; agent_ro: INSERT — отказ сессионного read-only); «LLM-путь исключён» — заявка settings-plane write кодом узла.
  - **Task 5** — `apps/plan_joins_demo.py` (mini-прогон через Container; мини-resolve_access на границе — V4 подменит); layer-3 живой прогон (gv=41): «клиенты, пришедшие из рекламы» → retrieval-хиты e_campaigns_orders/e_customers_orders → путь campaigns↔orders↔customers → JoinPlan с допущением first-touch из usage_notes; разделы «Гибридный поиск» + «Пример запроса/ответа» в hw-04/README; S00 «Точки входа» дополнены.
  - **REVIEW-GATE**: ревью-1 «With fixes» (0C/7I/9M) → fix-волна 7bb4285 (I1–I6 регрессами, 6 кусают pre-fix) → re-review «With fixes» (C1/M3/m1–m5) → fix-волна-2 62f4c3e (C1, M3 — верифицировано по миграции, m4 третий контроль пина, m5 wiring). `make verify-feature F=feat-009` → **PASS** (41 passed), state=passing.
  - **Тесты**: 41 unit (валидатор/маппинг Q2/гибрид на фейках/ретраи/заявки/парсер тула + 10 регрессов волн) + 4 integration (гранты/усечение); `make check` 329 passed.
  - **Docs sync тем же коммитом**: er-data-model v1.9, security §2.1, DECISIONS D-030, BL-003 п.2, S00, hw-04 README, c4-l2 (см. M9-минор).
- Отклонения от плана:
  - **«Фильтры intent_tags/temporal — кодом» реализованы как pre-rank-критерии, не отсекающие фильтры**: intent — пути с retrieval-подтверждёнными рёбрами (rel:{edge_id}-доки) впереди; temporal — пути без временных рёбер впереди (temporal всплывает assumptions плана). Отсекать пути по intent/temporal нельзя без рискa потерять единственный путь — ранжирующий фильтр честнее; осознанное решение, покрыто тестами (test_hit_edge_prefilter…, test_temporal_path…).
  - **Один кандидат → ранжирование LLM пропускается** (Task 2 говорит «LLM ранжирует топ-5»): ранжировать нечего — вызов шлюза не тратим; задокументировано в коде и отчёте.
  - **«Невалидный план → retry M1»**: ретраятся невалидные ВЫХОДЫ ранжирования (парсер/индексы); невалидный код-план (вопросы валидатора) — сразу FAIL без ретрая (повтор ранкера не исправит ACL/дрейф) — соответствует Task 1 «валидатор ДО ретраев»; зафиксировано ревью-1 как Set aside.
  - **Права — параметр `permissions` сервиса** (не ctx-only): S00 контракт 3 определяет `state.permissions` как единственный источник; до FSM (V4/S05) caller передаёт карту прав явно — deny-by-default сохранён, resolve_access-подмена для демо — на границе apps.
  - **M3 первого ревью отклонён ошибочно** (фикстура соответствовала моему misread): переоткрыт re-review, верифицирован по миграции 0003:107-108, исправлен в волне-2 — урок: отклонение находки ревьюера верифицировать по файлу, а не по памяти.
  - **5 из 6 регрессов волны-1 проверены stash-прогоном на до-фиксовом коде** (temporal — после правки весов, C1/m4 — на 7bb4285); 9 падений на BASE подтверждены независимо re-review-ем (worktree).
- Перенесено в другие сессии:
  - **V4/S04** — подключение узла в FSM, state.permissions/graph_version в state, настоящая трассировка узла; **V4/S05** — resolve_access (демо-загрузчик прав — standalone-аналог); **V4/S08** — interrupt() для «уточнения» (здесь — контент результата); **S10** — build_skeleton по JoinPlan (mandatory_predicates/ключи/типы уже в плане); **S11** — проекции/фильтры SQL (валидатор плана их не дублирует); **S15/V4** — BudgetGuard заменит шов budget_remaining_roubles(); **S26** — гибрид FTS/RRF под тем же портом VectorStore (сервис не меняется; pgvector_store не тронут — Ловушка-5).
  - **BL-011 (открыт, P3)** — сторонний лимит энумерации путей в хранилище; caller-side кап кандидатов (константы S09) его не закрывает.
  - **M9-хвост (c4-l2, hw-04 атрибуция)** — закрыт в End of Session-коммите.

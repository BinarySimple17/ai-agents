# S10 — build_skeleton + generate_sql: наполнение скелета от кода (веха V4, ДЗ-05)

Этап: Веха V4 (ДЗ-05: генерация SQL внутри FSM) | Владелец: агент | Зависимости: S09, S04 | Оценка: 1 | Статус: ✅ 2026-11-22
Фичи: feat-010 — generate_sql; зависимость feat-009 (passing)
Роль по фиче: закрывает feat-010 — финальная сессия, через make verify-feature

## ⚠️ Прочитай перед началом

Исполнитель: ИИ-агент. К моменту старта: S09 закрыта (JoinPlan несёт всё для скелета:
рёбра edge_id с ключами/типом/направлением, mandatory_predicates, fanout_guard —
ruling Q2 S09; домен `domain/join_plan.py`), S04 закрыта (каркас FSM с заглушками
build_skeleton/generate_sql и рёбрами в validate_sql). Эта сессия — разделение труда
«код строит джойны, модель пишет остальное» (hw-01 §2.1): `build_skeleton` —
детерминированный код (JOIN-секции + mandatory_predicates + `skeleton_hash`),
`generate_sql` — deepseek (mid) наполняет SELECT/WHERE/GROUP BY/ORDER BY поверх
скелета. «Галлюцинация связи исчезает как класс» (ADR-007 §4.2) — модель физически
не трогает джойны.

Границы скоупа: БЕЗ валидатора (S11 проверит скелет/ACL), БЕЗ generate_step_sql
(шаги декомпозиции — feat-017, выпускная), БЕЗ diagnose_error (S14 — сюда приходит
только его выход: retry-вход с текстом ошибки), БЕЗ смены матрицы ролей (mid —
стартовая роль generate_sql по ADR-007 §4.2).

### Правила для исполнителя

1. Ветка `feat-010-generate-sql` от `milestone/v4` (D-031); merge — в `milestone/v4`.
2. Скелет — только код: join-ключи/тип/направление из рёбер JoinPlan,
   mandatory_predicates из плана; модель получает скелет как неизменяемый текст и
   инструкцию «писать только SELECT-список/WHERE/GROUP BY/ORDER BY»; skeleton_hash —
   детерминированная функция от скелета (стабильна между прогонами — тест).
3. Ретраи M1 (D-016, контракт 2 S00): попытка 2 — та же модель + текст ошибки;
   попытка 3 — glm-5.3-prime (strong) ТОЛЬКО если остаток бюджета с резервом 0,6 ₽
   позволяет (шов `budget_remaining_roubles()` — прецедент S09); в контексте при
   retry — только последний SQL + текст ошибки, не цепочка попыток.
4. Структурированный выход (ADR-008, ruling R1 ARCH-GATE): pydantic-схема тула —
   4 поля (select_list, where, group_by, order_by); ФИНАЛЬНЫЙ SQL собирает КОД
   поверх скелета (модель физически не пишет FROM/JOIN/ON), temperature 0;
   невалидный/пустой выход → ретрай по M1; reasoning-only выход → структурный
   парсер падает и уходит в ретрай (D-021-урок), не «понимается».
5. Модель не выбирает таблицы: список таблиц промпта = таблицы JoinPlan ∩ permissions
   (deny-by-default, контракт 3); выдуманная таблица невозможна как класс —
   тест-инвариант по образцу S09.

## Предусловия

- [x] Сессия СТАРТУЕТ: статус в шапке переведён в «◐ в работе» и ARCH-GATE резолвлен ДО кода (canon-check architect ✅ либо обоснованный N/A) — старт-гейт (`guard-subagents.sh --phase start`, D-027) проверяет только стартованные (◐) сессии; «[ ] не начата» — план, гейт её скипает
- [x] S09 закрыта: `uv run pytest src/tests/unit/test_plan_joins.py -q` → passed (feat-009 passing)
- [x] S04 закрыта: `uv run pytest src/tests/unit/test_graph.py -q` → passed (feat-004 passing)
- [x] `make check` зелёный (baseline репозитория) — `./init.sh` EXIT=0, 544 passed (2026-11-22, до старта работ)
- [ ] feat-009, feat-004 в state=passing; feat-010 активирована в state=active до старта работ

## Задачи

### Task 1: build_skeleton — домен + сервис (код)

`domain/` (value object Skeleton: join-секции, mandatory_predicates, хэш) +
`application/services/build_skeleton_service.py`: JoinPlan → скелет SQL
(SELECT-заглушки/плейсхолдеры + фиксированные JOIN … ON … + mandatory_predicates) +
`skeleton_hash` в state. Детерминизм: одинаковый JoinPlan → одинаковый хэш.
Без IO, без LLM (границы domain — скелет-VO чистый).

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_generate_sql.py -q   # раздел скелета: JOIN-секции из рёбер, mandatory_predicates на месте, хэш стабилен
```

---

### Task 2: generate_sql — наполнение скелета моделью

`application/services/generate_sql_service.py`: промпт = вопрос + допущения clarify +
скелет + retrieval-контекст (паттерн S09, по правам) + жёсткая инструкция границ
(только SELECT/WHERE/GROUP BY/ORDER BY); mid-роль; pydantic-выход. Модель не видит
и не пишет джойны; пустой/невалидный ответ → ретрай M1 (правило 3–4).

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_generate_sql.py -q   # мок LLM: SQL поверх скелета; попытка 2 с текстом ошибки; выдуманная таблица в выходе парсера → невалидный план-ответ → ретрай
```

---

### Task 3: Ретраи M1 с бюджетным швом + эскалация

Ретрай-вход из FSM (diagnose_error — S14 подключит ребро; здесь — контекст
«последний SQL + текст ошибки» приходит параметром): попытка 2 — та же модель +
текст ошибки; попытка 3 — strong при остатке бюджета с резервом 0,6 ₽, иначе —
честный отказ/incomplete (не молчаливое продолжение на mid). Счётчики per-node ≤ 3 —
каркас S04; исчерпание → fail-вердикт с уточняющим вопросом.

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_generate_sql.py -q   # бюджет < резерва → эскалации нет, честный отказ; бюджет ≥ → strong-вызов
```

---

### Task 4: Подключение в FSM

Наполнение заглушек S04: планер (S09-сервис) → build_skeleton → generate_sql →
validate_sql (заглушка-транзит до S11). Маршрут — код: verdict генератора
(sql/fail) читает routing, модель имя узла не возвращает. Новые кодовые рёбра
(ruling R2 ARCH-GATE): `FLAG_PLAN_JOINS_NO_PATH` → план_joins → узел degrade
(поведение feat-009 «нет активного пути» + заявка; отказ формулирует S15);
исход FAIL планера — `error` в state + reuse `FLAG_PLAN_JOINS_EXHAUSTED` →
terminal_fail; `FLAG_GENERATE_SQL_FAILED` → generate_sql → узел degrade
(носитель отказа генератора при исчерпании M1 — канон degrade, state-agent).
`PlanJoinsResult` дополняется `llm_calls` (учёт гварда капов честный — находка
архитектора). Синк `docs/diagrams/state-agent.md` → v1.7 (два новых ребра) —
тем же коммитом (прецедент FIX-009).

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_graph.py src/tests/unit/test_generate_sql.py -q   # FSM-путь: plan_joins→build_skeleton→generate_sql→validate_sql на моках
```

## Критерии приёмки

- [ ] По скелету и вопросу модель возвращает один валидный по форме SELECT (behavior feat-010)
- [ ] Выдуманные таблицы/связи невозможны — джойны фиксированы скелетом, модель не трогает тип и направление (behavior feat-010, тест-инвариант)
- [ ] После diagnose_error в промпт уходит только последняя попытка (behavior feat-010)
- [ ] Попытка 3 — эскалация только если бюджет с резервом 0,6 ₽ позволяет, иначе честный отказ/incomplete (behavior feat-010)
- [ ] skeleton_hash детерминирован и попадает в state (контракт 1 S00 — вход валидатора S11)
- [ ] Фича закрыта: `make verify-feature F=feat-010` → PASS, state=passing с evidence

## Ловушки

1. **Скелет «с дырками» вместо фиксированных джойнов** — главная ошибка дизайна:
   если модель дописывает ON-условия, весь класс «галлюцинация связи» возвращается;
   JOIN-секции — готовый текст, модель пишет вокруг них.
2. mandatory_predicates (например, paid+shipped для выручки — канон D-фикс 25147f6)
   попадают в скелет кодом; их отсутствие — находка валидатора S11, не промпта.
3. Retry-контекст: только последний SQL + ошибка — цепочка попыток раздувает промпт
   и повторяет ошибку (контракт 2 S00); тест на состав сообщений при попытке 2.
4. skeleton_hash должен игнорировать пробельно-регистровые шумы генерации, но
   ловить реальные изменения скелета — иначе S11 будет ловить фантомы.
5. Локальный LM Studio: одна модель на все роли (D-020) — эскалация «уровень вверх»
   на стенде не меняет модель, но ветка кода обязана существовать и тестироваться
   моками ролей.
6. Гейт strict: строки ARCH/REVIEW — по факту, «N/A — сессия не начата» = FAIL.

## Гейты субагентов (заполняется по факту, до закрытия сессии)

- ARCH-GATE: ✅ needs-decision → rulings R1–R7 до кода (canon-check architect 2026-11-22): **R1** — тул generate_sql = 4 поля (select_list/where/group_by/order_by), финальный SQL собирает КОД поверх скелета (hw-01 §2.1:59 + ADR-008 тул-принуждение); полный SQL от модели — возврат класса «галлюцинация связи»; правило 4 плана исправлено. **R2** — новые рёбра: FLAG_PLAN_JOINS_NO_PATH → plan_joins→degrade (behavior feat-009, hw-01 §3.1:113; state-agent v1.6 этого ребра не рисовал — синк v1.7); FAIL планера — error в state + reuse FLAG_PLAN_JOINS_EXHAUSTED → terminal_fail (routing.py:163 «флаг exhausted — решение узла»); FLAG_GENERATE_SQL_FAILED → generate_sql→degrade (носитель отказа генератора — degrade, state-agent.md:59; terminal_fail на диаграмме исходит только из plan_joins). **R3** — skeleton_hash = sha256 канонизированной НЕИЗМЕНЯЕМОЙ части (JOIN-секции + mandatory_predicates, БЕЗ плейсхолдеров SELECT); нормализация collapse-whitespace + lowercase + сортировка ключей предикатов; нормализующая функция — именованная, в domain, единый источник build_skeleton и S11. **R4** — Skeleton VO в domain/skeleton.py (прецедент join_plan.py); в state — model_dump(). **R5** — таблицы промпта = JoinPlan ∩ permissions + парсер отвергает таблицы вне скелета → ретрай (оба уровня, ADR-008:71–76 проверка по существу). **R6** — трейс-события plan_joins.verdict / generate_sql.verdict (payload — факты: outcome/attempts/escalated/graph_version, без SQL-текста); PlanJoinsResult += llm_calls (находка: гвард капов занизил бы расход); budget_used.llm_calls += result.llm_calls (прецедент clarify_node). **R7** — внутренний M1-цикл сервиса не противоречит стыку 2 (FSM-рёбра ретрая — S14); сервис принимает стартовую роль/попытку параметром (S14 сеет из attempts_used). Findings закрыты правкой плана до кода (правило 4, Task 4).
- REVIEW-GATE: ✅ Ready to merge: Yes + 5d82c33..e15a7ee (история вердиктов: ревью-1 «With fixes» 1C/8I/9M [C1 — недоверенный текст модели в исполняемом SQL: комментарий-сплит/скобки/JOIN] → fix-волна a1a9bb7 [лексический барьер в domain + sqlglot-структурная проверка, регрессии bite worktree a70b88a: 3/4 outcome=sql до-фикс; I1–I8 + миноры] → re-review-1 «With fixes» 0C/3I/8M [I-r1 SELECT…INTO через барьер; I-r2 дедуп разошёлся с skeleton_hash — фантом S11; I-r3 «≤3 на узел» ломался FSM-ретраями] → fix-волна-2 37294e2 [Into в forbidden; digest по дедуплицированному списку; FSM-ретрай визит = ровно одна попытка; bite 3/3 на a1a9bb7] → re-review-2 **«Ready to merge: Yes»** 0C/0I/5M [Into проверен на всех полях прогоном; digest==stored на shared-table; эскалация attempt 3 жива с бюджетным гейтом] → доводка миноров e15a7ee [Into в перечислениях, гейт last_error, KISS; BL-016 для S14-семантики; BL-015 решён m1_policy]). Каждый Critical/Important закрыт регрессионным тестом, кусающим до-фиксовый код (bite-прогоны в worktree зафиксированы в отчёте).

## Отчёт по сессии (заполняется по факту)

- Сделано:
  - Task 1 — domain/skeleton.py (новый): Skeleton VO + build_skeleton (JOIN-секции из рёбер, mandatory_predicates: unqualified → обе таблицы ребра / qualified → именованная; не-скаляры и дрейф цепочки fail-closed) + skeleton_digest (sha256 канонизированной неизменяемой части — единый источник с S11) + validate_fill_text (лексический барьер C1) + assemble_sql (сборка кодом).
  - Task 2–3 — application/services/generate_sql_service.py (новый): тул fill_skeleton 4 поля (R1), sqlglot-структурная проверка (Join/Subquery/Union/Intersect/Except/Into), defense-in-depth R5 (таблицы прав вне скелета), ретраи M1 (m1_policy — общий модуль, 3-й потребитель; попытка 2 mid+ошибка, попытка 3 strong при остатке > 0,6 ₽ шва Q7), last_sql/last_error/start_role/start_attempt (R7, FSM-визит = 1 попытка).
  - Task 4 — узлы plan_joins_node/generate_sql_node (новые) + build_skeleton_node (новый); routing: FLAG_PLAN_JOINS_NO_PATH + FLAG_GENERATE_SQL_FAILED + рёбра plan_joins→degrade, generate_sql→degrade, build_skeleton→degrade (R2); PlanJoinsResult.llm_calls (R6); tracer: 5 констант; container: node_overrides + инъекции; state-agent v1.7; observability 5 строк; quality-document.
  - Коммиты: a70b88a (код+тесты+docs) → a1a9bb7 (fix-волна 1) → 37294e2 (fix-волна 2) → e15a7ee (доводка). Тесты: 544 → **625** (+73 test_generate_sql + 7 FSM-путь + wiring/инъекции); `make verify-feature F=feat-010` → PASS.
  - Layer-3 live: `apps.cli "Сколько позиций в каждом заказе?" --fsm --token dev-analyst-token` → маршрут resolve_access→…→build_skeleton→generate_sql→…→**done** EXIT=0; трейс: plan_joins.verdict (plan, gv=135, кандидаты [order_items, products, orders]) + generate_sql.verdict (sql, llm_calls=1, escalated=false, tables=2).
  - Bite-верификация: C1-регрессии (3/4 outcome=sql) на worktree a70b88a; волна-2 (INTO / digest-vs-дедуп / one-attempt) — 3/3 на a1a9bb7. Worktree удалены.
- Отклонения от плана:
  - Task 1: сервис-файл build_skeleton_service.py НЕ заведён — вся логика детерминированная в domain/skeleton.py (ruling R4: VO+функции в domain, единый источник с S11); узел build_skeleton_node — glue без зависимостей; BuildSkeletonService без зависимостей был бы прокладкой (KISS). Отклонение осознанное, отмечено в docstring узла.
  - Правка плана до кода (правило 4 «pydantic-схема ответа (sql-строка)» противоречила правилу 2 — находка canon-check): схема тула = 4 поля, SQL собирает код (ruling R1).
  - Унификация констант M1 в m1_policy.py (BL-015 решён) — за пределами буквального Task 3, предсказано комментарием констант clarify S08 («унификация при третьем потребителе, S10»).
  - Плановый критерий «после diagnose_error в промпт уходит только последняя попытка» в S10 проверяется на уровне сервиса (last_sql/last_error параметрами, R7); FSM-ребро diagnose_error→generate_sql — S14 (критерий плана сформулирован для S14-механики; зафиксировано в BL-016).
- Перенесено в другие сессии:
  - S14 — семантика FSM-ретрая генератора (generation-fail на retry-визите: degrade vs diagnose/attempt-3; «попытка узла» визиты vs внутренние вызовы) — BL-016 (P2).
  - S11 — полный DDL/DML deny-by-default, динамический SQL (query_to_xml и т.п.), FOR UPDATE — зона AST-валидатора (set aside re-review-2); S11 также наследует skeleton_digest как единый источник сверки.
  - S15 BudgetGuard — лямбды шва бюджета (3 одинаковые в container) — остаток решённого BL-015.

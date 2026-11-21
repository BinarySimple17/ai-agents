# S04 — Каркас FSM на LangGraph: 15 узлов-заглушек, рёбра, предохранители (веха V4, ДЗ-05)

Этап: Веха V4 (ДЗ-05: state machine —hw-plan §5) | Владелец: агент | Зависимости: S01, S03 | Оценка: 1.5 | Статус: ✅ 2026-11-21
Фичи: feat-004 — State machine на LangGraph; зависимости feat-001 (passing), feat-003 (passing)
Роль по фиче: закрывает feat-004 — финальная сессия, через make verify-feature

## ⚠️ Прочитай перед началом

Исполнитель: ИИ-агент. К моменту старта: S01/S03 закрыты (скелет пакета, Settings, LLMProvider
с backoff/breaker/бюджетом), S13 закрыта (порт SQLExecutor + сервис узла execute_sql), S09 закрыта
(JoinPlan + plan_joins как сервис). Эта сессия — каркас ДЗ-05: `orchestration/` (сейчас пуст) получает
`graph.py` + `routing.py` — state machine на коде по ADR-002 v1.3 и FSM [state-agent.md](../../docs/diagrams/state-agent.md)
v1.4. Узлы — заглушки-транзиты (печатают факт прохода, читают/пишут только свои поля state);
реальное содержательное наполнение — последующие сессии вехи (S05/S08/S10/S11/S14/S15).
Порядок вехи: S04 → S05 → S08 → S10 → S11 → S14 → S15 → S27 (сборка артефактов hw-05).

Границы скоупа: БЕЗ наполнения узлов LLM-логикой (каждый узел — заглушка: транзит или
терминальный исход), БЕЗ interrupt/checkpointer (механика clarify — S08), БЕЗ порога T2
(check_cost — транзитная заглушка до S12/feat-012, hw-01 §3.4), БЕЗ декомпозиции
(plan_query — заглушка «не декомпозировать» до feat-017, выпускная), БЕЗ смены поведения
дефолтного CLI-тул-лупа (артефакт ДЗ-02/03, feat-020).

### Правила для исполнителя

1. Ветка `feat-004-fsm-skeleton` от `milestone/v4` (D-031); merge — в `milestone/v4`.
2. Маршрут — только код (hw-01 §2.1, ADR-002): условные рёбра в `routing.py` читают
   `route_flags`/счётчики, которые пишет только код; LLM не возвращает имя узла —
   это инвариант, проверяемый тестом (как test_validator_cuts_invented_join в S09).
3. Узлы не знают о LangGraph (ADR-002 §5): orchestration — только рёбра и обёртки,
   вызывающие application-сервисы; сборка — `composition/container.py`.
4. Два уровня предохранителей — оба кодом: per-node `retry_count` ≤ 3 (M1, D-016)
   и `recursion_limit` на весь граф (ADR-002 v1.2); плюс глобальные капы прогона
   ≤ 8 LLM-вызовов / ≤ 90 s / ≤ 2 ₽ (пул S03: оркестратор делает reset бюджетного
   предохранителя на старте прогона — обязательство S03; жёсткий BudgetGuard — S15).
5. CLI: FSM-прогон — за отдельным флагом/подкомандой (решение зафиксировать в
   ARCH-GATE); дефолтное поведение `apps/cli.py` (тул-луп + --plain) не меняется.

## Предусловия

- [x] Сессия СТАРТУЕТ: статус в шапке переведён в «◐ в работе» и ARCH-GATE резолвлен ДО кода (canon-check architect ✅ либо обоснованный N/A) — старт-гейт (`guard-subagents.sh --phase start`, D-027) проверяет только стартованные (◐) сессии; «[ ] не начата» — план, гейт её скипает
- [x] S01, S03 закрыты: `uv run pytest src/tests/unit/test_smoke.py src/tests/unit/test_llm_client.py -q` → passed
- [x] `make check` зелёный (baseline репозитория)
- [x] feat-001, feat-003 в state=passing; feat-004 активирована в state=active до старта работ

## Задачи

### Task 1: AgentState — контракт стыка 1 (S00)

`src/text_to_sql_agent/domain/entities.py`: typed dict `AgentState` — поля контракта 1
S00 дословно: `question`, `rewritten_question`, `assumptions[]`, `permissions{}`,
`graph_version`, `join_plan`, `skeleton` + `skeleton_hash`, `sql`, `validation`,
`rows`/`compact`, `error`, `retry_count`, `budget_used`, `route_flags`,
флаги `truncated`/`approximate`/`incomplete`. Инвариант «узел читает только свои поля
и пишет только свои» — комментарием-картой в docstring (какое поле чей).

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_domain_entities.py -q   # AgentState: состав полей, дефолты флагов
```

---

### Task 2: graph.py — 15 узлов-заглушек + routing.py — условные рёбра

`src/text_to_sql_agent/orchestration/graph.py`: StateGraph с 15 узлами hw-01 §2.1
(`resolve_access, clarify, plan_joins, build_skeleton, plan_query, generate_sql,
generate_step_sql, validate_sql, check_cost, execute_sql, merge_results,
compact_result, diagnose_error, interpret_result, degrade`) — заглушки-транзиты.
`routing.py`: условные рёбра по state-agent v1.5 — clarify-ветка (флаг из заглушки),
validate-ветка (ok/галлюцинация/чужие данные), probe после execute при 0 строк,
терминальные исходы `done/degrade/short_circuit/overflow/fail/rejected/degraded_out/
budget_stop/clarification` (END с различимым payload-типом исхода, не traceback).
`check_cost` — транзит; `plan_query` — заглушка «не декомпозировать» (feat-017).

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_graph.py -q   # состав 15 узлов; каждый терминал достижим;happy-path порядок узлов
```

---

### Task 3: Предохранители — per-node ≤ 3 + recursion_limit + глобальные капы

Per-node счётчик `retry_count` в state (инкремент — кодом ребра, сброс при смене узла);
исчерпание ≤ 3 → `fail`-терминал с уточняющим вопросом, не исключение. `recursion_limit`
на весь граф (константа Settings с комментарием «почему»); GraphRecursionError
перехватывается на границе оркестратора → валидный отказ. Глобальные капы: reset
бюджетного аккумулятора S03 на старте прогона, счётчик LLM-вызовов ≤ 8 и дедлайн 90 s
проверяются кодом до вызова узла с LLM (заглушки имитируют) → `budget_stop`/`fail`.

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_graph.py -q   # счётчик → fail; recursion_limit → валидный отказ; капы → budget_stop
```

---

### Task 4: CLI-точка входа FSM-прогона (composition wiring)

`composition/container.py`: сборка графа (узлы-заглушки + routing) и оркестратор
`answer_question` (application/use_cases) — запуск с reset бюджета, перехват
recursion_limit, терминальный payload наружу. `apps/cli.py`: флаг/подкоманда FSM-прогона
(дефолт тул-лупа не трогать) — печатает порядок узлов и итоговый исход.

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_container_wiring.py -q   # граф собирается из Container; дефолт CLI не изменился
uv run python -m apps.cli "Сколько заказов у клиентов из РФ?" --<fsm-флаг>   # порядок узлов-заглушек + терминальный исход (layer-3 фичи)
```

## Критерии приёмки

- [x] `uv run python -m apps.cli "<вопрос>"` (FSM-режим) прогоняет вопрос через все узлы-заглушки в правильном порядке и печатает результат (behavior feat-004) — layer-3: happy-path порядок + исход done; узлы веток (plan_query/probe/шаговый цикл/терминалы) — тесты достижимости (интерпретация «все узлы» = все узлы маршрута вопроса; состав 15 узлов и рёбра веток — тестами состава/графа)
- [x] LLM не может изменить маршрут: route_flags/счётчики пишет только код, тест-инвариант (behavior feat-004)
- [x] Попытки ограничены двумя уровнями кода (per-node ≤ 3 + recursion_limit) и глобальными капами ≤ 8 вызовов / ≤ 90 s / ≤ 2 ₽ (behavior feat-004) — гвард end-to-end (ревью I1: имитация оплаты + рёбра гейтов под гвардом)
- [x] Все терминальные исходы FSM (state-agent v1.5) достижимы и различимы в payload
- [x] Дефолтное поведение CLI (тул-луп feat-020, --plain) не изменилось — артефакты ДЗ-02/03 живы
- [x] Фича закрыта: `make verify-feature F=feat-004` → PASS, state=passing с evidence

## Ловушки

1. **Заглушка ≠ пропущенное ребро:** validate-ветка должна существовать уже в каркасе
   (рёбра галлюцинация/deny/ok) — S11 лишь наполнит проверку; если ребро не заложить
   сейчас, S11 полезет править граф — это архитектурный сдвиг посреди вехи.
2. LangGraph `interrupt()` в S04 не нужен — clarify-заглушка возвращает флаг «уточнение»
   и routing ведёт в терминал `clarification`; настоящий interrupt + checkpointer — S08.
3. `recursion_limit` подбирается с запасом над суммой худших ретраев (обосновать числом
   в комментарии; cap 8 LLM-вызовов — отдельный, более тесный предел).
4. Reset бюджетного аккумулятора — ровно тот шов, который S03 оставил оркестратору
   (`reset_request_budget()` в Protocol); не заводите второй аккумулятор.
5. Windows: `python` в PATH, `python3` нет (S00) — команды самопроверок через `uv run`.
6. verify-feature strict-режим: строки гейтов заполняются по факту, «N/A — сессия
   не начата» не проходит (Ловушка-6 S09).

## Гейты субагентов (заполняется по факту, до закрытия сессии)

- ARCH-GATE: ✅ canon-check архитектора 2026-11-21 — verdict needs-decision → rulings R1–R6 ДО кода: (R1, W1) логика узлов-заглушек — функции/классы в application/services (прецедент execute_sql_node.py), graph.py — только биндеры и рёбра (AGENTS.md Boundaries, S00:69 «по одному классу на узел»); (R2, W2) GraphRecursionError перехватывает orchestration (engine-адаптер runner → fail-терминал в state), answer_question (application/use_cases) получает runner через Protocol GraphRunner (application/ports) с DI из composition — application не импортирует langgraph (ADR-002 §5, направление зависимостей); (R3, W3) минимальный трейс-спан узла в обвязке orchestration (graph.node через порт Tracer, ADR-002 §2), порядок узлов для CLI — новое обвязочное поле state.visited_nodes (пишет обвязка, не содержательные узлы; синк S00 контракт 1 тем же коммитом), печать — apps/cli.py, не сервисы (S00-6); (R4, W4) источники overflow/short_circuit по hw-01 §3.5.4 — оба из шагового цикла execute_sql (пустой key-set → short_circuit; key-set > капа → overflow): рёбра закладываются в каркас (Ловушка-1), в S04 из заглушек динамически недостижимы; тест достижимости — инъекция route_flags в стартовом state (симуляция кодового решения, инвариант «маршрут пишет только код» не ослабляется); разрыв диаграммы state-agent v1.4 (входящие рёбра не нарисованы) — запись в solution/FIXES.md, синх диаграммы при закрытии сессии; (R5, N1) глобальные капы — именованные Settings (fsm_max_llm_calls=8, fsm_deadline_s=90, fsm_recursion_limit — запас над суммой худших ретраев, комментарий «почему»; AGENTS.md Принципы кода); (R6, N5) CLI-флаг FSM-прогона — `--fsm`, дефолтный тул-луп/--plain не трогаются; behavior/verification/layer-3 feat-004 синхронизируются тем же коммитом (S00-3). Принято по note: graph_version=0-заглушка в resolve_access (подменяют S05/S07), словарь route_flags — в docstring-карте Task 1, рёбра шагового цикла (execute_sql→merge_results→generate_step_sql, diagnose_error→generate_step_sql D-019) и T2-ветки check_cost (degraded_out/budget_stop) — в каркасе сразу, счётчик ребра diagnose_error→generate_sql инкрементирует generate_sql (ретраится генератор, не диагност). Compliant: TypedDict в domain без импорта langgraph (S00 контракт 1), reset_request_budget — шов S03 (порт llm_provider.py:156, второй аккумулятор не заводится), терминалы — END с payload-типом (ADR-002:87), инкремент retry_count кодом ребра (state-agent:100).
- REVIEW-GATE: ✅ Ready to merge: Yes — ревью-1 «With fixes» (0C/3I/9M, диапазон d25849f..b14d49f) → fix-волна c43591c: **I1** гвард глобальных капов end-to-end — (a) заглушки LLM-узлов «платят» вызовом (_bump_llm_calls: карта владения budget_used «заглушки имитируют», кап живой без инъекции), (b) рёбра гейтов ретрая через make_guarded_route (было безусловными — цикл ретрая входил в генератор мимо исчерпанных капов), LLM_NODES += plan_query (reasoning hw-01 §3.5.4) + degrade (отказ — S15); **I2** ветка probe «потерянный джойн» заложена ребром (FLAG_PROBE_LOST_JOIN → diagnose_error, Ловушка-1); **I3** гашение одноразовых флагов доходит до state при частичных апдейтах (updates.setdefault — инвариант анти-livelock не на побочном свойстве заглушек); minors M1–M5/M8/M9 → регрессии со stash-прогоном на b14d49f (5 failed — кусают) → scoped re-review **«Ready to merge: Yes»** (0C/0I, b14d49f..c43591c: I1/I2/I3 closed, регрессии верифицированы) → доводка миноров fa59d3d (минор-1 остатки v1.5-синка в settings/execute_sql_node/планах S08/S14/S15/S27; минор-2 FLAG_PROBE_LOST_JOIN в TRANSITION_FLAGS — стейл-True после ретрая не уводит S14 в diagnose мимо пробы; минор-3 параметризованный инвариант «узел ∈ LLM_NODES ⟺ заглушка платит»; set-aside аннотация make_guarded_route frozenset); make check EXIT=0 (**417 passed** = 38 graph + 379 прочих); layer-3: CLI --fsm happy-path done (4 вызова < капа 8), дефолтный --plain не изменился (живой прогон LM Studio «400 заказов»), запреты S00 grep чистые

## Отчёт по сессии (заполняется по факту)

- Сделано:
  - [Task 1: AgentState — контракт стыка 1] domain/entities.py: `AgentState` (typing.TypedDict, без импорта langgraph — домен чист) — состав полей S00 дословно + карта владения «какое поле чей» в docstring (error — кодовое поле routing с обязательством очистки — M2 ревью) + обвязочные поля orchestration (visited_nodes/outcome/deadline_at — ruling R3/R4, синк S00 стык 1 тем же коммитом) + `TerminalOutcome`/`TERMINAL_OUTCOMES` (9 исходов state-agent v1.5) + `initial_state()` с нейтральными дефолтами. Тесты: состав полей, stdlib-TypedDict, терминалы, дефолты.
  - [Task 2: graph.py 15 узлов + routing.py рёбра] application/services/stub_nodes.py — 15 заглушек-транзитов (ruling R1: логика узлов в services, graph.py — биндеры/гейты/терминалы; setdefault-семантика флагов: своё решение не затирает выставленное — инъекция теста симулирует код будущих сессий); orchestration/routing.py — 15 условных рёбер по state-agent v1.5 + константы FLAG_* (единственный источник) + FSMGuardCaps + attempts_used; orchestration/graph.py — build_fsm_app (биндеры с трейс-спаном graph.node через порт Tracer + visited_nodes, 3 гейта ретраев, 9 терминальных маркеров → END); orchestration/runner.py — LangGraphRunner (GraphRecursionError → fail-исход, дедлайн monotonic в state; ruling R2 — application не знает langgraph: порт GraphRunner в application/ports). Коммит b14d49f.
  - [Task 3: предохранители] per-node retry_count ≤ 3 — гейт на ребре ретрая (инкремент кодом ребра, сброс при смене ретрауемого узла, изоляция по FLAG_RETRY_TARGET — чужой счётчик не кусает; fuse в route_plan_joins/route_diagnose_error; на diagnose→generate_sql ретраится генератор); recursion_limit 50 (Settings.fsm_recursion_limit, комментарий «почему»: happy ≈12 супер-шагов, худший ≈44); глобальные капы ≤ 8 вызовов / ≤ 90 s / ≤ 2 ₽ — llm_guard на рёбрах перед LLM-узлами И на рёбрах гейтов ретрая (fix I1 ревью: без этого цикл ретрая входил в генератор мимо капов), end-to-end живой — заглушки LLM-узлов «платят» вызовом (_bump_llm_calls — имитация из карты владения; инвариант оплаты — параметризованный тест по всем 8 LLM_NODES); GraphRecursionError → валидный fail; Settings fsm_max_llm_calls/fsm_deadline_s/fsm_recursion_limit (ruling R5). Коммиты c43591c/fa59d3d.
  - [Task 4: CLI + wiring] application/use_cases/answer_question.py — AnswerQuestion (reset_request_budget — шов S03/ruling Q7, BudgetExceededError → budget_stop, LLMProviderError → degrade, отсутствие outcome → честный fail) + FSMResult (outcome/visited_nodes/answer); composition/container.py — fsm_runner (build_fsm_runner с caps из Settings) + answer_question; apps/cli.py — флаг `--fsm` (ruling R6): печатает порядок обхода (ASCII-стрелка — cp1251-redirect, M3) и терминальный исход, дефолтный тул-луп/--plain не тронуты. Коммит b14d49f.
  - Тесты: 417 passed (+46 к baseline 371: test_graph.py 38 — состав/счастливый путь/достижимость 9 терминалов/T1-ветка/шаговый цикл/инвариант маршрута/fuse/гвард/раннер/use case + 8 параметризованных оплаты; entities +4; settings +2; wiring +2). Регрессии ревью со stash-прогоном на до-фиксовом b14d49f (worktree): 5 failed — кусают.
  - live-прогон (layer-3): `uv run python -m apps.cli "Сколько заказов у клиентов из РФ?" --fsm` → маршрут 10 узлов happy-path, исход done, 4 LLM-вызова < капа 8; `--plain` — живой ответ LM Studio «400 заказов» (артефакты ДЗ-02/03 живы); make check EXIT=0.
- Отклонения от плана:
  - Интерпретация критерия «прогоняет вопрос через все узлы-заглушки»: FSM-прогон проходит узлы happy-path (10), веточные узлы (plan_query, generate_step_sql, diagnose_error, merge_results, degrade) канонно на happy-path не попадают (T1 выключен до feat-017) — их достижимость и порядок доказаны тестами (инъекция кодовых флагов); отмечено в критерии приёмки.
  - plan_query не вошёл в LLM_NODES на первом коммите (канон hw-01 §3.5.4: «средняя reasoning») и degrade не платил вызовом — закрыто fix-волной I1/M6 (LLM_NODES = 8 узлов, оплата — инвариант-тестом).
  - Флаги заглушек — setdefault-семантика (не безусловная запись): маршрут пишет только код, выставленный флаг = кодовое решение предыдущего звена/инъекции теста; иначе тесты веток требовали бы второй реализации узлов. Решенческие флаги не гасятся на ре-входе (узел перерешает), одноразовые (TRANSITION_FLAGS) гасятся биндером — анти-livelock каркаса (ревью I3).
  - Решение ребра execute_sql читает error (не только route_flags): error — кодовое поле (пишут узлы кодом), инвариант «модель не меняет маршрут» не ослаблен — зафиксировано в карте владения и docstring routing (M2).
- Перенесено в другие сессии:
  - S05/S08/S10/S11/S14/S15 — содержательное наполнение узлов (заглушки подменяются сервисами, граф не правится: биндер принимает любую функцию); S08 — interrupt()+checkpointer (в каркасе терминал clarification по флагу); S12 — порог T2 (check_cost — транзит); S14 — COUNT-проба probe_lost_join + эскалация M1 с бюджетной проверкой; S15 — жёсткий BudgetGuard + incomplete-контракт budget_stop + реальные ₽ в budget_used; S17/feat-017 — T1-декомпозиция (plan_query — заглушка «не декомпозировать»).
  - FIX-008 — в журнале противоречий (входящие рёбра overflow/short_circuit + ребро шагового цикла в state-agent v1.4; синх диаграммы v1.5 тем же коммитом; финальное подтверждение владельца — по clock-out).
  - Set aside re-review (не блокеры): реальные ₽ в имитации оплаты (заглушки платят только llm_calls) — S15/BudgetGuard; budget_stop вместо degrade при исчерпании попыток на грани капа (8-й вызов съеден diagnose) — следствие канонного капа ≤ 8, route-тесты degrade покрывают.


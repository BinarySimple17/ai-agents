# S15 — compact_result + interpret_result + degrade + BudgetGuard (веха V4, ДЗ-05)

Этап: Веха V4 (ДЗ-05: финальные узлы FSM — объяснение и честный отказ) | Владелец: агент | Зависимости: S05, S14 | Оценка: 1 | Статус: ✅ 2026-11-22
Фичи: feat-015 — interpret_result + degrade; зависимости feat-005 (закрывается S05), feat-014 (закрывается S14)
Роль по фиче: закрывает feat-015 — финальная сессия, через make verify-feature

## ⚠️ Прочитай перед началом

Исполнитель: ИИ-агент. К моменту старта: S05 закрыта (permissions — для degrade-словаря
«что доступно взамен»), S14 закрыта (execute_sql пишет rows в state; retry-цикл и probe
живы), S03 закрыта (бюджетный аккумулятор best-effort + шов budget_remaining_roubles —
S09/S10 использовали). Эта сессия закрывает прогон: `compact_result` (код) сворачивает
строки в компактный вид, `interpret_result` (glm-5.3-prime, strong) пишет финальный
текст: SQL + допущения (usage_notes рёбер из JoinPlan) + флаги усечения/approximate +
стоимость. Degrade — отказ по правам бизнес-понятиями. Здесь же BudgetGuard становится
полноценным портом (S00: порт вводит S15) — оркестраторный учёт ₽ по прогону,
заменяющий шов S09/S10.

Границы скоупа: БЕЗ approximate/matview-механики (T2 — feat-012/017, выпускная;
флаг approximate в payload готов, но появляется только у выпускной), БЕЗ matview/async
исходов, БЕЗ Streamlit-формата (двухслойные форматы ADR-008 — выпускная), БЕЗ
eval-метрик (ДЗ-06).

### Правила для исполнителя

1. Ветка `feat-015-interpret-result` от `milestone/v4` (D-031); merge — в `milestone/v4`.
2. Вход interpret_result — ТОЛЬКО компактный вид от compact_result (row_count +
   ≤ 20 строк + агрегаты + флаги, ~1150 токенов): сырые строки в промпт не попадают
   (behavior фичи); лимиты — именованные константы с комментарием.
3. «Неполное не выдаётся за полное» (state-agent v1.5, контракт budget_stop): частичный
   результат — только с явным incomplete-флагом в payload и тексте; бюджет исчерпан —
   честный отказ или incomplete, не «уверенное объяснение неполных цифр» (R-Q2-класс).
4. Degrade (S9 hw-01): код фиксирует, чего не хватает (по permissions), LLM формулирует
   отказ бизнес-понятиями + доступный вариант; БЕЗ имён недоступных таблиц, БЕЗ
   подтверждения существования данных («данные премии недоступны», не «таблица
   bonuses скрыта» и не «в bonuses есть строки, но нельзя»).
5. BudgetGuard — порт (Protocol) + реализация на аккумуляторе S03: учёт ₽ по факту
   usage всех вызовов прогона, проверка до каждого LLM-вызова узла, incomplete при
   исчерпании; шов budget_remaining_roubles в S09/S10 переводится на порт (механика
   не меняется — меняется точка вызова).

## Предусловия

- [x] Сессия СТАРТУЕТ: статус в шапке переведён в «◐ в работе» и ARCH-GATE резолвлен ДО кода (canon-check architect ✅ либо обоснованный N/A) — старт-гейт (`guard-subagents.sh --phase start`, D-027) проверяет только стартованные (◐) сессии; «[ ] не начата» — план, гейт её скипает
- [x] S05, S14 закрыты: `uv run pytest src/tests/integration/test_resolve_access.py src/tests/unit/test_diagnose.py -q` → passed (feat-005, feat-014 passing)
- [x] `make check` зелёный (baseline репозитория) — ./init.sh exit 0 на 072fa53 (818 passed, 2026-11-22)
- [x] feat-005, feat-014 в state=passing; feat-015 активирована в state=active до старта работ

## Задачи

### Task 1: compact_result — код

`application/services/compact_result_service.py` + VO ResultCompact (domain, поля по
ER — ruling R2: `row_count`, `sample` ≤ 20 строк, `aggregates`, `truncated`,
`approximate`): из rows → row_count, первые ≤ 20 строк (усечение с флагом truncated),
агрегаты по числовым колонкам (sum/avg/min/max — код), флаги state. Без LLM. Бюджет
промпта — константа; превышение — усечение с флагом, не «молча больше».

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_interpret.py -q   # компакция: 100 строк → row_count=100 + 20 строк + truncated; агрегаты верны
```

---

### Task 2: interpret_result — финальный текст (strong)

`application/services/interpret_result_service.py`: вход ResultCompact + sql +
assumptions (clarify) + usage_notes рёбер (JoinPlan) + стоимость (BudgetGuard);
strong-роль; в ответе обязательны: текст, SQL, список допущений (usage_notes
раскрываются человеческим языком — флагман артефакта ДЗ-04 продолжает жить),
флаги усечения/approximate/incomplete, стоимость. pydantic-выход (ADR-008),
температура 0.

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_interpret.py -q   # состав ответа на моках; usage_notes в допущениях; флаги отражены
```

---

### Task 3: degrade + incomplete

Degrade-узел: вход — типизированный вердикт-причина (domain VO — ruling R8: cause +
бизнес-понятия + доступный вариант из permissions; сырые route_flags/validation в
промпт не идут); выход — отказ бизнес-понятиями + доступный вариант (LLM формулирует,
роль mid, одна попытка без эскалации — ruling R1). Инварианты текстов: без имён
недоступных таблиц, без подтверждения существования — adversarial-тесты на формулировки
(Ловушка-2) + **детерминированный санитайзер** поверх выхода (runtime-гарантия: нарушение
⇒ шаблон-fallback 0 вызовов) + шаблон-fallback при недоступности LLM (NFR-8) — R1.
**Amend ADR-007 v1.3** (degrade → LLM-формулировка mid, строка экономики) — тем же
коммитом. Incomplete: budget_stop ставит incomplete в обоих путях (терминал-карта +
exception-fallback answer_question — ruling R3); finish_reason=length с текстом →
incomplete (хвост S19), не «как есть».

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_interpret.py -q   # deny-сцена «премии»: нет имён таблиц/подтверждений; budget_stop → incomplete-флаг
```

---

### Task 4: BudgetGuard — порт + перевод швов

`application/ports/budget_guard.py` (Protocol, read-side — ruling R4) + реализация
`infrastructure/token_budget.py` поверх аккумулятора S03 (reset на старте прогона —
уже делает S04): check-before-call для всех узлов с LLM (адаптер + llm_guard —
порт лишь read-side учёт), расход по usage (usage_missing → консервативная оценка,
прецедент S03), остаток с резервом 0,6 ₽ для эскалаций. Узлы пишут budget_used.rub
снапшотом guard.spent_rub() (ruling R6 — ₽-половина llm_guard живёт). Перевод
вызовов budget_remaining_roubles (S09/S10) на порт; поведение тестов не меняется
(рефакторинг точки вызова, не механики). BL-005: константы EVENT_LLM_* — в ports/
tracer.py без новой семьи (ruling R5).

Триаж беклога 2026-11-22 — закрыть здесь хвосты: BL-015 (остаток: три одинаковые
лямбды шва budget_remaining_roubles в composition/container.py уходят на порт —
запись закрыть), BL-005 (имена llm.*-событий — константы в порте tracer, единый
источник эмиттера и рендерера: budget-события подключаются в этой же задаче),
BL-016-смежное (определение «попытки узла»: FSM-визиты vs внутренние вызовы M1 —
зафиксировать в учёте BudgetGuard; определение приходит из S14).

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_interpret.py src/tests/unit/test_plan_joins.py src/tests/unit/test_generate_sql.py -q   # шов заменён, поведение зелёное
```

---

### Task 5: Подключение в FSM — терминалы done/degrade

Наполнение заглушек: compact_result → interpret_result → done-терминал; degrade-узел →
done-терминал (деградация — валидный исход с payload); budget_stop-терминал с
incomplete. Полный happy-path через Container на моках: вопрос → … → ответ со
стоимостью (уже близко к сборке S27 — здесь на фейках).

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_graph.py src/tests/unit/test_interpret.py -q   # done/degrade/budget_stop терминалы с корректным payload
```

## Критерии приёмки

- [x] Успех: текст + SQL + допущения + флаги усечения/approximate + стоимость; вход interpret — компактный вид ~1150 токенов, не сырые строки (behavior feat-015) — тест prompt_not_raw_rows (100 строк) + live happy-path
- [x] Отказ по правам: «данные премии недоступны» + доступный вариант, без имён недоступных таблиц и без подтверждения наличия данных (behavior feat-015) — санитайзер-тесты (регистр/морфология/свои темы) + live degrade (только свои темы)
- [x] Бюджет исчерпан: честный отказ или incomplete-флаг; частичный результат — только с incomplete (behavior feat-015, контракт budget_stop) — R3: терминал-карта + exception-fallback, тесты обоих путей
- [x] BudgetGuard — порт из S00; шов budget_remaining_roubles переведён; резерв 0,6 ₽ сохранён (D-016) — ruling R4, wiring-тест 4 сервисов на одном источнике
- [x] usage_notes рёбер раскрываются в допущениях ответа (преемственность флагмана ДЗ-04) — JoinPlan.assumptions → final_answer.usage_notes (тест + live «агрегация поверх ребра…»)
- [x] Фича закрыта: `make verify-feature F=feat-015` → PASS, state=passing с evidence — «uv run pytest src/tests/unit/test_interpret.py -q» exited 0, 66 passed (2026-11-22)

## Ловушки

1. **Adversarial-тесты формулировок — не формальность:** модель любит «таблица
   bonuses защищена» — прогоняйте тексты deny-ответов через запрещённые маркеры
   (имена таблиц из недоступного множества, слова «существует/есть строки») —
   тест-словарь, а не глазами.
2. Стоимость — из usage фактических вызовов прогона (BudgetGuard), не пересчёт по
   матрице; usage_missing (локальный шлюз) → консервативная оценка с пометкой, не 0 ₽.
3. Компактный вид: усечение строк молча = ложный ответ; флаг truncated обязателен
   и в payload, и в тексте ответа (R-Q2-класс: неполное не выдаётся за полное).
4. interpret_result не видит сырой SQL-ошибки и стектрейсов — только свои входы;
   любое «объяснение» причин сбоя — из state-вердиктов, не из исключений.
5. strong-роль — самая дорогая: BudgetGuard-проверка ДО вызова interpret обязательна
   (иначе последняя миля пробивает кап); тест на «остатка нет → отказ без вызова».
6. Гейт strict: строки ARCH/REVIEW — по факту; «N/A — сессия не начата» = FAIL.

## Гейты субагентов (заполняется по факту, до закрытия сессии)

- ARCH-GATE: ✅ needs-decision → rulings R1–R8 закрыты ДО кода (canon-check architect 2026-11-22: план по канону; blocker — живой ADR-007:74 «degrade — код, без LLM»/«0 ₽» противоречит ADR-005:171 + state-agent v1.8 + routing.LLM_NODES → обязателен amend ADR-007 v1.3 тем же коммитом с degrade)
- REVIEW-GATE: ✅ Ready to merge: Yes (волна 1 «With fixes» 0C/2I/3M: I1 cost_rub — снапшот после вызова + регрессия с растущим guard'ом; I2 parity-гвард флагов degrade; M1 стемы-маркеры; M2 entities; M3 тул report_refusal → fix-волна 4655c66; scoped re-review 5/5 закрыто, регрессий нет — доводка гварда шаблонов + BL-020 [eb29e60]; 072fa53..eb29e60)

### Rulings ARCH-GATE S15 (canon-check 2026-11-22, до первого код-коммита)

- **R1 — degrade: LLM формулирует, код фиксирует и страхует.** Живой канон — ADR-005:171 («код фиксирует, чего не хватает; LLM формулирует») + prd FR-6/сцена RBAC №2 + state-agent v1.8 + routing.LLM_NODES; hw-01-таблица «код, без LLM» — исторический артефакт сдачи. Роль формулировки — **mid, одна попытка, без эскалации** (opinion архитектора, фиксируется amend'ом ADR-007). Гарантии S9 — двухслойные: adversarial-тесты валидируют промпт; **детерминированный санитайзер** поверх выхода — runtime-гарантия («текст LLM не доверенный», AGENTS Constraints): запрещённые маркеры (имена недоступных таблиц, слова существования) → нарушение ⇒ **шаблон-fallback** (0 вызовов). Шаблон-fallback также при недоступности LLM/бюджета (NFR-8) — живой остаток «degrade-кода» из ADR-007/hw-01. **Amend ADR-007 v1.3** (таблица узлов: degrade → LLM-формулировка mid; строка экономики «degrade — 0 ₽») — тем же коммитом с кодом degrade.
- **R2 — ResultCompact VO по ER:** поля по er-data-model.md:177 — `row_count`, `sample` (≤ 20 строк, не «rows»), `aggregates`, `truncated`, `approximate` (в VO с S15, False до выпускной feat-012/017); поле `columns` — НЕ вводить. MAX_COMPACT_ROWS=20 — именованная константа с комментарием (~1150 токенов входа interpret, ADR-007:131).
- **R3 — incomplete при budget_stop — оба пути:** make_terminal — декларативная карта терминальных payload `{budget_stop: {"incomplete": True}}` (протокольный клей orchestration, не бизнес-узел); exception-fallback answer_question (BudgetExceededError → budget_stop) ставит incomplete там же — иначе второй путь budget_stop обходил бы флаг.
- **R4 — BudgetGuard: порт read-side, реализация — infrastructure/token_budget.py** (S00:91 «token_budget.py», не инлайн в composition). Порт `{spent_rub() -> float; remaining_rub() -> float; reset() -> None}`; composition инжектирует callables над аккумулятором S03 (llm.budget_used_rub / llm.reset_request_budget); шов ruling Q7 S09 — 4 лямбды → guard.remaining_rub (сигнатура сервисов Callable[[], float] не меняется, BL-015 закрывается). Исключение ДО вызова поднимает адаптер (S00:116) и ребро llm_guard — порт лишь read-side учёт, сервисные проверки резерва 0,6 ₽ не меняются. Docstring порта ссылается на определение «попытки» R6 S14 (m1_policy) — BL-016-смежное зафиксировано.
- **R5 — BL-005 без новой семьи:** константы EVENT_LLM_* (llm.complete/retry/timeout/reject/breaker_open/budget_stop) — в ports/tracer.py, provider и console_tracer импортируют (infrastructure → ports разрешено); семью «budget.stop» НЕ вводить (llm.budget_stop уже в семье BL-005); отказ guard-проверки в сервисе трейсится node-событием `*.unavailable` reason=budget (прецедент R5 S14).
- **R6 — ₽-учёт в state: пишут узлы** (прецедент владения budget_used: plan_joins/generate_sql/diagnose nodes) — снапшот `guard.spent_rub()` тем же единым обновлением budget-dict, что llm_calls; фабрики узлов принимают optional budget_guard. llm_guard-проверка ₽ становится живой; стоимость для interpret — тот же снапшот (usage фактических вызовов, не пересчёт по матрице — Ловушка 2).
- **R7 — трейс-события новых узлов (семья R5 S14):** interpret.verdict — outcome/флаги/cost_rub/row_count, БЕЗ текста ответа и БЕЗ строк данных (JSONL-приватность); interpret.unavailable — reason (llm/budget); degrade.verdict — cause/has_variant (внутренние детали — JSONL, не пользователю), текст отказа не дублируется; degrade.unavailable — reason.
- **R8 — вход degrade: типизированный вердикт-причина (domain VO)** — сервис degrade собирает cause-enum + бизнес-понятия + доступный вариант из state.permissions ДО промпта; сырые route_flags/validation-словари в промпт не попадают. «Своё упоминать можно» — ADR-005:78 (граница знаний = граница прав).

## Отчёт по сессии (заполняется по факту)

- Сделано:
  - **ARCH-GATE** (до кода): canon-check architect — needs-decision, rulings R1–R8 записаны до первого код-коммита; blocker (ADR-007:74 «degrade — код, без LLM» против живого ADR-005:171/prd FR-6) закрыт amend'ом ADR-007 v1.3 тем же коммитом с degrade (Task 3). Findings: имена VO по ER (sample/approximate — R2), incomplete в обоих путях budget_stop (R3), реализация порта в infrastructure/token_budget.py (R4), без семьи budget.stop (R5), ₽-снапшот пишут узлы (R6), события R7, вердикт-причина R8.
  - **Task 1 (коммит 14323af — старт+Task 1)**: domain/result_compact.py — VO ResultCompact (row_count, sample ≤ MAX_COMPACT_ROWS=20 с fail-closed-валидатором, агрегаты sum/avg/min/max по числовым колонкам: bool не число, смешанные пропускаются, None — пропуск; truncated/approximate) + compact_result_node (код-узел, LLM-бюджет не платит; rows None — валидный T2-путь). 16 тестов.
  - **Task 2**: InterpretService (strong, тул report_answer, M1 ≤ 3 без эскалации; вход — компакт; INCOMPLETE при M1/шлюзе/BudgetExceededError; finish_reason=length → ANSWER+incomplete) + interpret_result_node (final_answer payload, ₽-снапшот R6); порт LLMResponse + finish_reason (провайдер передаёт, тест-прогонка); порт BudgetGuard (read-side Protocol); AgentState +final_answer (карта владения + CONTRACT_1_FIELDS). 31+1 тестов.
  - **Task 3**: domain/degradation.py (DegradationCase cause/situation/available/forbidden + fallback_text) + DegradeService (mid, одна попытка, тул report_refusal, санитайзер + шаблон-fallback) + degrade_node (cause-priority, final_answer с sql=None, события) + R3 (TERMINAL_PAYLOADS в make_terminal + exception-fallback answer_question) + amend ADR-007 v1.3. 26 тестов.
  - **Task 4**: infrastructure/token_budget.py (TokenBudgetGuard: spent-зеркало/remaining floor 0/reset-делегат) + container (4 лямбды → guard.remaining_rub, BL-015 закрыт; Container.budget_guard) + BL-005 закрыт (EVENT_LLM_* в порту tracer; provider/console_tracer импортируют). 6+1 тестов.
  - **Task 5**: container node_overrides (compact/interpret/degrade + инъекции interpret_service/degrade_service) + FSMResult.answer → final_answer + CLI _print_final_answer. 3 FSM-сценария на реальном графе.
  - **Тесты**: unit 807 + integration 82 (ruff чист); самопроверки всех задач зелёные; verify-feature feat-015 PASS (66 test_interpret).
  - **REVIEW-GATE**: волна 1 «With fixes» 0C/2I/3M → fix-волна 4655c66 (I1 — cost_rub payload/трейса ПОСЛЕ вызова strong [пользовательский контракт, ~0,58 ₽ из капа] + регрессия с растущим guard-фейком, кусающая до-фикс; I2 — parity-гвард литералов флагов degrade vs routing; M1 — стемы «содерж/имеютс/наличи» + 2 adversarial-кейса; M2 — entities карта владения; M3 — родной тул в промпт-тесте) → scoped re-review «Ready to merge: Yes» (5/5 закрыто, регрессий нет) → доводка eb29e60 (гвард шаблонов теми же стемами + BL-020 [эвристика стемов не исчерпывающая — P3]).
  - **Layer-3 live** (локальный шлюз qwen3.8-9b + БД): ① happy-path «Сколько всего заказов в базе?» — 10 узлов → done, ответ «В базе всего 1000 заказов» + исполненный SQL + допущение из usage_notes ребра (fanout_guard ADR-004) + стоимость; ② clarify-цикл u_logist «премии» — права-фильтрованный мир честно не содержит чужих таблиц (уточнение ×3 → clarification по TTL); ③ полный цикл «Сколько клиентов имеет более одной рекламной кампании?» — генерация → SQL-ошибка → diagnose → ретраи ×2 → degrade с честным отказом (только свои темы, без чужих имён и подтверждений существования — санитайзер/S9 в бою).
- Отклонения от плана:
  - Port — Task 4 планировался после Task 2/3, но порт BudgetGuard понадобился узлам уже в Task 2 (снапшот ₽/cost_rub) — Protocol создан в Task 2, реализация и перевод шва — в Task 4 по плану; поведение не менялось.
  - EVENT_COMPACT_VERDICT — объявлена и удалена в той же сессии (никем не эмитится; R7 не планировал компакт-событие — честный порт без мёртвых констант).
  - Живой degrade-сцены «премии» (из prd RBAC №2) в лоб не получилось: права-фильтрованный retrieval выводит таблицу из мира → путь через clarify, не degrade; degrade показан живьём на сцене «нет активного пути customers–campaigns» (перебор → генерация → самокоррекция → degrade) — инварианты текста те же.
- Перенесено в другие сессии:
  - [новых переносов нет; BL-015/BL-005 закрыты в Task 4; BL-019 (шаговые флаги) остаётся на S17; approximate-механика — выпускная feat-012/017]

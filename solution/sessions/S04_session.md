# S04 — Каркас FSM на LangGraph: 15 узлов-заглушек, рёбра, предохранители (веха V4, ДЗ-05)

Этап: Веха V4 (ДЗ-05: state machine —hw-plan §5) | Владелец: агент | Зависимости: S01, S03 | Оценка: 1.5 | Статус: [ ] не начата
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

- [ ] Сессия СТАРТУЕТ: статус в шапке переведён в «◐ в работе» и ARCH-GATE резолвлен ДО кода (canon-check architect ✅ либо обоснованный N/A) — старт-гейт (`guard-subagents.sh --phase start`, D-027) проверяет только стартованные (◐) сессии; «[ ] не начата» — план, гейт её скипает
- [ ] S01, S03 закрыты: `uv run pytest src/tests/unit/test_smoke.py src/tests/unit/test_llm_client.py -q` → passed
- [ ] `make check` зелёный (baseline репозитория)
- [ ] feat-001, feat-003 в state=passing; feat-004 активирована в state=active до старта работ

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
`routing.py`: условные рёбра по state-agent v1.4 — clarify-ветка (флаг из заглушки),
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

- [ ] `uv run python -m apps.cli "<вопрос>"` (FSM-режим) прогоняет вопрос через все узлы-заглушки в правильном порядке и печатает результат (behavior feat-004)
- [ ] LLM не может изменить маршрут: route_flags/счётчики пишет только код, тест-инвариант (behavior feat-004)
- [ ] Попытки ограничены двумя уровнями кода (per-node ≤ 3 + recursion_limit) и глобальными капами ≤ 8 вызовов / ≤ 90 s / ≤ 2 ₽ (behavior feat-004)
- [ ] Все терминальные исходы FSM (state-agent v1.4) достижимы и различимы в payload
- [ ] Дефолтное поведение CLI (тул-луп feat-020, --plain) не изменилось — артефакты ДЗ-02/03 живы
- [ ] Фича закрыта: `make verify-feature F=feat-004` → PASS, state=passing с evidence

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

- ARCH-GATE: [✅ <verdict: compliant | violations | needs-decision> + <план/файл> | N/A — <причина>]
- REVIEW-GATE: [✅ <Ready to merge? вердикт> + <BASE7>..<HEAD7> | N/A — <причина>]

## Отчёт по сессии (заполняется по факту)

- Сделано:
  - [Task: что реально сделано, коммиты, число тестов/ассертов, live-прогон]
- Отклонения от плана:
  - [что сделали не так, как в задаче, и почему; каждое отклонение — осознанное]
- Перенесено в другие сессии:
  - [ID сессии — что перенесено и почему; если в беклог — BL-NN-файл в solution/backlog/ (см. backlog/README.md)]

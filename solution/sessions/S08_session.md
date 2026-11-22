# S08 — clarify: классификация однозначности + interrupt()/checkpointer (веха V4, ДЗ-05)

Этап: Веха V4 (ДЗ-05: clarify-ветвление — FSM v1.4) | Владелец: агент | Зависимости: S04, S03 | Оценка: 1 | Статус: ◐ в работе
Фичи: feat-008 — clarify; зависимость feat-004 (закрывается S04 до старта)
Роль по фиче: закрывает feat-008 — финальная сессия, через make verify-feature

## ⚠️ Прочитай перед началом

Исполнитель: ИИ-агент. К моменту старта: S04 закрыта (каркас FSM: clarify-заглушка
возвращает флаг «уточнение» в терминал clarification; routing жив), S03 закрыта
(LLMProvider с ролями cheap/mid/strong). Эта сессия наполняет второй узел: qwen
(cheap) классифицирует однозначность вопроса; однозначный — допущения фиксируются
в `state.assumptions[]`, неоднозначный — **настоящий `interrupt()` + checkpointer**
(сессия в Valkey, TTL 10 мин): ответ пользователя резюмирует тот же прогон
(state-agent v1.5, S00 контракт 1). Источник «равных кандидатов пути» — verdict
планера (S09 отдавал исход «уточнение» контентом; теперь он поднимается до interrupt).

Границы скоупа: БЕЗ кэш-lookup (после clarify — да, но кэш целиком S16/выпускная),
БЕЗ переписывания вопроса (rewritten_question — поле state есть, мультиход-история
запрещена S00), БЕЗ Streamlit-интерактива (CLI: вопрос→interrupt→ответ пользователя
со stdin), БЕЗ повышения СТАРТОВОЙ роли классификатора (cheap — стартовая по матрице
ADR-007; эскалация попытки 3 — mid при достатке бюджета сохраняется по D-016, ruling R4).

### Правила для исполнителя

1. Ветка `feat-008-clarify` от `milestone/v4` (D-031); merge — в `milestone/v4`.
2. Структурированный выход (ADR-008 слой 2 — тул, прецедент rank_paths S09; ruling R7):
   pydantic-схема вердикта
   (unambiguous/ambiguous/insufficient_data + assumptions[] + вопрос при уточнении),
   temperature 0; невалидный выход → ретрай M1 (попытка 2 — та же модель + текст
   ошибки; попытка 3 — mid при достатке бюджета, D-016).
3. interrupt()/resume — только каркасными средствами LangGraph и только из этого
   узла; checkpointer — Valkey (redis-py, S00 стек), TTL 10 мин — именованная
   константа в Settings с комментарием; TTL истёк → прогон завершается
   clarification-терминалом (валидный исход), не исключением.
4. Допущения пользователя из ответа на уточнение попадают в state ДО продолжения
   прогона и остаются в финальном ответе (interpret_result их раскрывает — S15);
   ключа кэша в этой сессии нет (S16 возьмёт assumptions из state).
5. Тесты — на моках LLM (S00 правило 7); checkpointer в unit — фейк, живой Valkey —
   integration-тест скипается без инфры (conftest-паттерн S02).

## Предусловия

- [x] Сессия СТАРТУЕТ: статус в шапке переведён в «◐ в работе» и ARCH-GATE резолвлен ДО кода (canon-check architect ✅ либо обоснованный N/A) — старт-гейт (`guard-subagents.sh --phase start`, D-027) проверяет только стартованные (◐) сессии; «[ ] не начата» — план, гейт её скипает
- [x] S04 закрыта: `uv run pytest src/tests/unit/test_graph.py -q` → passed (feat-004 passing; 2026-11-22: 38 passed)
- [x] `make check` зелёный (baseline репозитория; 2026-11-22: EXIT=0, 479 passed)
- [ ] feat-004 в state=passing; feat-008 активирована в state=active до старта работ

## Задачи

### Task 1: Сервис clarify — классификация + допущения

`application/services/clarify_service.py`: промпт с retrieval-контекстом вопроса
(VectorStore-хиты уже есть как паттерн S09 — контекст по правам), вызов cheap-роли,
структурированный выход через тул (ADR-008 слой 2, ruling R7 — не JSON-текст-парсинг);
однозначен → вердикт + `assumptions[]` (явные допущения фиксирует
код-валидатор схемы, не вольный текст; VO в domain — ruling R5); неоднозначен/нехватка
данных → вердикт +
один уточняющий вопрос (ровно один — behavior фичи). Ретраи M1 по правилу 2.

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_clarify.py -q   # однозначный → допущения; неоднозначный → 1 вопрос; невалидный выход LLM → ретрай M1
```

---

### Task 2: interrupt() + checkpointer Valkey (TTL 10 мин)

Wiring графа: checkpointer на Valkey (serialization state, ключ = thread/session_id;
кастомный BaseCheckpointSaver на redis-py + JsonPlusSerializer — без новой
зависимости, ruling R2), checkpointer — параметр build_fsm_app → compile();
thread_id — параметр раннера (config.configurable, не поле state — ruling R2).
Узел clarify при ambiguous-вердикте вызывает interrupt() с вопросом; CLI FSM-режима:
interrupt печатает вопрос и ждёт ответ (stdin) → resume того же прогона; ответ
становится допущением. TTL 10 мин: истёкшая сессия → clarification-терминал
(валидный), живая → продолжение того же thread (никаких новых прогонов);
TTL-истёк синтезирует раннер (граница движка, ruling R3). Раннер при resume
перевзводит deadline_at (90 s — кап обработки, не ожидания пользователя — ruling R6).

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_clarify.py -q                          # фейк-checkpointer: interrupt → resume того же state
make infra-up && uv run pytest src/tests/integration/test_clarify_resume.py -q   # живой Valkey: resume после TTL-окна; истёкший TTL → clarification-терминал
```

---

### Task 3: Подключение в FSM — ветвление после clarify

Наполнение заглушки S04: routing читает вердикт — unambiguous → plan_joins
(допущения уже в state), ambiguous → interrupt-механика Task 2. Сшивка с планером:
исход «уточнение» из plan_joins (S09: равные кандидаты) поднимается до повторного
входа clarify с контекстом «равные кандидаты пути: X vs Y». Ребро plan_joins → clarify
в FSM НЕ существовало (диаграмма v1.5 его не рисует, routing.py его не имеет —
канон-противоречие hw-01:226 vs диаграмма, FIX-009): закладываем ребро в S08 по
прецеденту S04 Ловушка-1 («ветки в каркасе, активирует S14/S17») — новый флаг
FLAG_PLAN_JOINS_CLARIFY (владение — планер, ruling R1); диаграмма state-agent v1.6 —
в том же коммите; активирует S10 (FSM-подключение plan_joins).

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_graph.py src/tests/unit/test_clarify.py -q   # ветвление после clarify; ребро plan_joins→clarify живо
```

## Критерии приёмки

- [ ] Однозначный вопрос проходит дальше с зафиксированными допущениями (behavior feat-008)
- [ ] Неоднозначный — приостанавливает граф interrupt()-ом (checkpointer, TTL 10 мин), ответ пользователя резюмирует тот же прогон (behavior feat-008)
- [ ] Допущения попадают в state (и далее — в финальный ответ, поле готово для S15) (behavior feat-008)
- [ ] TTL истёк → clarification-терминал — валидный исход, не traceback (S00 правило 8)
- [ ] Уточняющий вопрос — ровно один (behavior feat-008), формулировка без раскрытия недоступной схемы
- [ ] Фича закрыта: `make verify-feature F=feat-008` → PASS, state=passing с evidence

## Ловушки

1. **interrupt без checkpointer = GraphInterrupt наружу** — в S04 его не было;
   здесь wiring обязателен, иначе CLI получит исключение. Собирайте граф через
   Container с checkpointer-параметром (подмена фейком в тестах — S00 правило 7).
2. LangGraph resume: ответ пользователя передаётся resume-значением в тот же thread;
   случайный новый invocation = новый прогон (допущения потеряются) — тест «тот же
   thread_id до/после».
3. reasoning-only выход шлюза (D-021): для JSON-контракта это CoT — структурный
   парсер обязан упасть и уйти в ретрай, а не «понять» текст (урок S13/S19).
4. Допущения — структурированный список (какой фильтр применён, какая атрибуция),
   не свободная проза: их будет сверять кэш (S16) и раскрывать interpret (S15).
5. Valkey TTL проверять на integration-паттерне S02 (skip без инфры с понятной причиной) —
   не спать 10 минут в тесте: TTL-константа инъектируется коротким значением.

## Гейты субагентов (заполняется по факту, до закрытия сессии)

- ARCH-GATE: ✅ verdict needs-decision → rulings R1–R7 закрыты до кода (canon-check architect 2026-11-22, план S08_session.md: ребро plan_joins → clarify закладывается новым флагом FLAG_PLAN_JOINS_CLARIFY + диаграмма v1.6 в том же коммите, противоречие hw-01:226 vs state-agent v1.5 — FIX-009 — R1; кастомный BaseCheckpointSaver на redis-py без новой зависимости, thread_id — параметр раннера — R2; расширение порта GraphRunner run(…, thread_id)/resume + TTL-истёк синтезирует раннер, S00-таблица портов в том же коммите — R3; стартовая роль cheap, эскалация попытки 3 по D-016 сохраняется — R4; assumption — pydantic VO {source, text} в domain + S00 стык 1 синхрон — R5; капы бюджета/вызовов продолжаются после resume, deadline_at перевзводится раннером — R6; вердикт классификатора через тул по ADR-008 слой 2 — R7)
- REVIEW-GATE: [✅ <Ready to merge? вердикт> + <BASE7>..<HEAD7> | N/A — <причина>]

### Rulings ARCH-GATE (canon-check architect 2026-11-22 — verdict needs-decision, закрыты до кода)

- **R1 (ребро plan_joins → clarify):** посылка плана «ребро существует в FSM v1.4» ложна — routing.py его не имеет, диаграмма v1.5 не рисует; hw-01:226 («на равных кандидатах — clarify») конфликтуeт с диаграммой → противоречие канона — [solution/FIXES.md](../FIXES.md) FIX-009. Ребро закладывается в S08 по прецеденту S04 Ловушка-1 (ветки в каркасе, активирует S14/S17): новый флаг `FLAG_PLAN_JOINS_CLARIFY` (переиспользование `FLAG_CLARIFICATION_NEEDED` запрещено — флаг узла clarify, карта владения; «узел пишет только свои поля»); диаграмма state-agent → v1.6 в том же коммите (docs-in-same-commit); активирует S10.
- **R2 (checkpointer):** кастомный `BaseCheckpointSaver` на redis-py (redis>=6.0 в pyproject; JsonPlusSerializer — транзитивная зависимость langgraph) — без новой зависимости; официальный langgraph-checkpoint-redis — дрейф стека, отклонён. Встраивание: параметр `checkpointer` в build_fsm_app → `compile(checkpointer=...)`; адаптер — infrastructure, сборка — composition. `thread_id` — параметр раннера (config.configurable), НЕ поле state (стык 1 закрыт; deadline_at в state потому, что его читает гвард рёбер — thread_id рёбрам не нужен).
- **R3 (порт GraphRunner):** расширение run(state, *, thread_id)/resume(answer, *, thread_id) — правомочно (прецедент расширения сигнатуры порта: S00:84 VectorStore N1/S06; S19/S25 — LLMProvider). Контракт «run обязан вернуть outcome» переформулирован («outcome заполнен либо прогон приостановлен» + маркер приостановки); таблица портов S00 + docstring порта — в том же коммите. TTL-истёк → clarification-терминал синтезирует раннер (orchestration — граница движка, app.get_state; «исключения движка не проходят наружу»); CLI передаёт раннеру исходный вопрос для payload (чекпоинт истёк — state восстановить неоткуда).
- **R4 (роль классификатора):** противоречие мнимое: «БЕЗ повышения роли» = стартовая роль остаётся cheap по матрице ADR-007 §4; M1-эскалация попытки 3 (mid при остатке бюджета с резервом 0,6 ₽) — отдельный канон (S00 стык 2, ADR-007 §4.1, прецедент кода `_ROLE_UP` plan_joins_service). Формулировка границы скоупа уточнена.
- **R5 (схема допущений):** assumption — pydantic-VO в domain {source: "classifier"|"user_answer", text: str} — «структурированный список» = машиночитаемость для сверки кэшем (S16) и раскрытия interpret (S15); ответ пользователя свободен по природе, маркер source даёт провенанс. AgentState.assumptions (сейчас list[str]) + S00 стык 1 — синхрон в том же коммите (правило 3: изменение state-схемы через контракты стыка). Допущения классификатора — закрытый набор, валидирует код. JoinPlan.assumptions — отдельное поле другого владельца, не унифицировать.
- **R6 (капы vs interrupt):** капы считаются по state (budget_used/deadline_at), checkpointer персистит state → повторный вход = тот же прогон, счётчики не сбрасываются («ответ пользователя резюмирует тот же прогон», S00 стык 1). Коллизия fsm_deadline_s=90 < TTL 600: раннер перевзводит deadline_at при resume (его поле, его владение; 90 s — кап обработки, не ожидания пользователя) + тест. clarify при resume ре-исполняется (механика interrupt) — узел не делает второй LLM-вызов при наличии resume-значения.
- **R7 (механизм структурированного выхода):** вердикт классификатора — через тул (ADR-008 вариант C слой 2, tool-based enforcement; прецедент — тул rank_paths plan_joins_service); JSON-текст-парсинг = отвергнутый вариант B ADR-008. pydantic ниже — валидация СУЩНОСТИ, формат гарантирует механизм тула.

## Отчёт по сессии (заполняется по факту)

- Сделано:
  - [Task: что реально сделано, коммиты, число тестов/ассертов, live-прогон]
- Отклонения от плана:
  - [что сделали не так, как в задаче, и почему; каждое отклонение — осознанное]
- Перенесено в другие сессии:
  - [ID сессии — что перенесено и почему; если в беклог — BL-NN-файл в solution/backlog/ (см. backlog/README.md)]

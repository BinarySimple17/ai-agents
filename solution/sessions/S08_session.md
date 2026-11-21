# S08 — clarify: классификация однозначности + interrupt()/checkpointer (веха V4, ДЗ-05)

Этап: Веха V4 (ДЗ-05: clarify-ветвление — FSM v1.4) | Владелец: агент | Зависимости: S04, S03 | Оценка: 1 | Статус: [ ] не начата
Фичи: feat-008 — clarify; зависимость feat-004 (закрывается S04 до старта)
Роль по фиче: закрывает feat-008 — финальная сессия, через make verify-feature

## ⚠️ Прочитай перед началом

Исполнитель: ИИ-агент. К моменту старта: S04 закрыта (каркас FSM: clarify-заглушка
возвращает флаг «уточнение» в терминал clarification; routing жив), S03 закрыта
(LLMProvider с ролями cheap/mid/strong). Эта сессия наполняет второй узел: qwen
(cheap) классифицирует однозначность вопроса; однозначный — допущения фиксируются
в `state.assumptions[]`, неоднозначный — **настоящий `interrupt()` + checkpointer**
(сессия в Valkey, TTL 10 мин): ответ пользователя резюмирует тот же прогон
(state-agent v1.4, S00 контракт 1). Источник «равных кандидатов пути» — verdict
планера (S09 отдавал исход «уточнение» контентом; теперь он поднимается до interrupt).

Границы скоупа: БЕЗ кэш-lookup (после clarify — да, но кэш целиком S16/выпускная),
БЕЗ переписывания вопроса (rewritten_question — поле state есть, мультиход-история
запрещена S00), БЕЗ Streamlit-интерактива (CLI: вопрос→interrupt→ответ пользователя
со stdin), БЕЗ повышения роли классификатора (cheap — стартовая по матрице ADR-007).

### Правила для исполнителя

1. Ветка `feat-008-clarify` от `milestone/v4` (D-031); merge — в `milestone/v4`.
2. Структурированный выход (ADR-008): pydantic-схема вердикта
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

- [ ] Сессия СТАРТУЕТ: статус в шапке переведён в «◐ в работе» и ARCH-GATE резолвлен ДО кода (canon-check architect ✅ либо обоснованный N/A) — старт-гейт (`guard-subagents.sh --phase start`, D-027) проверяет только стартованные (◐) сессии; «[ ] не начата» — план, гейт её скипает
- [ ] S04 закрыта: `uv run pytest src/tests/unit/test_graph.py -q` → passed (feat-004 passing)
- [ ] `make check` зелёный (baseline репозитория)
- [ ] feat-004 в state=passing; feat-008 активирована в state=active до старта работ

## Задачи

### Task 1: Сервис clarify — классификация + допущения

`application/services/clarify_service.py`: промпт с retrieval-контекстом вопроса
(VectorStore-хиты уже есть как паттерн S09 — контекст по правам), вызов cheap-роли,
pydantic-выход; однозначен → вердикт + `assumptions[]` (явные допущения фиксирует
код-валидатор схемы, не вольный текст); неоднозначен/нехватка данных → вердикт +
один уточняющий вопрос (ровно один — behavior фичи). Ретраи M1 по правилу 2.

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_clarify.py -q   # однозначный → допущения; неоднозначный → 1 вопрос; невалидный выход LLM → ретрай M1
```

---

### Task 2: interrupt() + checkpointer Valkey (TTL 10 мин)

Wiring графа: checkpointer на Valkey (serialization state, ключ = thread/session_id),
узел clarify при ambiguous-вердикте вызывает interrupt() с вопросом; CLI FSM-режима:
interrupt печатает вопрос и ждёт ответ (stdin) → resume того же прогона; ответ
становится допущением. TTL 10 мин: истёкшая сессия → clarification-терминал
(валидный), живая → продолжение того же thread (никаких новых прогонов).

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
входа clarify с контекстом «равные кандидаты пути: X vs Y» (ребро plan_joins → clarify
существует в FSM v1.4 — проверить, что заглушка S04 его уже несёт).

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

- ARCH-GATE: [✅ <verdict: compliant | violations | needs-decision> + <план/файл> | N/A — <причина>]
- REVIEW-GATE: [✅ <Ready to merge? вердикт> + <BASE7>..<HEAD7> | N/A — <причина>]

## Отчёт по сессии (заполняется по факту)

- Сделано:
  - [Task: что реально сделано, коммиты, число тестов/ассертов, live-прогон]
- Отклонения от плана:
  - [что сделали не так, как в задаче, и почему; каждое отклонение — осознанное]
- Перенесено в другие сессии:
  - [ID сессии — что перенесено и почему; если в беклог — BL-NN-файл в solution/backlog/ (см. backlog/README.md)]

# S14 — diagnose_error + retry-цикл + execute_sql в FSM (веха V4, ДЗ-05)

Этап: Веха V4 (ДЗ-05: цикл самокоррекции в машине) | Владелец: агент | Зависимости: S13, S04 | Оценка: 1 | Статус: [ ] не начата
Фичи: feat-014 — diagnose_error + retry-цикл; зависимости feat-013 (passing), feat-004 (закрывается S04 до старта)
Роль по фиче: закрывает feat-014 — финальная сессия, через make verify-feature

## ⚠️ Прочитай перед началом

Исполнитель: ИИ-агент. К моменту старта: S13 закрыта (порт SQLExecutor + сервис
`execute_sql_node.py` с заготовкой рёбер diagnose_error/probe/compact_result и чисткой
стейла при ошибке — туда встаёт FSM-подключение), S04 закрыта (каркас: рёбра
`execute_sql → diagnose_error/probe/merge|compact_result` и `diagnose_error →
generate_sql | degrade` существуют), S10 закрыта (generate_sql принимает retry-вход
«последний SQL + текст ошибки»). Эта сессия замыкает цикл самокоррекции ADR-002 §4:
`generate → validate → execute → diagnose → generate` — внутри машины. Узел
execute_sql (feat-013) встаёт в граф; probe COUNT — код после execute при 0 строк.

Границы скоупа: БЕЗ ретрая шага декомпозиции (ребро diagnose_error → generate_step_sql —
заглушка до feat-017, выпускная; в базовом цикле не достижимо), БЕЗ interpret/degrade
как текстов (S15; здесь — только вердикты-классы), БЕЗ расширенной диагностики
эмоций/причин (разбор ошибки — cheap-модель, один pydantic-выход).

### Правила для исполнителя

1. Ветка `feat-014-diagnose-error` от `milestone/v4` (D-031); merge — в `milestone/v4`.
2. Разбор ошибки — qwen (cheap): класс (syntax/semantics/timeout/empty/permissions
   — по SQLFailureClass S13) + краткая подсказка регенерации (pydantic, ADR-008);
   подсказка уходит в retry-контекст generate_sql (S10), не в обход валидатора.
3. Ретраи M1 (D-016, ADR-007 §4.1): ≤ 3 попыток на узел; попытка 2 — та же модель +
   текст ошибки; попытка 3 — уровень вверх ТОЛЬКО после проверки остатка бюджета с
   резервом 0,6 ₽; эскалацию назначает код (retry_count + route_flags), не модель.
4. Исчерпание любого предохранителя (per-node ≤ 3, recursion_limit, капы ≤ 8/90 s/2 ₽)
   → валидный отказ с уточняющим вопросом (fail-терминал), не бесконечный цикл и
   не traceback наверх (behavior фичи).
5. Пустой результат ≠ ошибка: 0 строк → probe COUNT (код, не LLM): подтверждённый
   ноль — валидный пустой ответ (short_circuit-путь), подозрение на потерянный
   джойн (COUNT по факту > 0 в связанной таблице) → diagnose_error (state-agent v1.4).

## Предусловия

- [ ] Сессия СТАРТУЕТ: статус в шапке переведён в «◐ в работе» и ARCH-GATE резолвлен ДО кода (canon-check architect ✅ либо обоснованный N/A) — старт-гейт (`guard-subagents.sh --phase start`, D-027) проверяет только стартованные (◐) сессии; «[ ] не начата» — план, гейт её скипает
- [ ] S13 закрыта: `make infra-up && uv run pytest src/tests/integration/test_execute_sql.py -q` → passed (feat-013 passing)
- [ ] S04 закрыта: `uv run pytest src/tests/unit/test_graph.py -q` → passed (feat-004 passing)
- [ ] `make check` зелёный (baseline репозитория)
- [ ] feat-013, feat-004 в state=passing; feat-014 активирована в state=active до старта работ

## Задачи

### Task 1: Сервис diagnose_error — разбор + подсказка

`application/services/diagnose_error_service.py`: вход — ошибка (SQLFailureClass +
текст от SQLExecutor S13 / вердикт валидатора S11 / probe-подозрение) + последний
SQL; вызов cheap-роли; pydantic-выход: класс + подсказка регенерации (что исправить,
одним абзацем) + флаг «повтор бессмыслен» (permissions-класс → сразу degrade, LLM
не «уговаривает» валидатор — ADR-005). Невалидный выход LLM → ретрай M1.

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_diagnose.py -q   # разбор на моках; подсказка в retry-контекст; permissions-класс → без LLM-ретрая
```

---

### Task 2: execute_sql в FSM + probe COUNT

Подключение сервиса S13 в граф (наполнение заглушки): успех → compact_result-заглушка
(S15 наполнит); SQL-ошибка → diagnose_error; 0 строк → probe COUNT (код: COUNT по
таблицам плана; повторный вызов через SQLExecutor под agent_ro). Подозрение на
потерянный джойн: строки есть в корневой таблице, а в джойне 0 — вердикт в
diagnose_error с контекстом. RLS/капы канала S13 не расширяются.

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_diagnose.py src/tests/unit/test_execute_sql_node.py -q   # рёбра execute_sql; probe-ветки (пусто/подозрение) на фейках
```

---

### Task 3: Retry-контур в машине + честные исходы

Замыкание цикла: routing ведёт diagnose → generate_sql с retry-контекстом (последний
SQL + подсказка), счётчик per-node инкрементится кодом ребра; попытка 3 — эскалация
после бюджетной проверки (шов S09/S10); исчерпание → fail-терминал с уточняющим
вопросом; рекурсion_limit-страховка S04 остаётся вторым предохранителем (тест
«счётчики сломаны → recursion_limit ловит» — намеренно скормить зацикливающий мок).

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_graph.py src/tests/unit/test_diagnose.py -q   # цикл ≤ 3 попыток; эскалация по бюджету; fail — валидный payload
```

---

### Task 4: Интеграционная проверка цикла на живой БД

Integration-тест (скип без инфры): сгенерированный SQL с ошибкой (мок LLM отдаёт
сломанный, потом исправленный SQL) → execute падает → diagnose → retry → успех;
та же схема с исчерпанием счётчика → fail-терминал. Полный путь через собранный
Container, не напрямую сервисы.

**✅ Самопроверка:**

```bash
make infra-up && uv run pytest src/tests/integration/test_retry_cycle.py -q   # живой прогон цикла: падение→диагноз→регенерация→успех; исчерпание→fail
```

## Критерии приёмки

- [ ] После ошибки SQL в контекст generate_sql уходит только последний SQL + разбор ошибки (behavior feat-014)
- [ ] Попытка 2 — та же модель + текст ошибки, попытка 3 — эскалация после проверки остатка бюджета (behavior feat-014)
- [ ] Исчерпание — валидный отказ с уточняющим вопросом, не бесконечный цикл; recursion_limit — второй предохранитель (behavior feat-014)
- [ ] Узел execute_sql подключён в FSM: SQL-ошибка → diagnose_error, 0 строк → probe COUNT (feat-013 в V4, behavior feat-014)
- [ ] Probe: реально пусто — валидный пустой ответ; подозрение на потерянный джойн — в diagnose_error (state-agent v1.4)
- [ ] Фича закрыта: `make verify-feature F=feat-014` → PASS, state=passing с evidence

## Ловушки

1. **Retry не должен обходить валидатор:** путь diagnose → generate → VALIDATE →
   execute — если провести ребро diagnose → execute напрямую, вся безопасность
   ADR-002 §4 (валидатор на единственном ребре к исполнению) разрушена; тест
   недостижимости execute из diagnose.
2. Пустой результат — не ошибка (0 строк — норма, S24-фикс №1б: «отсутствие строк —
   не повод отказываться»); не гоняйте diagnose на честном нуле — probe решает.
3. probe COUNT выполняется тем же каналом agent_ro (капы LIMIT/timeout S13) — не
   отдельным «служебным» подключением; это тоже пользовательский SQL по счётчику вызовов.
4. Бюджет проверяется ДО эскалации, не после (иначе кап 2 ₽ пробивается третьей
   попыткой флагмана + interpret — расчёт ADR-007 §4.1 с резервом 0,6 ₽).
5. Подсказка diagnose — текст для промпта генерации, не патч SQL: код не применяет
   «исправления» модели к SQL сам (SQL пишет только generate_sql).
6. Гейт strict: строки ARCH/REVIEW — по факту; «N/A — сессия не начата» = FAIL.

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

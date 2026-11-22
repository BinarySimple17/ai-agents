# S14 — diagnose_error + retry-цикл + execute_sql в FSM (веха V4, ДЗ-05)

Этап: Веха V4 (ДЗ-05: цикл самокоррекции в машине) | Владелец: агент | Зависимости: S13, S04 | Оценка: 1 | Статус: ◐ в работе (старт 2026-11-22, ветка feat-014-diagnose-error)
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
4. Исчерпание любого предохранителя — валидный отказ, не бесконечный цикл и не
   traceback наверх (behavior фичи); носитель отказа — по канону (ruling R2):
   исчерпание ретрай-цикла генератора → degrade с уточняющим вопросом
   (routing.route_diagnose_error; ruling R2 S10 — терминал fail исходит только
   из plan_joins), recursion_limit → fail (перехват раннера, ADR-002 v1.2),
   глобальные капы ≤ 8/90 s/2 ₽ → budget_stop (routing.llm_guard).
5. Пустой результат ≠ ошибка: 0 строк → probe COUNT (код, не LLM): подтверждённый
   ноль — валидный пустой ответ (compact_result-путь — state-agent v1.7;
   short_circuit — терминал только шагового цикла, пустой key-set шага,
   FIX-008), подозрение на потерянный джойн (COUNT по факту > 0 в связанной
   таблице) → diagnose_error (state-agent v1.5).

## Предусловия

- [x] Сессия СТАРТУЕТ: статус в шапке переведён в «◐ в работе» и ARCH-GATE резолвлен ДО кода (canon-check architect ✅ либо обоснованный N/A) — старт-гейт (`guard-subagents.sh --phase start`, D-027) проверяет только стартованные (◐) сессии; «[ ] не начата» — план, гейт её скипает (старт 2026-11-22)
- [x] S13 закрыта: `make infra-up && uv run pytest src/tests/integration/test_execute_sql.py -q` → passed (feat-013 passing; 79 passed вместе с test_graph на живой инфре при старте)
- [x] S04 закрыта: `uv run pytest src/tests/unit/test_graph.py -q` → passed (feat-004 passing)
- [x] `make check` зелёный (baseline репозитория; `./init.sh` EXIT=0 после ARCH-GATE и перед закрытием — 817 passed)
- [x] feat-013, feat-004 в state=passing; feat-014 активирована в state=active до старта работ

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
таблицам плана; повторный вызов через SQLExecutor под agent_ro; факт пробы — событие
трейса, ruling R5). Подозрение на потерянный джойн: строки есть в корневой таблице,
а в джойне 0 — вердикт в diagnose_error с контекстом; подтверждённый ноль — валидный
пустой ответ → compact_result (ruling R1: не short_circuit). Маршрут —
routing.route_execute_sql (единственный источник; заготовка execute_sql_route из
S13 не подключается — ruling R4, retire). RLS/капы канала S13 не расширяются.

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

BL-016 (P2, re-review-2 S10; триаж 2026-11-22; **закрыт ruling'ом R3 ARCH-GATE**):
generation-fail на FSM-ретрай-визите (попытка 2, mid) раньше уходил в degrade без
эскалации — теперь различается: generation-fail (битый выход модели, ретрай
оправдан) на ретрай-визите при незатраченном fuse → РЕБРО generate_sql →
retry_generate_sql (гейт, инкремент кодом ребра) → визит 3 — эскалация strong в
сервисе с бюджетной проверкой; исчерпание/бюджет/шлюз недоступен →
FLAG_GENERATE_SQL_FAILED → degrade (R2 сохраняется в своей зоне — носитель отказа
при исчерпании/инфраструктуре). Детур через diagnose_error отвергнут (битый выход
генератора самодиагностируем — сервис уже несёт текст ошибки в retry-контексте;
механический транзит мимо diagnose сэкономил бы вызов, но добавил бы холостой узел).
Синх state-agent → v1.8 в том же коммите. Определение «попытки узла» (ruling R6):
попытка M1 = шаг политики ролей (1 — стартовая, 2 — та же + ошибка, 3 — вверх);
на первом визите шаги крутит внутренний цикл (≤ 3 вызовов), на FSM-ретрай-визитах —
по одной попытке (гейт считает визиты, fuse ≤ 3); потолок генератора — 5 LLM-вызовов,
абсолютный рубеж — llm_guard ≤ 8; зафиксировано в docstring m1_policy, передаётся
S15/BudgetGuard. Запись BL-016 закрыть/обновить здесь.

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

- [x] После ошибки SQL в контекст generate_sql уходит только последний SQL + разбор ошибки (behavior feat-014; unit test_hint_flows_into_generate_retry_context + FSM test_hallucination_retry_succeeds_on_attempt2 + live test_execute_error_retry_succeeds_on_attempt2)
- [x] Попытка 2 — та же модель + текст ошибки, попытка 3 — эскалация после проверки остатка бюджета (behavior feat-014; сервисные тесты M1 S10 + шов Q7 в diagnose_service; FSM-визиты — счётчик гейта, BL-016/R3 — эскалация сильной ролью на визите 3 во всех ветках)
- [x] Исчерпание — валидный отказ с уточняющим вопросом, не бесконечный цикл; recursion_limit — второй предохранитель (behavior feat-014; FSM test_exhaustion_yields_valid_degrade [3 визита → degrade] + test_broken_counters_caught_by_recursion_limit [сломанные счётчики → GraphRecursionError → fail]; live: budget_stop на капе 8)
- [x] Узел execute_sql подключён в FSM: SQL-ошибка → diagnose_error, 0 строк → probe COUNT (feat-013 в V4, behavior feat-014; wiring container + route_execute_sql; live done-прогон)
- [x] Probe: реально пусто — валидный пустой ответ; подозрение на потерянный джойн — в diagnose_error (state-agent v1.5; unit probe-ветки + live test_empty_result_probe_lost_join_retries_to_success)
- [ ] Фича закрыта: `make verify-feature F=feat-014` → PASS, state=passing с evidence (после REVIEW-GATE — гейт требует заполненного REVIEW-GATE)

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

- ARCH-GATE: ✅ needs-decision → rulings R1–R6 закрыты ДО кода (canon-check architect 2026-11-22; R1 — probe-ноль → compact_result, не short_circuit; R2 — носители исчерпания по канону; R3 — BL-016: ребро generate_sql → retry_generate_sql, state-agent v1.8; R4 — retire заготовки execute_sql_route; R5 — трейс-события diagnose.*/execute.probe; R6 — определение «попытки узла» в m1_policy; правки правил 4/5 и Task 2/3 плана — секция Rulings в этом файле)
- REVIEW-GATE: ✅ Ready to merge: Yes (scoped re-review fix-волны eff8162: все 5 находок волны-1 [0C/1I/4M] закрыты по существу, новые тесты кусают до-фиксовый код [проверено ревьюером против eff8162^], фикс Minor-2 попутно устранил реальный риск stale-RETRYABLE на ребре ретрая) — диапазон acbb42e..1107312 + fix-волна eff8162; цикл: ревью-1 «With fixes» (0C/1I/4M — S9-инвариант probe-фактов в пользовательском message) → fix-волна eff8162 (message фиксированный, факты — внутренний error["probe"] → промпт диагностики; parity шаговых флагов с routing-константами; симметричная очистка RETRYABLE; тип executor — SQLExecutor; осмысленные detail) → re-review-2 Yes; новый Minor (дублирование литерала step-флагов) → BL-019, nit (инлайн-комментарий entities) — закрыт в коммите закрытия

### Rulings ARCH-GATE S14 (canon-check 2026-11-22, до первого код-коммита)

- **R1** — probe-подтверждённый ноль → compact_result (пустой ответ строится через
  interpret_result — state-agent v1.7); short_circuit — терминал только шагового
  цикла (пустой key-set шага). Правка правила 5/Task 2 плана (canon-check warning 1).
- **R2** — носители исчерпания по канону: ретрай-цикл генератора → degrade
  (ruling R2 S10), recursion_limit → fail (раннер), капы → budget_stop. Правка
  правила 4 плана (canon-check warning 2).
- **R3** — BL-016: generation-fail на FSM-ретрай-визите при незатраченном fuse →
  ребро generate_sql → retry_generate_sql (гейт) → визит 3 strong (бюджет-проверка
  в сервисе); исчерпание/бюджет/шлюз → FLAG_GENERATE_SQL_FAILED → degrade (R2 в
  своей зоне). Детур через diagnose_error отвергнут. Синх state-agent v1.8 — в том
  же коммите (canon-check warning 3).
- **R4** — заготовка execute_sql_route (S13) в граф не подключается: маршрут —
  routing.route_execute_sql (единственный источник рёбер); заготовка удаляется
  (probe — код визита execute_sql, FLAG_PROBE_LOST_JOIN).
- **R5** — трейс-события: diagnose.verdict / diagnose.unavailable (S9-прецедент
  generate_sql.verdict/unavailable) + факт пробы execute.probe — порт Tracer +
  observability-каталог тем же коммитом (canon-check note).
- **R6** — «попытка узла»: попытка M1 = шаг политики ролей (1/2/3); первый визит —
  внутренний цикл ≤ 3 вызовов, FSM-ретрай-визит — одна попытка (гейт считает
  визиты, fuse ≤ 3); потолок генератора 5 LLM-вызовов, абсолютный рубеж llm_guard
  ≤ 8; docstring m1_policy + передача в S15/BudgetGuard (BL-016 смежное).

## Отчёт по сессии (заполняется по факту)

- Сделано:
  - Task 1 (9acb657): `diagnose_error_service.py` — два контура: futile-классы КОДОМ без LLM (permission_denied/read_only/acl_denied → ADR-005, connection → S00-8; retry_futile — карта кода, не поле модели), остальные — qwen/cheap structured output через тул `report_diagnosis` (ADR-008 слой 2); ретраи M1 (попытка 3 — cheap→mid при остатке > резерва 0,6 ₽, шов Q7); `diagnose_error_node.py` — glue: fail-closed дефекты каркаса, fuse ≤ 3 зеркалом attempts_used, `state.diagnosis` {error_class, hint, retry_futile} (НОВОЕ поле AgentState + S00 стык 1 синк), `state.error` не трогается (S9); подсказка доходит до retry-контекста generate_sql (D-016); события diagnose.verdict/unavailable (порт Tracer + observability); 25 unit.
  - Task 2 (516e582): `make_execute_sql_node` — FSM-glue исполнителя S13: RunContext из state, defense-in-depth (SQL только от validate_sql), probe COUNT при 0 строк одиночного запроса тем же каналом agent_ro (COUNT по каждой таблице плана; все > 0 → FLAG_PROBE_LOST_JOIN → diagnose, любая пустая → валидный пустой ответ → compact_result [ruling R1 — НЕ short_circuit]; сбой пробы — вердикта нет); дефекты каркаса → FLAG_EXECUTE_DEFECT → degrade без diagnose (новый флаг routing, прецедент resolve_access → rejected); заготовка execute_sql_route (S13) удалена (ruling R4); wiring container + инъекция executor; события execute.probe/unavailable; 22 unit.
  - Task 3 (6ccca71): ребро generate_sql → retry_generate_sql (BL-016, ruling R3) — `GenerateSqlResult.fail_kind` (generation|unavailable), флаг generate_sql_retryable ставит ТОЛЬКО узел на FSM-ретрай-визите, route_generate_sql проверяет fuse; diagnose_error подключён в FSM (DiagnoseService со швом Q7, инъекция diagnose_service); TestRetryCycle — 6 FSM-сценариев на реальных узлах S11/S14 (галлюцинация→ретрай→успех с проверкой retry-контекста, исчерпание → degrade за 3 визита, diagnose-недоступен → без ретрая, execute-ошибка → цикл замыкается, Ловушка 1 [diagnose → execute недостижимо — аудит routing], recursion-страховка [сломанные счётчики → fail]); state-agent → v1.8; m1_policy docstring (R6); BL-016 закрыт; unit 735.
  - Task 4 (2d956a5): `test_retry_cycle.py` — 3 живых сценария через собранный Container (живой agent_ro-канал, живые права/каталог): execute-ошибка → ретрай → успех; исчерпание → degrade; 0 строк → probe COUNT live (orders: 400, order_items: 1000 → потерянный джойн) → ретрай → успех.
  - Layer-3 (live CLI, LM Studio qwen3.8-9b): «Сколько всего заказов в базе?» → done (полный happy path 10 узлов, живой execute_sql); «Сколько заказов у клиентов из РФ?» → execute-ошибка (orders.country не существует — галлюцинация колонки) → diagnose.verdict (LLM) → ретрай попыткой 2 → снова ошибка → попытка 3 → исчерпание → кап 8 вызовов → budget_stop (валидный исход, exit 0, не traceback) — цикл самокоррекции и предохранители работают live; `make verify-feature F=feat-014` → PASS (после REVIEW-GATE); `./init.sh` EXIT=0 — 817 passed (735 unit + 82 integration).
- Отклонения от плана:
  - Заготовка execute_sql_route (S13) не «не подключена», а УДАЛЕНА (ruling R4, canon-check note): единственный источник рёбер — routing.route_execute_sql; мёртвый код в сервисе остаться не должен.
  - Новое поле AgentState.diagnosis (сверх карты стыка в плане — план говорил «подсказка уходит в retry-контекст», механизм не был зафиксирован): внутренняя подсказка отделена от пользовательского state.error.message (S9, прецедент state.validation.issues S11); синк S00 стык 1 тем же коммитом.
  - Новый флаг FLAG_EXECUTE_DEFECT + ветка degrade в route_execute_sql (сверх плана): дефекты каркаса на execute_sql (identity/sql/скелет) не должны идти в diagnose (LLM-разбор дефекта подмены бессмыслен и тратит вызов) — прецедент FLAG_IDENTITY_FAILED; canon-check warning 1/2 не затрагивает (терминалы не менялись).
  - Probe строится по таблицам СКЕЛЕТА (= план) без mandatory_predicates: COUNT отвечает «есть ли данные в таблице вообще» (ложное срабатывание при фильтрах, отсёкших всё, — допустимо: исход diagnose → ретрай ограничен fuse; выбор задокументирован в docstring).
  - Клиент wiring: build_container + инъекции executor и diagnose_service (прецедент permission_store) — юнит-wiring без БД.
- Перенесено в другие сессии:
  - S15/BudgetGuard — определение «попытки узла» (ruling R6: визиты vs внутренние M1, потолок 5 вызовов, рубеж ≤ 8) и ₽-аккумулятор в state.budget_used (сейчас резидент адаптера — шов Q7); формулировки отказов futile-классов пользователю (diagnosis.hint — внутренний, бизнес-текст — degrade S15).
  - S27 (сборка V4) — live-сцена «retry-самокоррекции» уже воспроизводима (см. layer-3): сценарий «Сколько заказов у клиентов из РФ?» показывает полный цикл с budget_stop.

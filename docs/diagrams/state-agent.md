# State Diagram — агент Text-to-SQL (FSM)

- **Статус:** каноническая схема v1.2 (2026-11-16 — валидация JoinPlan + эскалация plan_joins; v1.1 — ревизия 02: позиция кэша, кольцо T2)
- **Требования prd.md:** FR-1…FR-14; переходы контролирует код — модель не может перепрыгнуть валидацию или уйти из цикла (ADR-002)
- **Связанные:** ADR-004 (ветка декомпозиции), ADR-005 (degrade vs diagnose), ADR-007 (эскалация на retry), ADR-009 (кэш); c4-l3-agent.md (компоненты узлов), sequence-query.md (интерактивный путь целиком)

```mermaid
stateDiagram-v2
    [*] --> resolve_access: вопрос + identity из проверенного токена
    resolve_access --> clarify: права в state (scope, allowed_tables/columns)
    resolve_access --> rejected: identity не проверена — fail-closed

    clarify --> clarification: неоднозначен → один уточняющий вопрос
    clarification --> [*]: валидный результат (done)
    clarify --> plan_joins: допущения зафиксированы

    plan_joins --> generate_sql: валидный лёгкий план (T1 не сработал) → JoinPlan
    plan_joins --> plan_query: T1 (≥ 4 таблиц или ≥ 3 хопов)
    plan_joins --> plan_joins: план невалиден, retry не исчерпан → эскалация (cheap→mid→strong)
    plan_joins --> fail: план невалиден, retry исчерпан

    plan_query --> steps: QueryPlan валиден (инварианты — код)
    plan_query --> generate_sql: план невалиден после retry → откат к одиночному SQL

    state steps {
        [*] --> generate_step: следующий шаг DAG
        generate_step --> validate_step
        validate_step --> execute_step: ok
        validate_step --> step_diagnose: нарушения
        step_diagnose --> generate_step: retry не исчерпан → эскалация на флагман
        step_diagnose --> fail: retry исчерпан
        execute_step --> short_circuit: 0 ключей (аналог пустой itab)
        execute_step --> overflow: вернул max_keys+1 (LIMIT max_keys+1)
        execute_step --> generate_step: ключи → state, есть ещё шаги
        execute_step --> [*]: ключи получены, шагов больше нет
    }

    steps --> merge_results: шаги кончились → чанки, дедуп, агрегация
    merge_results --> interpret_result

    generate_sql --> validate_sql
    validate_sql --> diagnose_error: галлюцинация (таблицы/связи нет) — retry
    validate_sql --> degrade: вне прав пользователя — без retry
    validate_sql --> check_cost: ok
    check_cost --> execute_sql: cost в норме
    check_cost --> plan_query: T2 (EXPLAIN-cost выше порога)

    execute_sql --> diagnose_error: SQL-ошибка или пустой джойн
    execute_sql --> interpret_result: успех

    check_cost --> budget_stop: накопленная стоимость превысила кап 2 ₽

    diagnose_error --> generate_sql: retry не исчерпан → подсказка регенерации
    diagnose_error --> fail: retry_count ≥ 3

    interpret_result --> done: ответ: текст + SQL + допущения + стоимость
    degrade --> done: деградация: отказ/доступный вариант (схема скрыта)
    short_circuit --> done: «данных нет» (зависимые шаги не выполнялись)
    budget_stop --> [*]: остановка — частичный ответ (incomplete-флаг) или отказ
    overflow --> [*]: уточняющий вопрос: сузить период/сегмент
    fail --> [*]: отказ + уточняющий вопрос
    rejected --> [*]: отказ без вызова LLM

    done --> [*]
```

## Чтение схемы

- **Терминальные исходы (все — валидные `done`-состояния, не ошибки протокола):**
  `clarification` (уточняющий вопрос), `done` после `interpret_result` (полный ответ),
  `degrade` (отказ/вариант по правам), `short_circuit` («данных нет»), `overflow` и
  `fail` (уточняющий вопрос), `rejected` (identity), `budget_stop` (кап 2 ₽).
- **Контракт `budget_stop` — неполное не выдаётся за полное.** Бюджет-гuard
  проверяется до каждого LLM-вызова; в диаграмме показана плановая точка
  (`check_cost`). При срабатывании: без `interpret_result` над неполными
  агрегатами — флагман «уверенно объяснил бы» неполные цифры как полные (тот же
  класс, что R-Q2). Вариант честной остановки: пользователю — что успело
  выполниться (SQL шагов, счётчики) + формулировка «остановлено по бюджету»;
  если частичный результат всё же интерпретируется — только с явным
  `incomplete = true` в payload и в ответе (трейс: `query_trace.incomplete`).
- **Кэш** (ADR-009) на схеме не рисуется как состояние: lookup — **после `clarify`,
  до `plan_joins`** (допущения для сверки п. 6 к этому моменту уже зафиксированы;
  ревизия 02 §3.3). Hit при совпадении допущений → короткий путь: одиночный SQL →
  `validate_sql`; декомпозированный → цикл шагов (`validate_step → execute_step`)
  минуя `plan_query`/`generate_step`. Miss (или допущения не совпали) → полный
  цикл. Валидация кэшированного SQL не пропускается; `put` — оркестратор по факту
  успешного прогона (в значении — допущения `clarify`, для декомпозиции ещё
  `query_plan` + `sql_steps`).
- **Лимиты задаются кодом:** retry ≤ 3 — **общий** бюджет (в декомпозиции — на весь
  план, не на шаг: иначе 4 шага × 3 попытки = до 12 генераций; эскалации модели
  расходуют тот же бюджет — ADR-007 v1.1); кап key-set 5000;
  кап 2 ₽. Модель не переопределяет лимиты. Повторное срабатывание T2 на
  откатившемся одиночном SQL расходует общий retry-бюджет; второе T2 → `overflow`
  («сузьте период/сегмент»), а не новый круг декомпозиции (ADR-004 §4; ревизия 02 §3.4).
- **Ветка `degrade` vs `diagnose_error` — различение причин** (ADR-005): «таблица не
  существует» — галлюцинация, retry оправдан; «существует, но вне прав» — сигнал
  безопасности, retry не оправдан (модель не должна «уговорить» валидатор) — деградация:
  код фиксирует, чего не хватает, LLM формулирует отказ/вариант бизнес-понятиями.
- **Ветка декомпозиции** (ADR-004): `plan_query` → цикл `steps` — каждый шаг проходит
  ту же валидацию, что и весь SQL (AST + ACL + граф + whitelist key-колонок);
  `short-circuit` и `overflow` — честные остановки: молча обрезать нельзя.
  Откат к одиночному SQL при невалидном плане после одного retry.
- **Эскалация на retry** (ADR-007 v1.1) — единое правило генерирующих узлов:
  повторная попытка после провала идёт на уровень вверх (cheap→mid→strong).
  `diagnose_error → generate_sql` и `step_diagnose → generate_step` — после провала
  mid сильнее (strong); `plan_joins` — валидация JoinPlan кодом при парсинге
  (таблицы ∈ scope, джойны ∈ рёбрам графа, ≤ 4 таблиц, связность — как инварианты
  `plan_query`, ADR-004): невалидный план → retry с эскалацией cheap→mid→strong,
  исчерпание → `fail` (уточняющий вопрос). Эскалацию назначает код (`retry_count`,
  `route_flags`); назад — не откат, а разбор ошибки.
- **Порядок гарантирует код:** SQL не строится без JoinPlan (`plan_joins`), не
  выполняется без валидации, декомпозиция не включается без порога. Переходов «в
  обход» не существует — это свойство графа, не дисциплины промпта.

---

## Трассировка

Ревизии: v1.2 (v1.2 — валидация JoinPlan + эскалация plan_joins; v1.1 — ревизия 02: кэш, кольцо T2) · Связанные ADR: ADR-002, ADR-004, ADR-005, ADR-007, ADR-009 · Канон: hw-01/README.md §2, §3.1, §6

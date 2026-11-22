# ДЗ-05. Логика выполнения агента: state machine (LangGraph)

Веха V4. Сборка — сессия S27 (все узловые сессии вехи закрыты: S04 — каркас FSM,
S05 — resolve_access, S08 — clarify/interrupt, S10 — plan_joins/build_skeleton/
generate_sql, S11 — validate_sql, S14 — diagnose_error + execute, S15 —
compact/interpret/degrade/BudgetGuard). Полные исторические спецификации —
`.docs/` (вне гита, READ ONLY). Базовый цикл hw-plan §5 —
`clarify → plan_joins → generate_sql → validate_sql → execute_sql (+ interpret_result)` —
выполняется живым прогоном; декомпозиция (ADR-004, T1/T2) в этот ДЗ не входит.

## Схема логики

Каноническая FSM — [docs/diagrams/state-agent.md](../docs/diagrams/state-agent.md)
v1.8 — **сдаётся как есть** (hw-plan §5): 15 узлов
(`resolve_access, clarify, plan_joins, build_skeleton, plan_query, generate_sql,
generate_step_sql, validate_sql, check_cost, execute_sql, merge_results,
compact_result, diagnose_error, interpret_result, degrade`) + 9 терминальных
исходов (`done, degrade, short_circuit, overflow, fail, rejected, degraded_out,
budget_stop, clarification`) — все валидные результаты, не ошибки протокола.
Предохранители — два уровня (оба код, ADR-002): per-node счётчики ≤ 3 (политика
ретраев M1, D-016) + `recursion_limit` на весь граф; глобальные капы прогона
≤ 8 LLM-вызовов / ≤ 90 s / ≤ 2 ₽ (BudgetGuard S15 — при срабатывании частичный
ответ только с `incomplete`-флагом).

## Код графа

- [orchestration/graph.py](../src/text_to_sql_agent/orchestration/graph.py) —
  StateGraph: узлы-функции (bind), условные рёбра-гейты, checkpointer;
- [orchestration/routing.py](../src/text_to_sql_agent/orchestration/routing.py) —
  единственный источник флагов `FLAG_*`, гвард капов перед LLM-узлами, политика
  ретраев M1 на рёбрах (`retry_count`, эскалацию назначает код);
- [orchestration/runner.py](../src/text_to_sql_agent/orchestration/runner.py) —
  граница движка: `GraphRecursionError`/`interrupt` → валидные исходы в state.

Инвариант hw-01 §2.1 «маршрут — только код»: модель не возвращает имя следующего
узла — переходы определяют рёбра графа; SQL не строится без JoinPlan и скелета
(джойны пишет код в build_skeleton), не исполняется без валидации. Узлы — сервисы
(`application/services/`), сборка — `composition/container.py` (подмена заглушек
содержательными узлами через `node_overrides`).

## Ветвления

**clarify — уточняющий вопрос (`interrupt()` + checkpointer, S08).** Неоднозначный
вердикт классификатора или равные кандидаты планера (ребро `plan_joins → clarify`)
→ РОВНО ОДИН вопрос в `clarify_question` → само-ребро `clarify → clarify` →
`interrupt()`: сессия сериализуется в Valkey (TTL 10 мин), CLI ждёт ответ со stdin;
ответ резюмирует ТОТ ЖЕ прогон (тот же `thread_id`, капы персистятся, дедлайн
перевзводится) и становится допущением `user_answer`. TTL истёк / ответа нет —
`clarification`-терминал (честный отказ, не исключение).

**validate_sql — различение причин (ADR-005).** «Таблица/связь не существует» —
галлюцинация → retry через `diagnose_error` (попытка 2 — та же модель + текст
ошибки, попытка 3 — уровень вверх при достатке бюджета с резервом 0,6 ₽).
«Существует, но вне прав» — сигнал безопасности → `degrade` БЕЗ retry (модель не
должна «уговорить» валидатор). Каталог мира — `agent_data_sources`: вне каталога —
«не существует» (retry), в каталоге вне прав — «вне прав» (degrade).

**probe COUNT.** `execute_sql` вернул 0 строк → код-проба `COUNT(*)` без фильтра:
счётчик тоже 0 — «реально пусто» (валидный пустой ответ); счётчик > 0 —
подозрение на потерянный джойн → `diagnose_error` → retry. `LIMIT 1000` /
timeout 30 s / роль `agent_ro` (RLS по `app.user_id`) — исполнитель S13.

**degrade — отказ бизнес-понятиями с двухслойными гарантиями (S9, S15).** Код
фиксирует, чего не хватает (`DegradationCase`); модель mid формулирует отказ +
доступный вариант (одна попытка): в промпт не попадают чужие имена (R8), выход
санитируется детерминированно — имена недоступных таблиц и слова подтверждения
существования данных (стемы «существ/присутству/содерж/имеютс/имее/наличи/
обнаруж/встреча/найден…») → нарушение ⇒ шаблон-fallback. Пользовательский текст
не раскрывает схему; служебный канал трейса имеет право на детали.

**Инструмент/память** (ДЗ-02…04) — уже подключены и демонстрируются в прогоне:
retrieval-нога plan_joins (pgvector гибрид dense+FTS, фильтр прав в обеих ногах),
граф связей Neo4j (`find_join_paths`, `graph_version` пинится на прогон), тул
`run_sql` под `agent_ro`, стриминг объяснения (CLI retrieval-контур `--fsm` не
использует; объяснение FSM — буферное `interpret_result`).

## Пример выполнения

Воспроизводимость (порядок обязателен — на холодной БД без сидов/синка plan_joins
не найдёт рёбер и уйдёт в no-path):

```bash
make infra-up && make infra-migrate && make sync-graph && make index-knowledge
TOKEN_U_LOGIST=<из AGENT_DEV_TOKENS в .env — формат user_id:token>
uv run --env-file .env python -m apps.cli "Сколько заказов у клиентов из РФ?" \
    --fsm --token "$TOKEN_U_LOGIST"       # > stdout-ответ, 2> stderr-трейс
```

Шлюз — локальный LM Studio (`qwen3.8-9b`, D-020), стоимость 0,00 ₽ (цены локальной
модели нулевые). `stdout` — ответ; `stderr` — трейс этапов (ConsoleTracer):
вопрос → узлы → вердикты (plan/SQL/validate/interpret с payload) → вызовы модели
(токены/латентность/₽). graph_version монотонен (правки реестра растят счётчик) —
в трейсах ниже 223–225, важен факт пина, не значение.

### Сцена 1 — happy-path: вопрос → допущения → план → SQL → валидация → ответ

`hw-05/fsm-happy-stdout.txt` / `fsm-happy-stderr.txt` — «Сколько заказов у клиентов
из РФ?», пользователь u_logist. Полный маршрут 10 узлов → `done`; SQL собран кодом
по скелету (`INNER JOIN customers ON orders.customer_id = customers.customer_id`),
ловушка кодов обойдена (`country = 'RU'`, не `'Russia'` — подсказка справочника в
SYSTEM_PROMPT, S24), допущение из `usage_notes` ребра раскрыто в ответе:

```text
Маршрут: resolve_access -> clarify -> plan_joins -> build_skeleton -> generate_sql
         -> validate_sql -> check_cost -> execute_sql -> compact_result -> interpret_result
Исход: done
Ответ: У клиентов из РФ оформлено 100 заказов.
SQL: SELECT COUNT(*) AS orders_count FROM orders INNER JOIN customers ON
     orders.customer_id = customers.customer_id WHERE (customers.country = 'RU') LIMIT 1000
Допущение: customers.created_at — критерий «новый клиент» (по created_at)
Стоимость: 0.0000 руб.
```

Трейс (`fsm-happy-stderr.txt`, выдержка):

```text
▶ Вопрос: Сколько заказов у клиентов из РФ?
  → узел resolve_access
  → узел clarify
  · qwen3.8-9b · 1031→63 ток · 6.0с · 0.00₽
  · clarify: исход unambiguous, допущений 1
  → узел plan_joins
  · plan_joins: исход plan, gv 223, кандидаты: orders, customers
  → узел generate_sql
  · generate_sql: исход sql, таблиц 2, вызовов 1, эскалация нет
  → узел validate_sql
  · validate_sql: исход ok, замечаний 0, LIMIT 1000
  ...
  · interpret: answer, строк 1, 0.00₽
```

### Сцена 2 — clarify-interrupt: пауза, ответ пользователя, тот же прогон

`hw-05/fsm-clarify-resume-stdout.txt` / `fsm-clarify-resume-stderr.txt` —
«клиенты, пришедшие из рекламы», u_logist, ответ со stdin («только заказы с
привязкой к кампании»). Равные кандидаты атрибуции → interrupt (живой
Valkey-checkpointer) → ответ резюмирует тот же прогон (`clarify` ×2, допущение
`user_answer`) → `done`: «Всего 20 клиентов, пришедших из рекламы (first-touch…)».
Дополнительно в этом же трейсе — ветвление validate: первая генерация
`hallucination` → `diagnose` (класс semantics) → регенерация по M1 → `ok`.

Без ответа (`stdin` закрыт) — честный `clarification`-исход:
`hw-05/fsm-clarify-eof-stdout.txt` («ответ на уточняющий вопрос не получен —
сессия истечёт по TTL»).

### Сцена 3 — retry-самокоррекция: галлюцинация → diagnose → регенерация

`hw-05/fsm-retry-stdout.txt` / `fsm-retry-stderr.txt` — «Сколько заказов у
клиентов из Бурундии?» (страны нет в сиде), u_logist: первая генерация отклонена
валидатором (`hallucination`) → `diagnose` → попытка 2 (та же модель + текст
ошибки, D-016) → `ok` → честный ответ «0 шт.»:

```text
Маршрут: resolve_access -> clarify -> plan_joins -> build_skeleton -> generate_sql
         -> validate_sql -> diagnose_error -> generate_sql -> validate_sql
         -> check_cost -> execute_sql -> compact_result -> interpret_result
Ответ: Заказов у клиентов из Бурундии нет: 0 шт.
```

### Сцена 4 — deny: «премии» без прав HR (clarify-мир ADR-005)

`hw-05/fsm-deny-bonuses-stdout.txt` / `fsm-deny-bonuses-stderr.txt` — «Какие
премии получили сотрудники в октябре?», пользователь **u_analyst** (права только
orders/order_items/products). Права-фильтрованный retrieval (ADR-005 «недоступное
не существует») — employees/bonuses не существуют в мире агента, чужих имён ни
промпт, ни вопрос классификатора не содержат: агент честно спрашивает «о каких
выплатах речь» → interrupt → EOF → `clarification`:

```text
Уточнение: Какие именно премии вас интересуют: все выплаты, называемые «премиями»,
           или только премии за результативность (KPI)?
Исход: clarification
```

Проверка S9 по пользовательскому тексту: `grep -icE "employees|bonuses"
hw-05/fsm-deny-bonuses-stdout.txt` → 0.

Это канонный исход deny-мира: права-ограниченный пользователь получает уточняющий
вопрос, а не отказ — данные, о которых он спрашивает, для него не существуют
(детиагностика — S15, живой опыт; решение зафиксировано ruling'ом ARCH-GATE S27:
в артефакт идут ОБЕ сцены — clarify-мир здесь и degrade ниже).

### Сцена 5 — degrade: полный цикл ретраев → честный отказ

`hw-05/fsm-degrade-validate-retries-stdout.txt` / `-stderr.txt` — «Сколько
клиентов имеет более одной рекламной кампании?», u_logist: прямого ребра
customers×campaigns в реестре нет — планер собирает то, что есть в графе
(campaigns↔orders), модель дважды галлюцинирует колонку (`validate_sql:
hallucination` ×2 → `diagnose` ×2 → попытка 3 — **эскалация strong**), DB
(второй рубеж) отвергает неисчезнувшую галлюцинацию (`column orders.client_id
does not exist` — ACL `'*'` не ловит несуществующую колонку), `diagnose` (класс
syntax) → fuse генератора исчерпан → `degrade` с формулировкой модели mid:
отказ без деталей инфраструктуры, только свои темы u_logist (campaigns, customers,
orders, order_items, products — «своё упоминать можно», ADR-005):

```text
Маршрут: ... -> execute_sql -> diagnose_error -> degrade
Исход: degrade
Ответ: К сожалению, я не могу выполнить ваш запрос в данный момент... Я работаю
       с данными о рекламных кампаниях (campaigns), клиентах (customers), заказах
       (orders) ... Пожалуйста, попробуйте задать вопрос позже.
```

`hw-05/fsm-degrade-execute-retries-stdout.txt` / `-stderr.txt` — второй контур
ретраев (S14): execute → diagnose → регенерация ×3 → degrade. Деградация —
валидный исход, не сбой: недоступное не выполняется, неполное не выдаётся за
полное.

## Ограничения стенда (не баги)

1. **check_cost — транзитный узел-заглушка**: пороги T2 (hw-01 §3.4) не активны
   до S12/feat-012 (веха V5/выпускная) — узел всегда пропускает к execute_sql.
2. **plan_query / generate_step_sql — заглушки**: декомпозиция T1 (ADR-004) —
   выпускная feat-017 (план Б hw-plan §5); ветка T1 в графе заложена, не активна.
3. **LM Studio — одна модель на все роли** (D-020): эскалация «уровень вверх» в
   трейсе НЕ меняет модель (qwen3.8-9b играет cheap/mid/strong) — лимит стенда,
   экономика с ненулевыми ценами — облако routerai.ru (ADR-007).
4. **Планер недетерминирован** (temp 0, но retrieval/ранжирование на локальной
   модели варьируются): формулировки «клиенты × кампании» сегодня собирают
   campaigns↔orders, а не no-path — сцена 5 снята как наблюдаемая (правило
   воспроизводимости: фиксируем факт, не ожидание).

## Как воспроизвести

```bash
make infra-up && make infra-migrate && make sync-graph && make index-knowledge
uv run pytest src/tests/unit/test_interpret.py -q          # 66: санитайзер/шаблоны S9
uv run pytest src/tests/unit/test_graph.py -q              # каркас FSM, interrupt, гейты
uv run pytest src/tests/unit/test_valkey_checkpointer.py -q # allowlist serde чекпойнтера
uv run --env-file .env python -m apps.cli "<вопрос>" --fsm --token "$TOKEN"   # сцены выше
```

Полный верификационный контур — `make check` (uv sync + ruff + pytest, 898+).
Правки интеграционной сборки этой сессии: ConsoleTracer рендерит FSM-события,
`agent.start` в трейсе FSM-прогона, allowlist msgpack-типов чекпойнтера (устранён
deprecation-warning LangGraph на resume), стемы санитайзера BL-020 — каждая с
регрессионными тестами (коммиты 1a61840, 6cd7d9f, d3f22ca).

# ДЗ-04. Гибридная память: граф связей (Neo4j) + усиление retrieval

Веха V3. Сборка README — за финальной закрывающей сессией вехи (S09/S26 —
смотреть статусы [индекса сессий](../solution/sessions.md)); этот раздел
написан сессиями S07 (feat-007 — графовая половина памяти, «Связи между
объектами») и S09 (feat-009 — гибридный поиск, «Гибридный поиск» и
«Пример запроса/ответа»). Полные исторические спецификации прошлых ДЗ — `.docs/` (вне гита,
READ ONLY); полная спецификация ДЗ-04 появится там же к сдаче.

## Связи между объектами

Графовая память (feat-007, сессия S07) построена по ADR-003 v2.1 / ADR-006 v1.3:
**источник рёбер — только реестр** `agent_relationships` (владелец данных
вносит связи со семантикой; FK каталога — генератор кандидатов в очередь
владельцу, НЕ ребро), Neo4j хранит **производный снапшот топологии** —
пары таблиц + rel_id; семантика читается из PostgreSQL по rel_id (в граф не
дублируется — иначе дубль дрейфует молча). Синк — по счётчику
`settings_version('relationships')`, ревизия снапшота `graph_version`
пинится на прогон (state, ключ кэша, аудит); reconciliation-тест «активные
строки реестра = рёбра графа» ловит дрейф в любую сторону (ADR-006 §4.3).

```bash
make infra-up && make infra-migrate && make sync-graph   # снапшот реестра → Neo4j
```

Вывод синка (2026-11-21, живой прогон):

```text
Граф синхронизирован: 7 вершин, 5 рёбер, graph_version=13
```

(7 вершин — все бизнес-таблицы демо-схемы, 5 активных рёбер реестра +
маркер ревизии; повторный запуск идемпотентен. Счётчик монотонен —
значение зависит от истории правок реестра, важно только, что правка
настроек его растит, а синк подхватывает.)

Поиск пути — детерминированный код по снапшоту (`find_join_paths`, порт
`SchemaGraphStore`; LLM пути не строит — ранжирование кандидатов моделью
приходит в узле plan_joins, S09): k взвешенных путей по активным рёбрам,
порядок — убывание суммарного веса реестра, фильтр прав — deny-by-default
(`allowed_tables` из state.permissions: путь через недоступную таблицу не
существует в мире агента).

Семантика рёбер пути для демо-пары ДЗ-04 «клиенты, пришедшие из рекламы» —
вывод `path_semantics` (2026-11-21, живой прогон, пользователь u_logist):

```text
Путь: customers -> orders -> campaigns (weight=2.2)
  ребро e_customers_orders: join_type=inner, meaning='клиент разместил заказ'
    intent_tags=['buyer', 'client']
    usage_notes: customers.created_at — критерий «новый клиент» (по created_at)
  ребро e_campaigns_orders: join_type=inner, meaning='заказ атрибутирован кампании'
    intent_tags=['attribution', 'ads']
    usage_notes: «пришли из рекламы» = first-touch: первый заказ клиента из
      ads-кампании; any-touch завышает цифру; campaign_id надёжен с 2026-03;
      NULL campaign_id — заказы вне кампаний, INNER JOIN их молча теряет
      (ADR-006 §4.7)
```

Допущение атрибуции (first-touch) живёт в `usage_notes` ребра реестра
(миграция 008) и раскрывается в ответе агента как допущение — это ловушка
«FK есть, смысл другой»: технически верный INNER JOIN по `campaign_id`
молча считает any-touch и завышает цифру (ADR-006 §1, разрыв №1).

Инварианты, проверенные тестами ([test_join_graph.py](../src/tests/integration/test_join_graph.py) —
12 integration + юнит-тесты ранжирования/валидации
[test_join_paths_ranking.py](../src/tests/unit/test_join_paths_ranking.py) и
skip-гвард [test_join_graph_skip_guard.py](../src/tests/unit/test_join_graph_skip_guard.py)):

- путь по реальным рёбрам или `[]` — джойн не выдумывается (пары без связи,
  таблицы вне прав пользователя — пути нет);
- семантика каждого ребра пути — из настроек PG; ребро вне active-вьюхи —
  честная ошибка дрейфа (fail-closed), не пустая семантика;
- reconciliation: активные строки реестра = рёбра графа; deprecated-ребро
  в графе отсутствует; правка настроек → счётчик растёт → повторный синк
  меняет граф (`graph_version` = счётчику);
- фильтр прав deny-by-default: u_analyst (без прав на campaigns) путь
  orders→campaigns не получает; пустой scope — пустой результат.

## Гибридный поиск: retrieval + граф → JoinPlan (plan_joins)

Узел plan_joins (feat-009, сессия S09) — флагман ДЗ-04: соединяет обе половины
памяти (hw-plan §4). Нога 1 — semantic retrieval через порт `VectorStore`
(dense; гибрид FTS/RRF из S26 подменяется реализацией под портом, интерфейс
не меняется): находит релевантные таблицы и рёбра (RELATIONSHIP-документ,
`source_ref = rel:{edge_id}` — подтверждение релевантности ребра вопросу).
Нога 2 — граф: код перебирает пары кандидатов (∩ права, deny-by-default) и
строит k взвешенных путей по снапшоту (`find_join_paths`). Модель (qwen, cheap
— матрица ADR-007) только **ранжирует готовых кандидатов** через тул (ADR-008,
structured output); маршрут, сборку плана и валидацию ведёт код — инвариант
hw-01 §2.1 «LLM наполняет, код решает»: выдуманный джойн (edge_id вне снапшота)
отсекается валидатором `domain/join_plan.py` (тест-инвариант).

Ретраи M1 (D-016): попытка 2 — та же роль + текст ошибки; попытка 3 — роль
вверх при остатке бюджета с резервом 0.6 ₽ (ADR-007 §4.3, ruling Q7 — шов
к резидентному аккумулятору адаптера, BudgetGuard — S15/V4); исчерпание —
честный отказ с уточняющим вопросом, НЕ выдуманный план. Равные кандидаты →
исход «уточнение» (ADR-007: не эскалация; interrupt — V4/S08). Чистое «нет
активного пути» → degrade + заявка владельцу в `agent_edge_proposals`
(BL-003 п.2, D-030: INSERT-only под `agent_planner`, миграция 0009 — заявка
пишется кодом узла, не LLM).

## Пример запроса/ответа

Демо-вопрос вехи (layer-3 живой прогон, 2026-11-21):

```bash
make sync-graph && make index-knowledge
uv run --env-file .env python -m apps.plan_joins_demo "клиенты, пришедшие из рекламы"
```

Вывод (пользователь u_logist — logistics_viewer, миграция 0003):

```text
Вопрос: «клиенты, пришедшие из рекламы» (пользователь u_logist)
Retrieval: кандидаты-таблицы ['campaigns', 'orders', 'customers', 'order_items', 'products']; рёбра с релевантностью: e_campaigns_orders, e_customers_orders
Исход: PLAN (graph_version=41)
Путь: campaigns ↔ orders ↔ customers
  джойн 1: campaigns(campaign_id) = orders(campaign_id) [e_campaigns_orders, inner]
  джойн 2: orders(customer_id) = customers(customer_id) [e_customers_orders, inner]
Допущения (usage_notes рёбер):
  - «пришли из рекламы» = first-touch: первый заказ клиента из ads-кампании; any-touch завышает цифру; campaign_id надёжен с 2026-03; NULL campaign_id — заказы вне кампаний, INNER JOIN их молча теряет (ADR-006 §4.7)
  - customers.created_at — критерий «новый клиент» (по created_at)
```

То, чего нет у готовых Text-to-SQL: план несёт **допущение атрибуции**
(first-touch из `usage_notes` ребра реестра) — оно войдёт в ответ
пользователю (раскрытие — interpret_result, feat-015), а `mandatory_predicates`
рёбер — skeleton-якоря для build_skeleton (S10). graph_version плана = пину
снапшота (Ловушка-3: сверка версий снапшота графовой ноги и плана).

Инварианты, проверенные тестами ([test_plan_joins.py](../src/tests/unit/test_plan_joins.py)
— 26 unit на фейках портов, [test_plan_joins_db.py](../src/tests/integration/test_plan_joins_db.py)
— 4 integration по грантам заявок):

- JoinPlan ≤ 4 таблиц по rel_id, связность, тип/направление/ключи джойна —
  из ребра реестра (направление обхода может разворачивать left/right —
  ключи всегда следуют своей таблице);
- подсунутый выдуманный джойн отсекается валидатором (edge_id вне активного
  снапшота); таблицы вне прав и ключи джойна вне ACL колонок — «ребро
  недоступно целиком» (deny-by-default, S00 контракт 3);
- ретраи M1: попытка 2 — та же модель + текст ошибки, попытка 3 — роль вверх
  при остатке бюджета с резервом 0.6 ₽, иначе честный отказ; исчерпание —
  fail с уточняющим вопросом;
- «нет активного пути» → заявка в agent_edge_proposals под agent_planner
  (INSERT-only); retrieval/синк-сбой → fail БЕЗ заявки (заявка — только от
  факта отсутствия пути по живому снапшоту);
- грантовая дисциплина: planner не может UPDATE/SELECT/DELETE очередь;
  agent_ro (канал исполнения) не может INSERT — заявка не пользовательский SQL.

## Как воспроизвести

```bash
make sync-graph                                             # снапшот → Neo4j
uv run --env-file .env python -c "..."                      # find_join_paths + path_semantics
uv run pytest src/tests/integration/test_join_graph.py -q   # верификация feat-007
make index-knowledge                                        # корпус знаний (S06)
uv run --env-file .env python -m apps.plan_joins_demo "клиенты, пришедшие из рекламы"
uv run pytest src/tests/unit/test_plan_joins.py -q          # верификация feat-009 (фича)
```

Вывод `path_semantics` снимается скриптом из раздела выше (контекст
RunContext + карта прав демо-пользователя u_logist — миграция 0003).

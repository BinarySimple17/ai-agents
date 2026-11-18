# C4 Level 3 — Component (внутреннее устройство агента)

- **Статус:** каноническая схема v1.5 (2026-11-18 — пост-приёмочный патч по отзыву преподавателя: Reason = краткий план следующего действия, трейс-минимум (решение + инструмент + результат). v1.4 — синхронизация с hw-01 §2.1/§3.1 (build_skeleton/compact_result/probe, k путей, M1); v1.3 — валидация JoinPlan; v1.2 — Neo4j; v1.1 — ревизия 02)
- **Требования prd.md:** FR-1…FR-11 (цикл, guardrails, RBAC, каскад), FR-12 (декомпозиция)
- **Связанные:** ADR-002 (карта «кто что решает», recursion_limit), ADR-004 (цикл шагов, T2-исходы), ADR-005 (рубежи 1–5), ADR-006 (реестр-SoT, `edge_id`), ADR-007 (модель на узел, политика M1), ADR-008 (структурированные выходы); state-agent.md (та же машина в state-нотации), sequence-query.md (сквозной путь тяжёлого запроса)

```mermaid
graph TB
    U["Вопрос пользователя<br/>identity из проверенного токена"]

    subgraph AG ["Агент: state machine на коде — модель заполняет узлы, код ведёт маршрут (15 узлов = hw-01 §2.1)"]
        RA["resolve_access — код<br/>user_id → роли → allowed_tables, allowed_columns, scope<br/>(кэш per-user, проверка версии прав)"]

        CL["clarify — cheap (qwen)<br/>Reason: однозначен ли вопрос?<br/>→ один уточняющий вопрос (interrupt() + checkpointer, TTL 10 мин)<br/>ИЛИ явные допущения"]

        PJ["plan_joins — код строит k взвешенных путей (find_join_paths),<br/>cheap (qwen) ранжирует топ-5; при равных — clarify<br/>Act: retrieval (pgvector, фильтр scope) + граф<br/>→ JoinPlan: edge_id рёбер<br/>валидация плана — код при парсинге (scope, рёбра графа, ≤ 4 таблиц)<br/>retry по M1: попытка 2 — та же модель + ошибка, попытка 3 — уровень вверх"]

        SK["build_skeleton — код, без LLM<br/>JOIN-скелет из рёбер плана: ключи, тип, mandatory_predicates<br/>→ skeleton_hash (модель скелет не трогает)"]

        PQ["plan_query — mid (deepseek, reasoning)<br/>JoinPlan + пробный SQL → QueryPlan DAG ≤ 4 шагов<br/>(инварианты проверяет код при парсинге — ADR-008)"]

        GS["generate_sql — mid (deepseek)<br/>наполнение скелета: SELECT / WHERE / GROUP BY / ORDER BY<br/>join_type — из плана, модель не решает"]

        V["validate_sql — код (главный рубеж, ADR-005)<br/>полный обход AST (одна инструкция, запрет SET/COPY/CALL/DO,<br/>обход CTE/UNION/подзапросов/LATERAL, LIMIT к внешнему, диалект postgres)<br/>+ ACL вкл. колонки джойна + deny-list схем<br/>+ skeleton_hash + mandatory_predicates + джойны ⊆ edge_id<br/>галлюцинация → diagnose · вне прав → degrade · ok → дальше"]

        CC["check_cost — код<br/>EXPLAIN (без ANALYZE) → порог T2; бюджет/дедлайн"]

        EX["execute_sql — код<br/>read-only agent_ro · LIMIT 1000 · timeout 30s · RLS + SET LOCAL app.user_id<br/>0 строк → probe COUNT (код): пусто и там — валидный пустой ответ,<br/>подозрение на джойн → diagnose"]

        STEPLOOP["Цикл шагов декомпозиции (ADR-004):<br/>generate_step_sql — mid (deepseek), retry по M1<br/>→ validate_sql над шагом — код (AST + ACL + скелет + whitelist key-колонок)<br/>→ execute_sql над шагом — код (bind ANY(:keys), чанки 1000,<br/>промежуточный key-set без LIMIT, кап кардинальности 50 000,<br/>short-circuit пустого key-set)"]

        MG["merge_results — код<br/>слияние шагов: чанки, дедуп; LLM никогда не сливает результаты"]

        CR["compact_result — код<br/>row_count + ≤ 20 строк + агрегаты + флаги усечения/approximate<br/>→ компактный вид для LLM (вход interpret_result в ~1150 токенов)"]

        DG["diagnose_error — cheap (qwen)<br/>разбор ошибки/пустоты → подсказка регенерации<br/>(контекст шага или всего SQL; в контекст при retry — только последний SQL + ошибка)"]

        IR["interpret_result — strong (glm)<br/>объяснение бизнесу + допущения из usage_notes<br/>вход — компакция, не сырые строки"]
    end

    MEM["Векторная память (pgvector)<br/>DDL · глоссарий · few-shot · связи с семантикой<br/>namespace'ы по ролям (ADR-009)"]
    GRAPH["Граф связей (Neo4j) — производный снапшот реестра (ADR-006 §4.3)<br/>топология active-рёбер (edge_id); graph_version пинится на прогон;<br/>семантика ребра — из PG-строк; хранилище заменяемо за find_join_paths() (D-015)"]
    PERM["Настройки прав<br/>agent_permissions · agent_user_roles"]
    CACHE["Кэш Valkey<br/>ключ: rights_hash + graph_version + assumptions → SQL<br/>(декомпозированный: query_plan + sql_steps)"]
    BUDGET["Бюджет-контур — код<br/>per-node ≤ 3 (M1) + recursion_limit · глобальные капы ≤ 8 вызовов / ≤ 90 s / ≤ 2 ₽ (резерв 0,6 ₽)<br/>backoff 1→2→4с · breaker 5→60с → деградация, не пустая ошибка"]
    LLMR["LLM-шлюз: роли cheap / mid / strong (ADR-007)"]

    U --> RA
    RA --> PERM
    RA --> CL
    CACHE -.->|"hit + допущения совпали → SQL к валидации (кэш не обходит гейт, S7)"| V
    CL -->|"уточнение"| OUTQ["Валидный результат:<br/>уточняющий вопрос"]
    CL -->|"допущения"| PJ
    CL -.->|"допущения зафиксированы → lookup"| CACHE
    PJ --> MEM
    PJ --> GRAPH
    PJ -->|"нет активного пути"| DENY2["degrade + заявка владельцу (agent_edge_proposals)"]
    PJ --> SK
    SK -->|"T1: ≥ 4 таблиц / ≥ 3 хопов — основной путь на боевом масштабе"| PQ
    SK -->|"T1 не сработал"| GS
    GS --> V
    PQ --> STEPLOOP
    V -->|"ok"| CC
    V -->|"галлюцинация"| DG
    V -->|"вне прав"| DENY["degrade: код фиксирует чего не хватает,<br/>LLM формулирует отказ/вариант бизнес-понятиями<br/>без retry, схема скрыта, существование данных не подтверждается"]
    CC -->|"T2: тяжёлый одиночный план"| HEAVY["async / matview / approximate /<br/>отказ с рекомендацией «сузьте период/сегмент»"]
    CC -->|"в норме"| EX
    EX -->|"ошибка"| DG
    EX -->|"успех"| CR
    STEPLOOP -->|"0 ключей"| SHORT["short-circuit: честное «данных нет»<br/>зависимые шаги не выполняются"]
    STEPLOOP -->|"шаги кончились"| MG
    MG --> CR
    CR --> IR
    DG -->|"retry по M1"| GS
    DG -.->|"шаг"| STEPLOOP
    IR --> OUT["Ответ: текст + SQL + meaning рёбер + допущения + флаги + стоимость"]
    DENY --> OUT
    DENY2 --> OUT
    SHORT --> OUT
    HEAVY --> OUT
    BUDGET -.->|"лимиты"| AG
    CL & PJ & PQ & GS & DG & IR -.-> LLMR
    GS & IR & MG -.-> MEM
```

## Чтение схемы

- **Состав узлов — 15, идентичен state-agent.md и hw-01 §2.1:** `resolve_access`,
  `clarify`, `plan_joins`, `build_skeleton`, `plan_query`, `generate_sql`,
  `generate_step_sql`, `validate_sql`, `check_cost`, `execute_sql`,
  `merge_results`, `compact_result`, `diagnose_error`, `interpret_result`,
  `degrade`; probe `COUNT` — ребро `execute_sql`, T2-ветка `degraded_out`.
- **ReAct-семантика внутри узлов:** каждый содержательный узел несёт Reason
  (краткий план следующего действия — структурированное обоснование, ~100 дешёвых
  токенов против повторного цикла при ошибочном вызове; не свободная вербализация —
  в трейсе достаточно решения, вызванного инструмента и результата) и Observe
  (результат инструмента в state). Чисто кодовые узлы
  (`resolve_access`, `build_skeleton`, `validate_sql`, `check_cost`, `execute_sql`,
  `merge_results`, `compact_result`, `degrade` и валидация/исполнение шага) — без
  LLM: граница прав и корректность не доверяются модели (ADR-002: детерминизм —
  безопасность, агентность — семантика).
- **Path-поиск — k взвешенных путей, не shortestPath-правило:** реестр — мультиграф
  (несколько активных рёбер между парой таблиц с разной семантикой), поэтому код
  строит k взвешенных путей с фильтрацией по свойствам рёбер (`intent_tags`,
  `temporal`), модель ранжирует топ-5, при равных кандидатах — `clarify` (A6).
  `shortestPath` — имя функции Neo4j, не решающее правило планировщика.
- **Инструменты агента** (вызываемые узлами, с ограничениями): `search_schema` /
  `find_join_paths` (только активные рёбра реестра), `describe_table` (только
  таблицы из прав), `run_sql` (read-only, LIMIT 1000, timeout 30s), `explain_sql`
  (только `EXPLAIN`; `EXPLAIN ANALYZE` запрещён — он выполняет запрос). Инструменты —
  порты приложения; LLM не выбирает их сама — маршрут зашит в граф.
- **Кэш:** lookup — **после `clarify`, до `plan_joins`** (ADR-009 п. 6, ревизия 02
  §3.3): сверять допущения можно только когда они уже зафиксированы. Ключ —
  `rights_hash + graph_version + assumptions`, кэшируется SQL, не результат; hit не
  обходит гейт: SQL проходит `validate_sql` + ACL против текущих прав + `EXPLAIN`
  (S7). Hit при совпадении допущений даёт короткий путь: одиночный SQL →
  `validate_sql` минуя генерацию; декомпозированный → цикл `validate → execute`
  минуя `plan_query`/`generate_step_sql` (реплей `query_plan` + `sql_steps` из
  значения). `put` — оркестратор по факту успешного прогона.
- **Рубежи прав на этой схеме (ADR-005 v1.2):** (1) фильтрация мира —
  `plan_joins`/retrieval/DDL только по scope; (2) `validate_sql` — главный рубеж
  (полный обход AST, deny-list команд/функций, скелет, `mandatory_predicates`);
  (3) deny-list схем (`pg_catalog`, `information_schema`, служебные схемы графа,
  `agent_*`); (4) ACL колонок соединения — ключи джойна ∈ правам, иначе ребро
  недоступно целиком; (5) `execute_sql` — БД (agent_ro, RLS + `SET LOCAL
  app.user_id`, раздельные коннекты планировщик/исполнитель). Различение исходов:
  не существует → `diagnose_error` (retry); вне прав → `degrade` без retry (ADR-005).
- **Два динамических механизма роутинга** без классификатора сложности: триггеры
  T1/T2 (детерминированные пороги, код) и политика ретраев M1 (D-016, ADR-007 §4.1):
  попытка 1 — базовая модель узла; попытка 2 — та же модель + текст ошибки; попытка
  3 — уровень вверх при достатке бюджета (проверка с резервом 0,6 ₽). Эскалацию
  назначает код (`retry_count`); в контекст при retry — только последний SQL/план +
  текст ошибки.
- **Бюджет-контур — сквозной:** два уровня предохранителей (per-node ≤ 3 +
  `recursion_limit` — ADR-002) и глобальные капы (≤ 8 вызовов / ≤ 90 s / ≤ 2 ₽);
  guard проверяется до каждого LLM-вызова и перед каждой эскалацией (плановая точка
  — `check_cost` → `budget_stop`, state-agent.md). Контракт остановки: неполное не
  выдаётся за полное — частичный результат только с `incomplete`-флагом, иначе
  отказ. Backoff/breaker на 429 — всё в коде; breaker завершается деградацией
  (валидный SQL без объяснения / отложенная задача), не пустой ошибкой.
- **Трейс:** каждый узел и переход — спан; в `query_trace` ложатся вопрос, допущения,
  план, `edge_id`, `graph_version`, SQL шагов, retry_count, latency, стоимость,
  cache hit — основа eval-сета (FR-11); аудит append-only (S8) — отдельно от трейсов.

---

## Трассировка

Ревизии: v1.5 (Reason = краткий план, трейс-минимум) · Связанные ADR: ADR-002, ADR-004, ADR-005, ADR-006, ADR-007, ADR-008, ADR-009 · Канон: hw-01/README.md §2–3

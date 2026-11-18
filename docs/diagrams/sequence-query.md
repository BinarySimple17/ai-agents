# Sequence Diagram — сквозной путь тяжёлого запроса (с декомпозицией)

- **Статус:** каноническая схема v1.4 (2026-11-18 — пост-приёмочный патч по отзыву (D-019): T2-исходы уточнены (approximate/matview — через interpret_result, async/отказ — терминал), ретрай шага явен. v1.3 — k путей, M1, probe, compact_result, флаги; v1.2 — Neo4j; v1.1 — ревизия 02)
- **Сценарий:** «Топ-10 категорий продуктов по выручке среди клиентов, пришедших из ads-кампаний за Q4 2026, и их средний чек» — джойн 5 таблиц, срабатывает T1
- **Требования prd.md:** FR-1…FR-8, FR-12
- **Связанные:** state-agent.md (та же FSM в state-нотации), c4-l3-agent.md (компоненты), ADR-002, ADR-004, ADR-005, ADR-006, ADR-007, ADR-009

```mermaid
sequenceDiagram
    autonumber
    participant U as Пользователь (CLI/Streamlit)
    participant RA as resolve_access (код)
    participant CL as clarify (qwen — cheap)
    participant OR as Оркестратор (граф, код)
    participant C as Кэш Valkey
    participant PJ as plan_joins (qwen — cheap)
    participant SK as build_skeleton (код)
    participant PQ as plan_query (deepseek — mid, reasoning)
    participant GS as Шаг: generate_step_sql<br/>(deepseek; retry по M1)
    participant V as validate_sql (код): AST + ACL + скелет + граф
    participant EX as execute_sql (код): agent_ro
    participant MG as merge_results (код)
    participant CR as compact_result (код)
    participant IR as interpret_result (glm — strong)
    participant DB as PostgreSQL<br/>(данные, pgvector, настройки)
    participant NEO as Neo4j<br/>(граф связей)

    U->>RA: вопрос + identity (проверенный токен)
    RA->>DB: agent_user_roles, agent_permissions<br/>(per-user кэш · версия — счётчик settings_version)
    DB-->>RA: роли → scope + graph_version снапшота (в state)
    RA->>CL: вопрос + права в state
    CL->>CL: Reason: однозначен (метрика, период)<br/>допущения зафиксированы

    alt вопрос неоднозначен
        CL-->>U: один уточняющий вопрос<br/>(interrupt() + checkpointer, TTL 10 мин) → done
    end

    OR->>C: lookup (rights_hash + graph_version + assumptions) — после clarify
    alt hit и допущения совпали
        C-->>OR: SQL + допущения (+ query_plan, sql_steps)
        OR->>V: короткий путь: одиночный SQL → validate_sql<br/>(декомпозированный — цикл validate → execute<br/>минуя plan_query/generate_step_sql)
        Note over OR,V: хит → валидация не пропускается (S7) → исполнение → ответ → done<br/>(сценарий ниже — miss)
    else miss (или допущения не совпали → miss)
        C-->>OR: miss
    end

    OR->>PJ: вопрос + допущения + scope
    PJ->>DB: retrieval: DDL, глоссарий, usage_notes (фильтр scope)
    PJ->>NEO: find_join_paths: k взвешенных путей по снапшоту реестра<br/>(active-рёбра, фильтр intent_tags/temporal; graph_version — ADR-006 §4.3)
    PJ->>PJ: Reason: ранжирование топ-5 — ребро<br/>«заказ атрибутирован кампании»; JoinPlan: 5 таблиц<br/>→ T1 срабатывает (код, ≥ 4 таблиц)
    PJ->>SK: JoinPlan (edge_id рёбер)
    SK->>SK: JOIN-скелет от кода: ключи, тип,<br/>mandatory_predicates → skeleton_hash
    SK->>PQ: скелет (T1 — декомпозиция)
    PQ->>PQ: Reason: как разбить на простые шаги<br/>(retry по M1 при невалидном плане)
    PQ-->>V: QueryPlan s1→s2→s3 (инварианты — код:<br/>≤ 4 шагов, ≤ 3 таблиц, топология, whitelist key-колонок)

    loop каждый шаг s1..s3
        PQ->>GS: purpose + шаблон + статистика key-set
        GS->>GS: наполнение шага скелетом (mid ·<br/>попытка 2 — та же модель + текст ошибки; попытка 3 — эскалация)
        GS->>V: SQL шага
        V->>V: полный обход AST + ACL (вкл. колонки джойна)<br/>+ skeleton_hash + mandatory_predicates<br/>+ джойны ⊆ edge_id + ANY(:keys) на whitelist-колонке

        alt нарушения — галлюцинация (таблицы/колонки нет)
            V->>GS: diagnose шага → retry по M1<br/>(≤ 3 на узел; исчерпание → fail)
        end
        alt таблица существует, но вне прав
            V-->>U: degrade: отказ/вариант без retry → done
        end

        V->>EX: ok
        EX->>DB: SELECT ... WHERE x = ANY(:keys)<br/>чанки по 1000 · промежуточный key-set без LIMIT<br/>кап кардинальности 50 000 · REPEATABLE READ

        alt 0 строк
            EX->>EX: probe COUNT (код): пусто и с ослабленным фильтром<br/>→ валидный пустой ответ; подозрение на джойн → diagnose
        end
        alt пустой key-set
            EX-->>U: short-circuit: «данных нет» → done
        end
        alt кап 50 000 превышен
            EX-->>U: overflow → async / отказ / флаг approximate → done
        end

        EX->>PQ: ключи → state (следующий шаг)
    end

    PQ->>MG: шаги кончились
    MG->>MG: слияние шагов: чанки, дедуп (только код)
    MG->>CR: итоговый набор
    CR->>CR: row_count + ≤ 20 строк + агрегаты<br/>+ флаги усечения/approximate
    CR->>IR: компактный вид (~1150 токенов), не сырые строки
    IR->>IR: Reason: что означает для бизнеса ·<br/>допущение first-touch раскрыто явно
    IR-->>U: ответ: текст + SQL шагов + допущения + флаги + стоимость<br/>(трейс: ₽ по узлам, graph_version)
    OR->>C: put после успеха: SQL-шаблоны шагов + query_plan<br/>+ допущения clarify
```

## Чтение схемы

- **Номера шагов и LLM-вызовы:** вход и права (рубеж 1 — фильтрация мира начинается
  с `resolve_access`), clarify, кэш-lookup, план декомпозиции, цикл шагов, merge →
  `compact_result` → объяснение и `put`. Сообщения к LLM — только через узлы clarify,
  plan_joins, шаги, interpret: классификация — cheap, код/план — mid, текст
  пользователю — strong (ADR-007).
- **Права проверяются на каждом шаге** (ADR-005, рубежи 1–5): мир отфильтрован до
  модели (retrieval и подграф только по scope), валидатор — главный рубеж (полный
  обход AST + ACL вкл. колонки джойна + скелет на **каждый** шаг), БД — настоящий
  рубеж (read-only `agent_ro`, RLS + `SET LOCAL app.user_id`). Различение исходов —
  alt-блоки в цикле: галлюцинация → retry через diagnose; вне прав → degrade без
  retry, без подтверждения существования данных (S9).
- **Кэш** (ADR-009 п. 6): lookup — **после `clarify`**: сверять допущения текущего
  вопроса можно только когда они уже зафиксированы. Ключ — `rights_hash +
  graph_version + assumptions`, кэшируется SQL, не результат; хит не обходит гейт
  (S7): SQL проходит `validate_sql` + ACL + `EXPLAIN`. Hit → короткий путь:
  одиночный SQL — сразу `validate_sql`; декомпозированный — цикл `validate →
  execute` минуя `plan_query`/`generate_step_sql`. Miss — полный цикл, показанный
  на схеме. `put` — **оркестратор** по факту успешного прогона.
- **Ретраи — политика M1** (D-016, ADR-007 §4.1): попытка 1 — базовая модель;
  попытка 2 — та же модель + текст ошибки; попытка 3 — уровень вверх (после проверки
  остатка бюджета с резервом 0,6 ₽). Счётчики — на узел; глобальные капы — на прогон
  (≤ 8 вызовов / ≤ 90 s / ≤ 2 ₽) + `recursion_limit`.
- **Декомпозиция** (ADR-004 v1.2): триггер T1 решает код — основной путь на боевом
  масштабе; QueryPlan валидируется инвариантами до исполнения; ключи передаются
  только bind-параметрами; кап кардинальности 50 000, чанки 1000 и short-circuit —
  в executor'е, не в промпте; `LIMIT 1000` — только финал, с флагом усечения.
  **T2 (тяжёлый одиночный план) на этой схеме не показан**: исход — async/matview/
  approximate/отказ, не декомпозиция (state-agent `degraded_out`).
- **LLM никогда не сливает и не объясняет сырые строки:** merge — только код;
  `compact_result` отдаёт `row_count` + ≤ 20 строк + агрегаты; `interpret_result`
  получает компактный вид и обязан раскрыть допущения (first-touch): ответ без
  допущений не проходит правила SOP.
- **Что не показано:** лёгкий путь (без декомпозиции: `plan_joins → build_skeleton
  → generate_sql → validate_sql → check_cost → execute_sql → compact_result`) —
  та же FSM короче; **T2 (тяжёлый одиночный план)**: approximate/matview — через
  `compact_result → interpret_result` с флагом `approximate`, async/отказ — терминал
  (не декомпозиция, ADR-004); ретрай шага — `diagnose_error → generate_step_sql` с
  контекстом шага (ретраится упавший шаг, не весь план); отказ по бюджету (кап 2 ₽)
  — остановка с частичным ответом (incomplete-флаг); 429-шторм → backoff/breaker
  (прозрачно для сценария, узел ждёт; breaker — деградация, не пустая ошибка).

---

## Трассировка

Ревизии: v1.4 (T2-исходы уточнены, ретрай шага явен) · Связанные ADR: ADR-002, ADR-004, ADR-005, ADR-006, ADR-007, ADR-009 · Канон: hw-01/README.md §2, §3.1

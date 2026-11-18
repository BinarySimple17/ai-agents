# Sequence Diagram — сквозной путь тяжёлого запроса (с декомпозицией)

- **Статус:** каноническая схема v1.2 (2026-11-15 — граф: Neo4j вместо AGE, ADR-003 v2.0; v1.1 2026-11-14 — ревизия 02 §3.3: lookup после `clarify`, `put` — оркестратор, реплей декомпозиции)
- **Сценарий:** «Топ-10 категорий продуктов по выручке среди клиентов, пришедших из ads-кампаний за Q4 2026, и их средний чек» — джойн 5 таблиц, срабатывает T1 (пример плана — .docs §3.5.3)
- **Требования prd.md:** FR-1…FR-8, FR-12
- **Связанные:** state-agent.md (та же FSM в state-нотации), c4-l3-agent.md (компоненты), ADR-002, ADR-004, ADR-005, ADR-007, ADR-009

```mermaid
sequenceDiagram
    autonumber
    participant U as Пользователь (CLI/Streamlit)
    participant RA as resolve_access (код)
    participant CL as clarify (qwen — cheap)
    participant OR as Оркестратор (граф, код)
    participant C as Кэш Valkey
    participant PJ as plan_joins (qwen — cheap)
    participant PQ as plan_query (deepseek — mid, reasoning)
    participant GS as Шаг: generate_step_sql<br/>(deepseek → эскалация glm)
    participant V as validate (код): AST + ACL + граф
    participant EX as execute (код): agent_ro
    participant MG as merge_results (код)
    participant IR as interpret_result (glm — strong)
    participant DB as PostgreSQL<br/>(данные, pgvector, настройки)
    participant NEO as Neo4j<br/>(граф связей)

    U->>RA: вопрос + identity (проверенный токен)
    RA->>DB: agent_user_roles, agent_permissions<br/>(per-user кэш · версия — счётчик settings_version)
    DB-->>RA: роли → scope (allowed_tables, columns)
    RA->>CL: вопрос + права в state
    CL->>CL: Reason: однозначен (метрика, период)<br/>допущения зафиксированы

    alt вопрос неоднозначен
        CL-->>U: один уточняющий вопрос → done
    end

    OR->>C: lookup (scope, вопрос) — после clarify (ADR-009 п. 6)
    alt hit и допущения совпали
        C-->>OR: SQL + допущения (+ query_plan, sql_steps)
        OR->>V: короткий путь: одиночный SQL → validate_sql<br/>(декомпозированный — цикл validate_step → execute_step<br/>минуя plan_query/generate_step)
        Note over OR,V: хит → валидация → исполнение → ответ → done<br/>(сценарий ниже — miss)
    else miss (или допущения не совпали → miss)
        C-->>OR: miss
    end

    OR->>PJ: вопрос + допущения + scope
    PJ->>DB: retrieval: DDL, глоссарий, usage_notes (фильтр scope)
    PJ->>NEO: shortestPath по графу (fk + business рёбра;<br/>топология — синк из PG по settings_version, ADR-006)
    PJ->>PJ: Reason: ребро «заказ атрибутирован кампании»<br/>JoinPlan: 5 таблиц → T1 срабатывает (код, ≥ 4 таблиц)
    PJ->>PQ: JoinPlan (декомпозиция)
    PQ->>PQ: Reason: как разбить на простые шаги
    PQ-->>V: QueryPlan s1→s2→s3 (инварианты — код:<br/>≤ 4 шагов, ≤ 3 таблиц, топология, whitelist key-колонок)

    loop каждый шаг s1..s3
        PQ->>GS: purpose + шаблон + статистика key-set
        GS->>GS: генерация SQL шага (mid ·<br/>при retry — эскалация strong)
        GS->>V: SQL шага
        V->>V: полный обход AST + ACL + джойн по графу<br/>+ ANY(:keys) на whitelist-колонке

        alt нарушения — галлюцинация (таблицы/колонки нет)
            V->>GS: diagnose шага → регенерация (retry ≤ 3 общий)
        end
        alt таблица существует, но вне прав
            V-->>U: degrade: отказ/вариант без retry → done
        end

        V->>EX: ok
        EX->>DB: SELECT ... WHERE x = ANY(:keys)<br/>чанки по 1000 · LIMIT max_keys+1 · REPEATABLE READ

        alt 0 ключей (аналог пустой itab)
            EX-->>U: short-circuit: «данных нет» → done
        end
        alt max_keys+1 строк (переполнение)
            EX-->>U: уточняющий вопрос: сузить период/сегмент → done
        end

        EX->>PQ: ключи → state (следующий шаг)
    end

    PQ->>MG: шаги кончились
    MG->>MG: конкатенация чанков, дедуп, агрегация (только код)
    MG->>IR: код-свод: агрегаты + usage_notes (сырых строк нет)
    IR->>IR: Reason: что означает для бизнеса ·<br/>допущение first-touch раскрыто явно
    IR-->>U: ответ: текст + SQL шагов + допущения + стоимость (≈ 0,65 ₽)
    OR->>C: put после успеха: SQL-шаблоны шагов + query_plan<br/>+ допущения clarify (ADR-009 п. 6)
```

## Чтение схемы

- **Номера шагов и LLM-вызовы:** 1–4 — вход и права (рубеж 1 — фильтрация мира
  начинается с `resolve_access`), 5–6 — clarify, 7–10 — кэш-lookup, 11–17 — план
  декомпозиции, 18–28 — цикл шагов, 29–34 — merge, объяснение и `put`. Сообщения к
  LLM — только через узлы clarify (5), plan_joins (14), шаги (19), interpret (32):
  классификация — cheap, код/план — mid, текст пользователю — strong (ADR-007).
- **Права проверяются трижды** (ADR-005): мир отфильтрован до модели (шаги 12–13 —
  retrieval и подграф только по scope), валидатор — главный рубеж (шаг 21: полный
  обход AST + ACL на **каждый** шаг, не только финальный SQL), БД — coarse
  (шаг 25, read-only `agent_ro`). Различение исходов — два alt-блока в цикле:
  галлюцинация → retry через diagnose (22); вне прав → degrade без retry (23).
- **Кэш** (ADR-009 п. 6; ревизия 02 §3.3): lookup — **после `clarify`** (шаг 7):
  сверять допущения текущего вопроса можно только когда они уже зафиксированы
  (шаг 5). Hit → короткий путь (шаг 9): одиночный SQL — сразу `validate_sql`;
  декомпозированный — цикл `validate_step → execute_step` минуя
  `plan_query`/`generate_step`; на хите экономятся и retrieval, и shortestPath
  (шаги 12–13 не выполняются). Miss (или допущения не совпали → miss) — полный
  цикл, показанный на схеме. `put` — **оркестратор** (шаг 34) по факту успешного
  прогона: SQL-шаблоны шагов + `query_plan` + допущения `clarify` в значении.
- **Декомпозиция** (ADR-004): триггер T1 решает код (шаг 14); QueryPlan валидируется
  инвариантами до исполнения (17); ключи передаются только bind-параметрами; капы
  (`max_keys`, чанки 1000) и short-circuit — в executor'е, не в промпте. Retry
  бюджета **общий на план** — alt-блок регенерации (22) расходует его, исчерпание →
  `fail` (отказ + уточняющий вопрос).
- **LLM никогда не сливает результаты:** merge — только код (шаг 30);
  `interpret_result` (31) получает **код-свод** — агрегаты + `usage_notes`, сырых
  строк нет (ревизия 02 §3.5) — и обязан раскрыть допущения (first-touch):
  ответ без допущений не проходит правила SOP.
- **Что не показано:** легковесный путь (без декомпозиции: `plan_joins →
  generate_sql → validate_sql → check_cost → execute_sql`) — та же FSM короче; отказ
  по бюджету (кап 2 ₽) — остановка с частичным ответом; 429-шторм → backoff/breaker
  (прозрачно для сценария, узел ждёт); повторное T2 после отката → общий
  retry-бюджет, второе T2 → overflow (ADR-004 §4, ревизия 02 §3.4).

---

## Трассировка

Ревизии: v1.1 · Связанные ADR: ADR-002, ADR-004, ADR-005, ADR-007, ADR-009 · Канон: hw-01/README.md §3.1, §3.3

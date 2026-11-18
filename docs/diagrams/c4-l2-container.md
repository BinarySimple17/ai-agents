# C4 Level 2 — Container

- **Статус:** каноническая схема v1.7 (2026-11-15 — граф: Neo4j вместо AGE, ADR-003 v2.0; v1.6 — решения владельца FIX-005: кэш вопросов без семантической близости, источник `/metrics` — Streamlit-демо; v1.5 2026-11-14 — ревизия 02 + SAP-точка роста, ADR-011 v1.1) — включает развёртывание (отдельный deployment.md не нужен)
- **Требования prd.md:** FR-1…FR-14 (контур целиком), NFR-7 (смена провайдера конфигом)
- **Связанные:** ADR-002 (state machine), ADR-003 (хранилища: PG + Neo4j), ADR-005 (RBAC, AuthProvider), ADR-006 (синк топологии графа), ADR-007 (роутер моделей), ADR-008 (принудительные форматы), ADR-009 (кэш), ADR-010 (Observability Plane); code-architecture-draft.md (слои и порты); observability.md (сигналы наблюдаемости)

```mermaid
graph TB
    U["Пользователь<br/>(identity: проверенный токен)"]
    ADMIN["Администратор<br/>(владелец проекта)"]

    subgraph APP ["Приложение (один процесс Python)"]
        CLI["CLI (apps/cli)"]
        UI["Streamlit-демо (apps/web)<br/>выбор пользователя — живое демо RBAC"]
        ORCH["**Агент-оркестратор**<br/>LangGraph state machine — граф на коде (ADR-002):<br/>resolve_access · clarify · plan_joins · generate_sql ·<br/>validate_sql · check_cost · execute_sql ·<br/>diagnose_error · interpret_result<br/>+ план декомпозиции: plan_query, цикл шагов<br/>(generate_step_sql → validate_step → execute_step)<br/>и merge_results (ADR-004)"]
        GUARD["Guardrails (обычный код)<br/>AST-валидатор sqlglot + ACL — главный рубеж (ADR-005)<br/>проверка джойнов по графу (ADR-006)<br/>бюджет-кап 2 ₽ · retry ≤ 3 · backoff/breaker"]
        AUTHPORT["Порт AuthProvider<br/>DevTokenAuth (MVP) · OIDCAuth (production)"]
    end

    subgraph DATA ["Data Plane"]
        PG[("PostgreSQL — хранилище истины (ADR-003 v2.0)<br/>данные: 7 таблиц · read-only роль agent_ro<br/>pgvector: DDL, глоссарий, few-shot (RAG, фильтр по scope)<br/>настройки: agent_permissions · agent_user_roles ·<br/>agent_relationships · agent_data_sources · settings_version<br/>trace: query_trace — system of record (запись: agent_trace)")]
        NEO[("Neo4j — граф связей (ADR-003 v2.0)<br/>только топология: fk из каталога PG + business рёбра<br/>shortestPath · синк по settings_version (ADR-006)")]
        VK[("Valkey — только кэш, не истина (ADR-009)<br/>кэш вопросов (точное совпадение): ключ (scope, вопрос)<br/>справочные шаги декомпозиции с TTL")]
    end

    subgraph OBS ["Observability Plane (ADR-010)"]
        JAE["Jaeger all-in-one —<br/>трейсы (OTLP-native)"]
        PROM[("Prometheus — метрики,<br/>алерт-правила")]
        GRAF["Grafana — дашборды,<br/>поиск трейсов"]
        AM["Alertmanager —<br/>webhook (опц. контейнер)"]
    end

    LLM["LLM-шлюз routerai.ru (внешний)<br/>Роутер моделей: cheap qwen3.7-flash ·<br/>mid deepseek-v4.1-flash · strong glm-5.3-prime<br/>тул-based enforcement для structured output (ADR-008)"]
    IDP["IdP / портал (внешний)<br/>аутентификация до агента"]

    U -->|"CLI / Streamlit"| CLI & UI
    CLI & UI --> ORCH
    IDP -.->|"проверенная identity"| AUTHPORT
    AUTHPORT --> ORCH
    ORCH -->|"LLM-вызовы по ролям узлов"| LLM
    ORCH -->|"проверка кэша (scope, вопрос)"| VK
    ORCH --> GUARD
    GUARD -->|"ok / нарушения / deny"| ORCH
    ORCH & GUARD -->|"read-only: SQL, retrieval, настройки прав"| PG
    ORCH -->|"shortestPath (топология)"| NEO
    ORCH -.->|"трейс прогона → query_trace (роль agent_trace, INSERT-only)"| PG
    ORCH -.->|"OTLP: спаны (вне запросного пути)"| JAE
    PROM -.->|"скрейп /metrics (pull; источник — Streamlit-демо)"| ORCH
    PROM -.->|"алерт-правила"| AM
    PROM --> GRAF
    JAE --> GRAF
    ADMIN -.->|"дашборды, waterfall трейса"| GRAF
```

## Чтение схемы

- **Продуктовых деплой-единиц четыре** — развёртывание docker-compose: `app` (весь
  Control Plane: CLI/Streamlit + оркестратор + guardrails + порт авторизации),
  `db` (PostgreSQL + pgvector, официальный образ), `neo4j` (граф связей),
  `valkey`. Внешние:
  LLM-шлюз и IdP. Плюс плоскость телеметрии (ADR-010): jaeger, prometheus, grafana,
  alertmanager (опциональный контейнер) — та же внутренняя сеть, порты наружу
  не публикуются; экспорт телеметрии асинхронный, вне запросного пути, без
  коллектора (спаны — OTLP в Jaeger из любого процесса; метрики — `/metrics`
  долгоживущего Streamlit-демо под скрейпом Prometheus — CLI-прогон живёт секунды
  и не скрейбится, ADR-010 v1.2).
- **Оркестратор — единственный процесс агента.** Guardrails и порт AuthProvider —
  компоненты того же процесса (библиотеки за портами), не отдельные сервисы:
  масштаб уровня «учебный проект + пет-проект», не микросервисы.
- **Слои и порты** (code-architecture-draft): `domain` (сущности AgentState, JoinPlan,
  JoinEdge, QueryPlan, QueryStep) → `application` (сервисы узлов + порты `LLMProvider`,
  `VectorStore`, `GraphStore`, `SQLExecutor`, `SemanticCache`, `AuthProvider`, `Tracer`,
  `BudgetGuard`) → `infrastructure` (адаптеры: routerai-провайдер, pgvector, neo4j,
  sqlglot, Valkey, DevToken/OIDC) → `orchestration` (только рёбра графа LangGraph) →
  `composition` (DI-сборка). Правила импортов проверяются `import-linter`.
- **Роутер моделей — свойство провайдера:** `LLMProvider.complete(role, ...)` с тремя
  ролями `cheap / mid / strong`; узлы не знают конкретных моделей (ADR-007). Смена
  провайдера (routerai ↔ локальный Ollama) — переменная окружения, без правки узлов
  (NFR-7). Тулы принудительных форматов регистрируются в composition-слое (ADR-008).
- **Data Plane не публикует порты наружу** (docker-compose: только внутренняя сеть);
  приложение — единственный клиент `agent_ro` (read-only, timeout 30s, one-time
  hardening — ADR-005). Valkey — единственный компонент вне PostgreSQL, и он кэш:
  потеря содержимого — деградация скорости, не данных.
- **Потоки данных:** вопрос и права — внутрь state машины; наружу — только LLM-промпты
  (текст вопроса + DDL разрешённых таблиц) и запись трейса. Результаты SQL к LLM
  уходят только в `interpret_result` и всегда компактным сводом, собранным кодом:
  агрегаты результата (row_count, суммы/средние по числовым колонкам) и при
  «списочных» вопросах не более 20 строк; сырые строки сверх N к LLM не ходят
  никогда — ни на лёгком пути, ни после merge_results (ADR-004; ревизия 02 §3.5).
- **Наблюдаемость (observability.md, ADR-010):** трейс прогона — в `query_trace`
  (system of record: включая `node_timings`/`ttft_ms` по LLM-вызовам); live — спаны
  OTLP в Jaeger, метрики — скрейп `/metrics` (источник — Streamlit-демо)
  Prometheus'ом, дашборды и поиск
  трейсов — Grafana, уведомления — Alertmanager; логи — структурные JSON в stdout
  контейнера; алерт-правила — по симптомным сигналам (§4 observability.md).
- **Точки роста за портами** (не меняя схему): `VectorStore` → отдельная векторная БД
  при миллионах точек; `GraphStore` → Neo4j (перенос рёбер и Cypher один-в-один);
  `AuthProvider` → корпоративный SSO; `AccessPolicy` → внешний policy-движок (revisit
  ADR-005); `SQLExecutor` → внешний источник SAP (спека QuerySpec по HTTPS — ручка
  на ABAP строит Open SQL и проверяет права; ADR-011).

---

## Трассировка

Ревизии: v1.5 · Связанные ADR: ADR-002…011 · Канон: hw-01/README.md §3.2

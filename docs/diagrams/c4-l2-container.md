# C4 Level 2 — Container

- **Статус:** каноническая схема v1.9 (2026-11-21 — S09: пул agent_planner — +INSERT заявок agent_edge_proposals, миграция 0009, D-030; остальное — v1.8). v1.8 (2026-11-18 — развёртывание N1 (ревью-03, D-017 Python-native): асинхронный API — `agent-api` FastAPI/uvicorn (stateless Deployment + HPA; `POST /ask → 202 + job_id`, `GET /ask/{id}/stream` — SSE) и `agent-worker` arq (очередь — Valkey Streams; Deployment); PostgreSQL StatefulSet + PVC, миграции — Alembic Job; раздельные psycopg-пулы по ролям (`agent_planner`/`agent_ro`); NetworkPolicy — egress только к LLM-шлюзу; OTel-instrumentation fastapi/psycopg/httpx; аудит (S8) отдельно от трейсов; `asyncio.timeout(90)`; minikube — целевой dev-контур. v1.7 — Neo4j вместо AGE; v1.6 — решения владельца FIX-005 (кэш точного совпадения, источник `/metrics` — Streamlit-демо); v1.5 — ревизия 02 + SAP)
- **Требования prd.md:** FR-1…FR-14 (контур целиком), NFR-1 (p95/TTFT), NFR-7 (смена провайдера конфигом)
- **Связанные:** ADR-002 (state machine), ADR-003 (хранилища: PG + Neo4j), ADR-005 (RBAC, AuthProvider, рубеж БД), ADR-006 (реестр-SoT, синк снапшота графа), ADR-007 (роутер моделей), ADR-008 (принудительные форматы), ADR-009 (кэш), ADR-010 (Observability Plane); code-architecture-draft.md (слои и порты); observability.md (сигналы наблюдаемости); security.md §5 (сеть)

```mermaid
graph TB
    U["Пользователь<br/>(identity: проверенный токен)"]
    ADMIN["Администратор<br/>(владелец проекта)"]
    OWNER["Владелец данных<br/>(ревью реестра связей — SQL-настройки)"]

    subgraph APP ["Продуктовый контур — Python-native (D-017), k8s (minikube — целевой dev)"]
        API["**agent-api** — FastAPI + uvicorn (async)<br/>POST /ask → 202 + job_id (валидация входа, AuthProvider)<br/>GET /ask/{id}/stream — SSE прогресс/результат<br/>stateless Deployment + HPA · asyncio.timeout(90)<br/>OTel: fastapi-instrumentation"]
        WORKER["**agent-worker** — arq (Deployment)<br/>очередь — Valkey Streams: прогон FSM-графа LangGraph:<br/>resolve_access · clarify (interrupt() + checkpointer) · plan_joins ·<br/>build_skeleton · plan_query · generate_sql · generate_step_sql ·<br/>validate_sql · check_cost · execute_sql · merge_results ·<br/>compact_result · diagnose_error · interpret_result · degrade<br/>OTel: psycopg/httpx-instrumentation · asyncio.timeout(90)"]
        CLI["CLI (apps/cli)<br/>OTLP-спаны + query_trace"]
        UI["Streamlit-демо (apps/web)<br/>выбор пользователя — живое демо RBAC;<br/>долгоживущий процесс — источник /metrics (ADR-010 v1.2)"]
        GUARD["Guardrails (библиотека, не сервис)<br/>AST-валидатор sqlglot + ACL вкл. колонки джойна — главный рубеж (ADR-005)<br/>джойны ⊆ edge_id реестра (ADR-006)<br/>бюджет: per-node ≤ 3 (M1) + recursion_limit · кап 2 ₽ (резерв 0,6 ₽)<br/>backoff/breaker → деградация"]
        AUTHPORT["Порт AuthProvider<br/>DevTokenAuth (MVP) · OIDCAuth (production)"]
    end

    subgraph DATA ["Data Plane"]
        PG[("PostgreSQL — StatefulSet + PVC — хранилище истины (ADR-003)<br/>данные: 7 таблиц · роли: agent_planner (планировщик, search_path<br/>со служебными схемами) · agent_ro (исполнитель, read-only, RLS)<br/>· agent_trace (трейс, INSERT-only) · registry_writer (владелец реестра)<br/>pgvector: DDL, глоссарий, few-shot (RAG, фильтр по scope)<br/>настройки: agent_permissions · agent_user_roles ·<br/>agent_relationships (реестр-SoT) · agent_edge_proposals ·<br/>agent_data_sources · settings_version · аудит S8 (append-only)<br/>trace: query_trace — system of record<br/>миграции — Alembic Job (единственный способ создать схему, D-009)")]
        NEO[("Neo4j — граф связей (ADR-003)<br/>производный снапшот реестра: только топология active-рёбер (edge_id);<br/>graph_version пинится на прогон; синк по settings_version (ADR-006 §4.3)")]
        VK[("Valkey — кэш + чекпоинтер + очередь (ADR-009)<br/>кэш вопросов (точное совпадение): ключ rights_hash + graph_version + assumptions<br/>сессии clarify (checkpointer, TTL 10 мин)<br/>очередь задач — Streams (arq)")]
    end

    subgraph OBS ["Observability Plane (ADR-010)"]
        JAE["Jaeger all-in-one —<br/>трейсы (OTLP-native)"]
        PROM[("Prometheus — метрики,<br/>алерт-правила")]
        GRAF["Grafana — дашборды,<br/>поиск трейсов"]
        AM["Alertmanager —<br/>webhook (опц. контейнер)"]
    end

    NP["NetworkPolicy (k8s):<br/>egress подов агента — только LLM-шлюз<br/>(+ Data Plane/OBS внутри кластера);<br/>ingress к agent-api — через ingress-контроллер"]

    LLM["LLM-шлюз routerai.ru (внешний)<br/>Роутер моделей: cheap qwen3.7-flash ·<br/>mid deepseek-v4.1-flash · strong glm-5.3-prime<br/>тул-based enforcement для structured output (ADR-008)"]
    IDP["IdP / портал (внешний)<br/>аутентификация до агента"]

    U -->|"POST /ask → 202 + job_id; SSE"| API
    U -->|"CLI / Streamlit"| CLI & UI
    API -->|"enque job (Valkey Streams)"| VK
    VK -->|"dequeue"| WORKER
    IDP -.->|"проверенная identity"| AUTHPORT
    AUTHPORT --> API
    API & WORKER --> GUARD
    WORKER -->|"LLM-вызовы по ролям узлов"| LLM
    WORKER -->|"кэш: lookup после clarify, ключ с правами и graph_version"| VK
    WORKER & GUARD -->|"пул agent_planner: retrieval, настройки, EXPLAIN,<br/>INSERT заявек agent_edge_proposals (v1.9 S09)"| PG
    WORKER & GUARD -->|"пул agent_ro: исполнение SQL (read-only, RLS + SET LOCAL)"| PG
    WORKER -->|"find_join_paths: k путей по снапшоту"| NEO
    WORKER -.->|"трейс прогона → query_trace (роль agent_trace, INSERT-only)"| PG
    WORKER -.->|"аудит S8 — append-only, отдельно от трейсов"| PG
    OWNER -.->|"ревью рёбер (registry_writer) · заявки agent_edge_proposals"| PG
    WORKER -.->|"OTLP: спаны (вне запросного пути)"| JAE
    PROM -.->|"скрейп /metrics (pull; источник — Streamlit-демо)"| UI
    PROM -.->|"алерт-правила"| AM
    PROM --> GRAF
    JAE --> GRAF
    ADMIN -.->|"дашборды, waterfall трейса"| GRAF
    NP -.->|"ограничивает"| API & WORKER
```

## Чтение схемы

- **Развёртывание — Python-native, k8s (N1, D-017; minikube — целевой dev-контур,
  compose остаётся для локальной отладки):**
  - **`agent-api` — FastAPI + uvicorn (async, stateless Deployment + HPA):**
    принимает `POST /ask` → `202 + job_id` (не блокирует соединение на долгий
    прогон), прогресс и результат — `GET /ask/{id}/stream` (SSE); на каждом хендлере
    `asyncio.timeout(90)` — дедлайн прогона enforced на границе API; stateless —
    сессии в checkpointer (Valkey), состояние прогона в checkpointer/state, горизонтальное
    масштабирование без sticky-сессий.
  - **`agent-worker` — arq (Deployment):** тяжёлые прогоны FSM-графа вне
    request-цикла; очередь — **Valkey Streams** (у Valkey они уже есть — отдельный
    брокер не добавляется); N воркеров → параллелизм прогонов, ретраи джобов —
    на уровне очереди, не модели.
  - **CLI/Streamlit** — точки входа дев-контура: CLI — OTLP-спаны + query_trace;
    Streamlit-демо — живое демо RBAC и источник `/metrics` (ADR-010 v1.2).
- **PostgreSQL — StatefulSet + PVC** (официальный образ pgvector/pgvector);
  **схема создаётся только миграциями Alembic** (`make infra-migrate`; Job/init-контур
  при деплое — D-009). **Раздельные psycopg-пулы по ролям:** планировщик
  (`agent_planner` — retrieval, настройки, `EXPLAIN`, INSERT заявок (S09), `search_path` со служебными
  схемами графа), исполнитель (`agent_ro` — read-only, RLS + `SET LOCAL
  app.user_id`, без служебных схем), трейс (`agent_trace`, INSERT-only), владелец
  реестра (`registry_writer` — запись рёбер только через ревью; агент read-only).
  Pooler в transaction-pooling-режиме — только `SET LOCAL` (security §2.1).
- **NetworkPolicy:** egress подов агента — только к LLM-шлюзу (плюс Data Plane и
  Observability Plane внутри кластера); ingress — через ingress-контроллер к
  `agent-api`; Data Plane наружу портов не публикует (security §5).
- **Guardrails и порт AuthProvider — библиотеки, не сервисы:** компоненты того же
  кода, что воркер/api (порты приложения); масштаб уровня «учебный проект +
  пет-проект», не микросервисы.
- **Слои и порты** (code-architecture-draft, AGENTS «Architecture Boundaries»):
  `composition` (DI-сборка) над двумя НЕЗАВИСИМЫМИ ногами — `orchestration`
  (только рёбра графа LangGraph) и `infrastructure` (адаптеры: routerai-провайдер,
  pgvector, neo4j, sqlglot, Valkey, DevToken/OIDC; единственный вместе с
  composition читает `config`) — обе внутрь: `application` (сервисы узлов + порты
  `LLMProvider`, `VectorStore`, `SchemaGraphStore`, `SQLExecutor`, `SemanticCache`,
  `AuthProvider`, `Tracer`, `BudgetGuard`; инфраструктура реализует только порты)
  → `domain` (сущности AgentState, JoinPlan, JoinEdge, QueryPlan, QueryStep,
  ResultCompact). Точки входа `apps` — над `composition`; `apps → {infrastructure,
  orchestration, config}` запрещены напрямую (приложение не импортирует адаптеры,
  граф и конфиг минуя composition). `orchestration` ↔
  `infrastructure` независимы (линейная цепочка «orchestration → infrastructure»
  из прежней формулировки — FIX-018). Правила импортов проверяются `import-linter`
  (`[tool.importlinter]` в pyproject.toml, 10 контрактов — S30).
- **Роутер моделей — свойство провайдера:** `LLMProvider.complete(role, ...)` с тремя
  ролями `cheap / mid / strong`; узлы не знают конкретных моделей (ADR-007). Смена
  провайдера (routerai ↔ локальный Ollama) — переменная окружения, без правки узлов
  (NFR-7). Тулы принудительных форматов регистрируются в composition-слое (ADR-008).
- **Data Plane не публикует порты наружу**; приложение — единственный клиент
  `agent_ro` (read-only, timeout 30s, one-time hardening — ADR-005). Valkey и Neo4j —
  вне PostgreSQL, оба — производные: потеря содержимого Valkey — деградация
  скорости; потеря Neo4j — деградация планирования («путь не найден»), данные и
  семантика — в PG (перестройка снапшота из реестра).
- **Потоки данных:** вопрос и права — внутрь state машины; наружу — только LLM-промпты
  (текст вопроса + DDL разрешённых таблиц) и запись трейса/аудита. Результаты SQL к
  LLM уходят только через `compact_result`: `row_count`, агрегаты, ≤ 20 строк и
  флаги усечения/`approximate`; сырые строки сверх N к LLM не ходят никогда — ни на
  лёгком пути, ни после `merge_results`. **Аудит (S8) — отдельная append-only
  таблица, отдельно от трейсов** (пользователь, вопрос, SQL, `edge_id`,
  `graph_version`, строки, отказы, стоимость, уровни моделей).
- **Наблюдаемость (observability.md, ADR-010):** OTel-instrumentation —
  **fastapi** (hendler-спаны), **psycopg** (SQL-спаны), **httpx** (LLM-вызовы);
  трейс прогона — в `query_trace` (system of record: включая `node_timings`/
  `ttft_ms`); live — спаны OTLP в Jaeger, метрики — скрейп `/metrics` (источник —
  Streamlit-демо) Prometheus'ом, дашборды и поиск трейсов — Grafana, уведомления —
  Alertmanager; логи — структурные JSON в stdout подов; алерт-правила — по
  симптомным сигналам (§4 observability.md).
- **Точки роста за портами** (не меняя схему): `VectorStore` → отдельная векторная БД
  при миллионах точек; `SchemaGraphStore` → альтернативное хранилище снапшота
  (BFS по adjacency — критерий переключения, D-015); `AuthProvider` → корпоративный
  SSO; `AccessPolicy` → внешний policy-движок (revisit ADR-005); `SQLExecutor` →
  внешний источник SAP (спека QuerySpec по HTTPS — ручка на ABAP строит Open SQL и
  проверяет права; ADR-011).

---

## Трассировка

Ревизии: v1.8 (развёртывание N1: agent-api/agent-worker, k8s, NetworkPolicy, OTel) · Связанные ADR: ADR-002…011 · Канон: hw-01/README.md §3.2 (развёртывание — N1 ревью-03)

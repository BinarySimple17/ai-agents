# Architecture

Полная архитектурная спецификация — архитектурный пакет v1.8: [arch-package.md](arch-package.md)
(порядок чтения, матрица «вопрос → документ», история), живые прикладные требования —
[prd.md](prd.md); историческая полная спецификация — `.docs/hw-01/README.md` (вне гита,
READ ONLY — D-011; `hw-01/README.md` — решение ДЗ-1, ужатый артефакт сдачи). Этот файл —
краткая свёртка, с которой агент стартует сессию; при расхождении источник — пакет.

## Overview

Python-стек (D-017): API — FastAPI (`POST /ask → 202 + job_id`, SSE-стрим), тяжёлые
прогоны — arq-воркеры за очередью Valkey Streams (развёртывание — [c4-l2 v1.8](diagrams/c4-l2-container.md));
валидатор — sqlglot, контракты — pydantic. Инфраструктура: PostgreSQL — единственное
хранилище истины (бизнес-данные + pgvector + настройки прав + query_trace + аудит),
граф связей — Neo4j (ADR-003), Valkey — кэш вопросов по точному совпадению, чекпоинтер
`clarify` и очередь задач (ADR-009), LLM — за OpenAI-совместимым шлюзом (облако routerai.ru или локальный LM Studio — D-020) с
Intent-Based роутингом «узел → навык → модель» (cheap/mid/strong, ADR-007). Оркестрация
— workflow-скелет: FSM на коде с агентным ядром (ADR-002); модель наполняет узлы, код
ведёт маршрут. Наблюдаемость — Observability Plane (ADR-010).

## Components

Код-архитектура — по [code-architecture-draft §2](`.drafts/code-architecture-draft.md`, вне гита; слои и порты — [c4-l2](diagrams/c4-l2-container.md)) с поправкой D-009: весь код — в `src/` (пакет, apps, миграции Alembic, тесты):

| Компонент | Ответственность | Технология |
|---|---|---|
| `src/apps/` (cli, index_knowledge, sync_graph, streamlit_app) | точки входа: CLI-прогон, индексация знаний, синк графа и Streamlit-демо RBAC (живой выбор пользователя) | Python, argparse / streamlit |
| `src/text_to_sql_agent/orchestration/` | LangGraph state machine: граф и условные переходы — только рёбра | langgraph |
| `.../application/services/` | по одному классу на узел (clarify, plan_joins, generate_sql, …) | чистая логика + порты |
| `.../application/ports/` | Protocol-интерфейсы: LLMProvider (complete с tools/usage, complete_stream — S19, embed), VectorStore, SchemaGraphStore, SQLExecutor, SemanticCache, AuthProvider + PermissionStore (S05: identity/права), Tracer, BudgetGuard, GraphRunner (S04; run/resume — interrupt-сессии S08), InterruptGate (S08: пауза узла — interrupt + checkpointer) | typing.Protocol |
| `.../domain/` | сущности (AgentState, JoinPlan), value objects, ошибки — без внешних зависимостей | Python + pydantic |
| `.../infrastructure/` | адаптеры портов: openai_provider, pgvector_store, neo4j_graph_store, postgres_executor, valkey_cache, guardrails (sqlglot-валидатор), jsonl_tracer | psycopg, openai SDK, neo4j, redis-py, sqlglot |
| `.../config/` | pydantic-settings: единственная точка чтения env | pydantic-settings |
| `.../composition/` | единственное место сборки зависимостей (container) | — |
| `src/migrations/` | инициализация и эволюция схемы БД — только миграции Alembic (`make infra-migrate`) | alembic + sqlalchemy |
| `src/tests/` | unit / integration / e2e (БД-тесты скипаются без инфры) | pytest |

Инфраструктура: PostgreSQL 17 + pgvector (официальный образ), Neo4j (граф связей, ADR-003 v2.0) и Valkey — docker compose; live-наблюдаемость (ДЗ-06, ADR-010) — Jaeger + Prometheus + Grafana (+Alertmanager) как Observability Plane.

## Boundaries & Layer Model

- Allowed dependencies: `apps → composition → orchestration → application/services → domain`; `infrastructure` реализует порты `application/ports` (зависимость направлена внутрь); domain — без внешних зависимостей и без LLM.
- Forbidden: узлы вызывают друг друга напрямую (только через state machine, ADR-002); бизнес-код читает env мимо `config.Settings`; сборка зависимостей вне `composition`; выполнение SQL в обход read-only роли `agent_ro` и валидатора (ADR-005); LLM управляет маршрутом.
- Границы слоёв проверяются механически — `import-linter`, 10 контрактов в `[tool.importlinter]` (pyproject.toml): два layers, independence `orchestration` ↔ `infrastructure`, семь forbidden — среди них `apps → {infrastructure, orchestration, config}` напрямую (ревью I1 S30) и `langgraph` вне orchestration/infrastructure; `lint-imports` внутри `./init.sh` — нарушение роняет `make check` (S30, FIX-018 — выравнивание c4-l2 с контрактами).

## Constraints

Инварианты, которые нельзя нарушать (полный список MUST NOT — [AGENTS.md](../AGENTS.md#constraints); обоснования — ADR-002/004/005/006/009):

1. **Граница прав = граница мира агента** (ADR-005): подграф, retrieval и DDL — только из `agent_permissions`; валидатор deny-by-default по типам узлов AST + deny-list схем + ACL колонок соединения.
2. **Текст от LLM — не доверенный** (ADR-005, ADR-008; S31/D-033 п.5 — единый рубеж на ОБОИХ рёбрах исполнения): AST-валидация до выполнения — FSM-ребро (скелет не изменён — `skeleton_hash`, джойны ⊆ `edge_id`) и тул-контур CLI (`RunSqlTool`: права по verified-identity → `validate_free` — общий ACL + инвариант реестра); структурированные выходы — pydantic-контракты + constrained decoding/тулы; `EXPLAIN` — да, `EXPLAIN ANALYZE` — никогда.
3. **Выполнение** (ADR-005): read-only роль `agent_ro`, `LIMIT 1000`, `statement_timeout` 30s, RLS + `SET LOCAL app.user_id`, раздельные коннекты планировщик/исполнитель; запись трейса — отдельная INSERT-only роль `agent_trace`; аудит (S8) — append-only, вне state.
4. **Лимиты задаёт код** (ADR-002/004/007): политика ретраев M1 — ≤ 3 попыток на узел (попытка 2 — та же модель + текст ошибки, попытка 3 — уровень вверх при достатке бюджета, D-016); `recursion_limit` на граф; глобальные капы ≤ 8 вызовов / ≤ 90 s / ≤ 2 ₽ с резервом 0,6 ₽ (`budget_stop`: «неполное не выдаётся за полное» — incomplete-флаг); T2 — async/matview/approximate/отказ, не декомпозиция.
5. **Связи не выдумываются** (ADR-006 v1.7): джойны только по активным рёбрам реестра — обоими контурами (граф — производный снапшот с `graph_version`; хранилище заменяемо за `find_join_paths()`/`join_edges()` — D-015; reconciliation-тест), тип/направление — из ребра; тул-контур сверяет пару операндов каждого JOIN со снимком (S31/R1).
6. **Кэш не отравляется и не обходит гейт** (ADR-009, S7): ключ `rights_hash + graph_version + assumptions`, кэшируется SQL, не результат; на hit — валидация + ACL + `EXPLAIN`; lookup — после `clarify`, `put` — оркестратор по факту успеха.

## Decision Records

11 принятых ADR — реестр: [docs/adr/README.md](adr/README.md); порядок чтения и трассировка FR → ADR — [prd.md](prd.md) §9. Оперативные решения сессий — [DECISIONS.md](../DECISIONS.md); архитектурно значимые из них переносятся в ADR.

## Related Documents

- [Архитектурный пакет](arch-package.md) — порядок чтения, матрица «вопрос → документ», история
- [ТЗ (prd.md)](prd.md) — FR/NFR и матрица трассировки
- [Модель данных (ER)](diagrams/er-data-model.md) — каноническая схема данных и инварианты
- [Контейнеры и развёртывание (C4 L2)](diagrams/c4-l2-container.md) — топология и сетевые границы
- [FSM агента](diagrams/state-agent.md) — терминальные исходы и лимиты
- [Карта документации](README.md) — приоритеты источников при конфликте

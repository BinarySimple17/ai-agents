# Architecture

Полная архитектурная спецификация — архитектурный пакет v1.7: [arch-package.md](arch-package.md)
(порядок чтения, матрица «вопрос → документ», история), живые прикладные требования —
[prd.md](prd.md); историческая полная спецификация — `.docs/hw-01/README.md` (вне гита,
READ ONLY — D-011; `hw-01/README.md` — решение ДЗ-1, ужатый артефакт сдачи). Этот файл —
краткая свёртка, с которой агент стартует сессию; при расхождении источник — пакет.

## Overview

Один процесс Python плюс общая инфраструктура: PostgreSQL — единственное хранилище
истины (бизнес-данные + pgvector + настройки прав + query_trace), граф связей — Neo4j
ADR-003), Valkey — только кэш вопросов по точному совпадению (ADR-009 v1.2), LLM — за
OpenAI-совместимым шлюзом routerai.ru с Intent-Based роутингом «узел → навык → модель»
(cheap/mid/strong, ADR-007). Оркестрация — workflow-скелет: FSM на коде с агентным
ядром (ADR-002); модель наполняет узлы, код ведёт маршрут. Развёртывание —
[c4-l2-container.md](diagrams/c4-l2-container.md) (Data Plane + Observability Plane, ADR-010).

## Components

Код-архитектура — по [code-architecture-draft §2](`.drafts/code-architecture-draft.md`, вне гита; слои и порты — [c4-l2](diagrams/c4-l2-container.md)) с поправкой D-009: весь код — в `src/` (пакет, apps, миграции Alembic, тесты):

| Компонент | Ответственность | Технология |
|---|---|---|
| `src/apps/` (cli, streamlit_app) | точки входа: CLI-прогон и Streamlit-демо RBAC (живой выбор пользователя) | Python, argparse / streamlit |
| `src/text_to_sql_agent/orchestration/` | LangGraph state machine: граф и условные переходы — только рёбра | langgraph |
| `.../application/services/` | по одному классу на узел (clarify, plan_joins, generate_sql, …) | чистая логика + порты |
| `.../application/ports/` | Protocol-интерфейсы: LLMProvider (complete с tools/usage + embed), VectorStore, SchemaGraphStore, SQLExecutor, SemanticCache, AuthProvider, Tracer, BudgetGuard | typing.Protocol |
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

## Constraints

Инварианты, которые нельзя нарушать (полный список MUST NOT — [AGENTS.md](../AGENTS.md#constraints); обоснования — ADR-002/004/005/006/009):

1. **Граница прав = граница мира агента** (ADR-005): подграф, retrieval и DDL — только из `agent_permissions`; валидатор deny-by-default по типам узлов AST.
2. **Текст от LLM — не доверенный** (ADR-005, ADR-008): AST-валидация до выполнения; структурированные выходы — через constrained decoding/тулы; `EXPLAIN` — да, `EXPLAIN ANALYZE` — никогда.
3. **Выполнение** (ADR-005): read-only роль `agent_ro`, `LIMIT 1000`, `statement_timeout` 30s; запись трейса — отдельная INSERT-only роль `agent_trace`.
4. **Лимиты задаёт код** (ADR-002/004): retry ≤ 3, эскалация только вверх, бюджет-кап 2₽ (`budget_stop`: «неполное не выдаётся за полное» — incomplete-флаг), кольцо T2 ограничено счётчиком.
5. **Связи не выдумываются** (ADR-006): джойны только по рёбрам графа (топология в AGE, семантика из PG — reconciliation-тест), тип/направление — из семантики ребра.
6. **Кэш не отравляется** (ADR-009): scope в ключе, сверка допущений на hit; lookup — после `clarify`, `put` — оркестратор по факту успеха.

## Decision Records

11 принятых ADR — реестр: [docs/adr/README.md](adr/README.md); порядок чтения и трассировка FR → ADR — [prd.md](prd.md) §9. Оперативные решения сессий — [DECISIONS.md](../DECISIONS.md); архитектурно значимые из них переносятся в ADR.

## Related Documents

- [Архитектурный пакет](arch-package.md) — порядок чтения, матрица «вопрос → документ», история
- [ТЗ (prd.md)](prd.md) — FR/NFR и матрица трассировки
- [Модель данных (ER)](diagrams/er-data-model.md) — каноническая схема данных и инварианты
- [Контейнеры и развёртывание (C4 L2)](diagrams/c4-l2-container.md) — топология и сетевые границы
- [FSM агента](diagrams/state-agent.md) — терминальные исходы и лимиты
- [Карта документации](README.md) — приоритеты источников при конфликте

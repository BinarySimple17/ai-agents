# ADR — Architecture Decision Records

Индекс архитектурных решений проекта. Формат — по практике ADR (Michael Nygard), правила — по образцу [walkinglabs/learn-harness-engineering](https://github.com/walkinglabs/learn-harness-engineering).

- **Что сюда попадает:** значимые архитектурные решения — выбор технологий, модели данных, границы сервисов, протоколы безопасности. Один ADR = одно решение.
- **Что НЕ сюда:** оперативные решения сессий (журнал [DECISIONS.md](../../DECISIONS.md) в корне) и факты (они — в [ARCHITECTURE.md](../ARCHITECTURE.md) и [docs/diagrams/](../diagrams/)).

Формат принятых ADR-001…011: шапка → §1 Контекст → §2 Сравнение по осям → §3 Варианты →
§4 Решение → §5 Последствия (+/−, revisit triggers) → §6 Ссылки → трассировка.
[ADR-TEMPLATE.md](ADR-TEMPLATE.md) — справочник структуры нового ADR; правила жизни — ниже.

## Реестр

| ADR | Решение | Статус |
|---|---|---|
| [ADR-001](ADR-001-build-vs-buy.md) | Своё на LangGraph vs готовое Text-to-SQL (Vanna/LangChain/LlamaIndex) | Принято v1.0 |
| [ADR-002](ADR-002-fsm-on-code.md) | Оркестрация: workflow-скелет (FSM на коде) с агентным ядром; два уровня предохранителей (per-node M1 + recursion_limit); единый рубеж — ребро FSM (тул-контур удалён — BL-048/D-041) | Принято v1.7 |
| [ADR-003](ADR-003-single-postgres.md) | Хранилища: PostgreSQL (pgvector) + Neo4j (граф); PG — истина, граф — производный снапшот реестра, хранилище заменяемо за find_join_paths() | Принято v2.1 |
| [ADR-004](ADR-004-query-decomposition.md) | Декомпозиция тяжёлых запросов (T1 — основной путь; T2 → async/matview/approximate; key-set без LIMIT, кап 50 000) | Принято v1.2 |
| [ADR-005](ADR-005-rbac-as-data.md) | Права как данные (настройки в БД), рубежи 1–5 (AST+скелет, deny-list схем, ACL колонок соединения, БД: RLS), AuthProvider | Принято v1.4 |
| [ADR-006](ADR-006-relationship-semantics.md) | Реестр связей — единственный SoT: контракт ребра R1, жизненный цикл draft → CI → active, graph_version, заявки; инвариант — явные JOIN рёбра плана, вложенные тела — ACL + «таблицы ⊆ план» (BL-051(a)); join_operator='=' — контракт v1 (BL-050(c)) | Принято v1.9 |
| [ADR-007](ADR-007-model-cascade.md) | Intent-Based Routing: узел → навык → модель (cheap/mid/strong), политика ретраев M1 (D-016), экономика — фикс. снимок 18.11.2026 | Принято v1.2 |
| [ADR-008](ADR-008-structured-output.md) | Валидные структурированные выходы: constrained decoding + тул-based enforcement | Принято v1.0 |
| [ADR-009](ADR-009-semantic-cache.md) | Кэш вопросов (точное совпадение): scope в ключе, сверка допущений на hit, позиция после clarify | Принято v1.2 |
| [ADR-010](ADR-010-observability-stack.md) | Live-наблюдаемость: OTel SDK → Jaeger + Prometheus (/metrics — Streamlit-демо) → Grafana (+Alertmanager) | Принято v1.2 |
| [ADR-011](ADR-011-external-sap-datasource.md) | Внешний источник SAP: OAuth за AuthProvider; QuerySpec → ABAP-ручка строит Open SQL | Принято v1.1 |

Полный порядок чтения и матрица «вопрос → документ» — [arch-package.md](../arch-package.md).

## Как заводить новый ADR

1. Номер — следующий по порядку (ADR-012); файл — `ADR-0NN-краткое-имя.md` в этой папке.
2. Формат — как в принятых ADR-001…011 (секции §1–§6 + трассировка); [ADR-TEMPLATE.md](ADR-TEMPLATE.md) — структурный справочник.
3. Заголовки секций сохранять; подсказки-комментарии удалить перед принятием.
4. Изменение уже принятого ADR — **новая версия в том же файле** с датой (история версий — в поле «Статус»), не тихая правка.
5. Решение, меняющее модель данных, — синхронно обнови [diagrams/er-data-model.md](../diagrams/er-data-model.md); меняющее схемы/топологию — соответствующие диаграммы (тот же PR).
6. Добавь строку в реестр выше и в «Состав пакета» [arch-package.md](../arch-package.md).

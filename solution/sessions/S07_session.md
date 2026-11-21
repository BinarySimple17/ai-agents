# S07 — Граф связей Neo4j: снапшот реестра, синк + reconciliation (веха V3, ДЗ-04)

Этап: Веха V3 (ДЗ-04: графовая половина памяти) | Владелец: агент | Зависимости: S02 | Оценка: 1.5 | Статус: [ ] не начата
Фичи: feat-007 — Граф связей (Neo4j); её зависимость feat-002 уже в passing
Роль по фиче: закрывает feat-007 — финальная сессия, через make verify-feature

## ⚠️ Прочитай перед началом

Исполнитель: ИИ-агент. К моменту старта: S02 закрыта — Neo4j поднят (bolt, аутентификация
`NEO4J_PASSWORD` из .env), миграции схемы v1.7 применены (`agent_relationships` с
kind/meaning/join_type/usage_notes сидированы миграцией 0003 — датасет ДЗ-04 «сиды
демо-схемы + agent_relationships» уже есть, проверьте полноту semantics для демо-вопроса
«клиенты из рекламы»: first-touch в usage_notes). Эта сессия строит графовую половину
памяти ДЗ-04 (hw-plan §4): снапшот реестра рёбер в Neo4j + графовое API путей джойнов.
Архитектура — ADR-003 v2.0: **Neo4j хранит только топологию** (таблицы, рёбра rel_id,
направления, join_type), вся семантика (meaning, usage_notes) читается из
`agent_relationships` PostgreSQL по rel_id — в граф не дублируется. Синк — по счётчику
`settings_version` + reconciliation-тест (ADR-006 §4.3). hw-plan упоминает AGE —
историческая формулировка (READ ONLY, D-011); канон проекта — Neo4j (ADR-003 v2.0),
расхождение уже снято решением ADR, не править hw-plan.

Границы скоупа: БЕЗ узла plan_joins (S09 — ранжирование/JoinPlan/ретраи), БЕЗ
retrieval-усиления (S26), БЕЗ FSM-узлов (V4), БЕЗ правки сидов 0003 сверх дополнения
семантики демо-рёбер (если нужно — новая миграция).

### Правила для исполнителя

1. Ветка `feat-007-<slug>` от `milestone/v3`, НЕ от `dev`/`milestone/v2` (D-029: веха V3
   изолирована до приёмки V2); merge рабочей ветки — в `milestone/v3`.
2. Neo4j — только топология с rel_id; семантика — джойном с `agent_relationships` по
   rel_id в PG (ADR-003 v2.0, «Neo4j — не источник истины»).
3. Порт `SchemaGraphStore` — в `application/ports/` по образцу `VectorStore` (S06):
   Protocol + `RunContext`; синк строки порта в S00 тем же коммитом (анти-дрейф).
4. Схема PG — только новыми миграциями (S00 правило 5; D-028); правка применённой
   запрещена. Синк `docs/diagrams/er-data-model.md` в том же коммите, если миграция
   меняет схему.
5. BL-003 п.3 (периметр registry_writer): решить в этой сессии — кто и под какой ролью
   читает реестр для синка (writer без SELECT по принципу наименьших привилегий;
   инструменту владельца — отдельная роль/DSN, не расширение writer). Решение — в
   DECISIONS.md, если меняет грани; GRANT — новой миграцией.

## Предусловия

- [ ] Сессия СТАРТУЕТ: статус в шапке переведён в «◐ в работе» и ARCH-GATE резолвлен ДО кода (canon-check architect ✅ либо обоснованный N/A) — старт-гейт (`guard-subagents.sh --phase start`, D-027) проверяет только стартованные (◐) сессии; «[ ] не начата» — план, гейт её скипает
- [ ] S02 закрыта: `make infra-up && docker compose ps neo4j` → Neo4j healthy; `uv run pytest src/tests/integration/test_infra_schema.py -q` → passed
- [ ] `make check` зелёный (baseline репозитория)
- [ ] feat-002 в state=passing, feat-007 активирована в state=active до старта работ (verify-feature требует active — Ловушки-2)
- [ ] Bolt отвечает с паролем из .env: `uv run python -c "<пинг драйвера из тестов>"` → подключение ок (хост 127.0.0.1, не localhost — D-020)

## Задачи

### Task 1: Порт SchemaGraphStore

`src/text_to_sql_agent/application/ports/schema_graph_store.py`: Protocol
(runtime checkable, по образцу `vector_store.py` S06) — методы: `sync()` (снапшот
реестра → граф, идемпотентно), `graph_version()` (из `settings_version`), поиск путей
джойнов по снапшоту: `find_join_paths(from_table, to_table, *, ctx, k)` — k взвешенных
путей по активным рёбрам (веса — из реестра), `path_semantics(edge_ids)` — семантика
рёбер из `agent_relationships`. Scope/контекст — `RunContext` (agent_planner-пул,
c4-l2 v1.8 — тот же канал, что retrieval S06). Синк строки порта в S00 тем же коммитом.

**✅ Самопроверка:**

```bash
uv run python -c "from text_to_sql_agent.application.ports.schema_graph_store import SchemaGraphStore"   # импорт ок
uv run ruff check src
```

---

### Task 2: Адаптер Neo4j + синк топологии

`src/text_to_sql_agent/infrastructure/graph/neo4j_graph_store.py`: реализация порта.
Синк: active-рёбра `agent_relationships` + FK-рёбра из каталога PostgreSQL → вершины
(таблицы) и рёбра (rel_id, join_type, направление, weight); снапшот маркирован
`graph_version` (полная перезагрузка снапшота — топология маленькая, диф не нужен;
решение зафиксировать комментарием «почему»). Точка запуска синка — по образцу
`index_knowledge` (S06, ruling N2): `src/apps/sync_graph.py` + make-цель
`make sync-graph`; подключение в `Container`. Neo4j-клиент — зависимость через uv,
DSN/пароль — только через Settings.

**✅ Самопроверка:**

```bash
make infra-up && make sync-graph   # отчёт: сколько вершин/рёбер загружено, graph_version
```

---

### Task 3: Семантика путей + веса

`path_semantics(edge_ids)`: SELECT из `agent_relationships` по rel_id — meaning,
join_type, usage_notes, intent_tags. Для демо-вопроса ДЗ-04 («клиенты, пришедшие из
рекламы») семантика ребра orders↔campaigns обязана раскрывать допущение first-touch
(usage_notes); если в сидах 0003 этого нет — дополнить НОВОЙ миграцией данных (правка
применённой запрещена, D-028), sync `docs/diagrams/er-data-model.md` тем же коммитом.

**✅ Самопроверка:**

```bash
uv run pytest src/tests/integration/test_join_graph.py -q   # семантика в путях (новые тесты) green
```

---

### Task 4: Периметр registry_writer (BL-003 п.3)

Решить и провести: чтение реестра для синка — под ролью agent_planner (GRANT уже есть
из 0002) или отдельной ролью синка; инструменту владельца (реальный registry_writer:
читает + пишет) — отдельная роль/DSN, не расширение writer. GRANT — новой миграцией,
er sync тем же коммитом. Решение — строкой в отчёте сессии; при смене грани ролей —
запись в DECISIONS.md.

**✅ Самопроверка:**

```bash
make infra-migrate && uv run pytest src/tests/integration/test_infra_schema.py -q   # гранты применены, роли соответствуют решению
```

---

### Task 5: Интеграционные тесты + reconciliation

`src/tests/integration/test_join_graph.py` (команда верификации фичи): (a) путь между
связанными таблицами по реальным рёбрам; (b) пути нет → null, джойн не выдумывается;
(c) семантика каждого ребра пути из настроек; (d) reconciliation: строки active-рёбер
+ FK каталога = рёбра графа (ADR-006 §4.3); (e) правка строки настроек → graph_version
растёт → повторный sync меняет граф; (f) фильтр прав: таблица вне прав пользователя —
пути через неё не возвращаются (deny-by-default, недоступное не существует в мире
агента).

**✅ Самопроверка:**

```bash
make infra-up && make sync-graph && uv run pytest src/tests/integration/test_join_graph.py -q   # green (верификация фичи)
```

---

### Task 6: Артефакт hw-04 (связи между объектами)

Раздел «Связи между объектами» в `hw-04/README.md` (по образцу hw-03): рёбра с
семантикой — вывод `path_semantics` для демо-пары (customers←orders→campaigns),
скриншот/вывод синка. Итоговую сборку README делает последняя закрывающаяся сессия
вехи (S09 или S26 — смотрите статусы индекса).

**✅ Самопроверка:**

```bash
grep -c "shortestPath\|graph" hw-04/README.md   # раздел существует и непуст
```

## Критерии приёмки

- [ ] Для пары таблиц возвращается путь по реальным рёбрам или null — джойн не выдумывается (behavior feat-007)
- [ ] Для каждого ребра пути возвращается семантика (join_type, meaning, usage_notes) из настроек (behavior feat-007)
- [ ] Reconciliation green: строки настроек + FK каталога = рёбра графа; graph_version инкрементируется правкой настроек
- [ ] Фильтр прав: пути через недоступные таблицы не возвращаются (deny-by-default)
- [ ] BL-003 п.3 решён (роль/DSN чтения реестра; GRANT миграцией) — отражён в отчёте
- [ ] Раздел hw-04 «Связи между объектами» написан
- [ ] Фича закрыта: `make verify-feature F=feat-007` → PASS, state=passing с evidence

## Ловушки

1. **Пустой граф молча роняет все тесты путей** — запускайте `make sync-graph` до
   интеграционных тестов; скип без инфры даёт ложную зелёность локально, но не в CI-контуре
   verify-feature.
2. Neo4j-драйвер: хост 127.0.0.1, не localhost (Windows `::1` — D-020); пароль —
   только из Settings (.env), никогда в коде.
3. Не тащите семантику в граф «для удобства» — ADR-003 v2.0 запрещает: расхождение
   граф↔реестр станет невидимым; reconciliation-тест (d) ловит только топологию.
4. LLM не строит пути: `find_join_paths` — детерминированный код по снапшоту;
   ранжирование кандидатов — S09 (там LLM наполняет, код решает — инвариант hw-01 §2.1).
5. verify-feature strict-режим отвергает N/A «сессия не начата» — к закрытию заменить
   обе строки гейтов на evidence/фактическую причину (Ловушки-3 S06).
6. Правка применённой миграции запрещена (S00-5, D-028): дополнение семантики сида —
   новым файлом миграции.
7. AGE в hw-plan — историческая формулировка; канон — Neo4j (ADR-003 v2.0), hw-plan
   READ ONLY не править (D-011).

## Гейты субагентов (заполняется по факту, до закрытия сессии)

- ARCH-GATE: N/A — сессия не начата (план батча V3; canon-check — при старте, D-027)
- REVIEW-GATE: N/A — сессия не начата (ревью диапазона — по факту закрытия)

## Отчёт по сессии (заполняется по факту)

- Сделано:
  - [Task: что реально сделано, коммиты, число тестов/ассертов, live-прогон]
- Отклонения от плана:
  - [что сделали не так, как в задаче, и почему; каждое отклонение — осознанное]
- Перенесено в другие сессии:
  - [ID сессии — что перенесено и почему; если в беклог — BL-NN-файл]

# BL-002 — Синхронизация канона docs/ с ревью-03 (hw-01/README.md уже отработан)

- **Статус:** открыт
- **Дата:** 2026-11-18
- **Сессия-источник:** apply-review-03 (ревизия hw-01/README.md по `.docs/review-03.md` + патч автора; FIX-006, D-015/D-016/D-017)
- **Приоритет:** P1

## Что обнаружено

Ревью-03 отработано в артефакте сдачи `hw-01/README.md` (полная ревизия: 15 узлов, реестр-SoT §3.5, ретраи M1, экономика E1–E6, §8 развёртывание, §9 eval, `AgentState`). Канон `docs/` остался на прежних решениях и теперь расходится с hw-01 по содержательным пунктам (полный список расхождений — FIX-006 «Следствие»).

## Почему вне скоупа сессии

Решение владельца: объём «только hw-01/README.md + процессные артефакты» — синхронизация ~15 файлов канона отдельной сессией, чтобы не делать её спешно в хвосте ревизии.

## Груз синхронизации (по файлам)

- **ADR-006 → v1.3** (реестр — единственный SoT, принцип 1): FK — генератор кандидатов, не ребро; контракт ребра R1 (полный DDL — `edge_id, version, status(draft/active/deprecated), replaced_by, left/right table+columns[], join_operator, join_type, cardinality, fanout_guard, mandatory_predicates, temporal, meaning, intent_tags[], usage_notes, weight, domain, owner, reviewed_at, ci_report`); жизненный цикл `draft → CI → active` (CI: колонки/типы/уникальность/сироты/измеренный fan-out); `graph_version` пинится на прогон (state, кэш, аудит — заменить/дополнить `settings_version`); заявки `agent_edge_proposals`; bootstrap FK + майнинг SQL/BI; data steward для кросс-доменных; cold start / time-to-edge; SAP-оговорка с `MANDT`; запись только владельцем через ревью, агент read-only
- **ADR-007 → v1.2** (D-016): ретраи M1 (2 — та же модель + ошибка, 3 — уровень вверх; ≤3 на узел; капы ≤8 вызовов / ≤90 s / ≤2 ₽; резерв 0,6 ₽ под `interpret_result`); экономика E1–E6 (0,61/0,62/0,67 ₽; 1000 ≈ 670 ₽; базлайн без retry 2,57 ₽; экономия 76%; 6,6К токенов на прогон, 0,9–2,6К на вызов); бенчмарки Spider 2.0/BIRD; structured output pydantic (temperature 0); circuit breaker → деградация, не пустая ошибка; рычаг `interpret_result` (M8, не в базовой версии)
- **ADR-005 → v1.2 + security.md**: четвёртый рубеж — ACL колонок соединения из `agent_permissions` (S2); deny-list схем `pg_catalog/information_schema/ag_catalog/agent_*` (S3); БД — настоящий рубеж: RLS + `SET LOCAL app.user_id`, `default_transaction_read_only`, `statement_timeout` на сессию, раздельные коннекты планировщик/исполнитель (S4); аудит append-only (S8: пользователь, вопрос, SQL, edge_id, graph_version, строки, отказы, стоимость, уровни моделей); отказ без раскрытия существования данных (S9); инъекции второго порядка — маркировка tool-output/retrieval (S6)
- **ADR-004 → v1.2**: T2 → async/matview/approx/отказ с рекомендацией, не декомпозиция (A8); T1 — основной путь на боевом масштабе, заглушка — только стенд (A9, снять «план Б по остатку времени»); промежуточные key-set без `LIMIT`, cap 50 000 ключей, флаг `approximate` (A2); лимиты `deadline 90 s / statement_timeout 30 s / max_sql_executions 4` (A10); fan-out guard + предагрегация в CTE (A3, вместе с ADR-006)
- **ADR-003**: добавить принцип «граф — производный снапшот реестра за `find_join_paths()`, хранилище заменяемо» + критерий переключения (BFS по adjacency) и ссылку на факты AGE §F ревью (канон остаётся Neo4j — D-013/D-015)
- **prd.md**: FR-7 — убрать двухисточниковую формулировку «FK-рёбра вычитываются из каталога автоматически» (реестр-SoT); FR-13/кэш — не обходит гейт (hit → validate_sql + ACL + EXPLAIN; ключ `rights_hash + graph_version + assumptions`; кэшируется SQL, не результат — S7); §6/§7 — eval-метрики N4 (execution accuracy, **ACL-leak = 0 hard gate**, join-correctness по edge_id, fan-out-агрегаты, честные отказы, точность уточнений, p95, ₽/запрос, эскалации, покрытие, time-to-edge; red-team: injection в вопросе/данных/RAG/usage_notes; CI-гейт: не мержить при ACL-leak > 0 или −3 п.п. accuracy)
- **ARCHITECTURE.md**: убрать устаревший «AGE» в constraint 5 (канон — Neo4j); constraint 4 — эскалация по M1 (D-016)
- **diagrams**: `state-agent` — 15 узлов (добавить `build_skeleton`, `compact_result`, probe `COUNT`; переименовать `generate_step` → `generate_step_sql`; `recursion_limit` вторым предохранителем); `c4-l3` — k путей + ранжирование вместо shortestPath, скелет, компакция; `sequence-query` — ретраи M1, probe, compact_result; `c4-l2` — развёртывание N1 (agent-api FastAPI+HPA, agent-worker arq/Valkey Streams, async-API 202+SSE, раздельные psycopg-пулы, NetworkPolicy egress, OTel, аудит); `er-data-model` — DDL контракта ребра (R1), `agent_edge_proposals`, `graph_version`, аудит-поля S8, pydantic-контракты `JoinPlan/QueryPlan/ResultCompact` + `AgentState` (N3)
- **model-card**: бенчмарки Spider 2.0/BIRD вместо SWE-bench/HumanEval (M4); снимок каталога routerai.ru 18.11.2026 (618 моделей, имена проверены) + фиксация тарифов с датой (M7/D-017); FastAPI/arq в стеке
- **RISKS**: R-D3 — cold start («пока реестр пуст, агент бесполезен»), метрики «доля вопросов с активным путём»/time-to-edge, mitigation без FK-автопула; R-C1 — 81% → 76%
- **S00/канон сессий**: список узлов FSM, контракт checkpointer/`interrupt()` для clarify (TTL 10 мин), Valkey-очередь задач, `recursion_limit`; **до старта S02** — er-data-model с контрактом ребра (feat-002 пишет первую миграцию по ER)

## Закрытие (заполняется при закрытии)

Сессия синхронизации docs/ (владелец одобрил объём «hw-01 отдельно, канон отдельно»); критерий закрытия — grep-сверка: в живых доках нет «каждый retry вверх» (старая политика), 3,31/81%, двухисточниковой FK-формулировки; 6,6К/76%/2,57 присутствуют; версии ADR-004/005/006/007 подняты, Состав пакета + история arch-package обновлены.

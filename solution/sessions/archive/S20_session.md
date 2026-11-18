# S20 — Синхронизация архпакета docs/ с ревью-03 (+ addition: Python-стек)

Этап: пред-V1 (канон, доки) | Владелец: агент | Зависимости: S00, S01 (+ harness-сессия apply-review-03 завершена) | Оценка: 1.5 | Статус: ✅ 2026-11-18 — канон сведён к ревизии hw-01 по ревью-03 (BL-002 закрыт, пакет v1.8)
Фичи: — (док-сессия: канон `docs/`; state фич в `feature_list.json` не меняется — только поля behavior/description)
Роль по фиче: — (не закрывает и не продолжает фичи; приёмка — grep-сверка канона с hw-01 и версии ADR)

## ⚠️ Прочитай перед началом

Исполнитель — автономный агент. К моменту старта:

- `hw-01/README.md` **уже отработан** по `.docs/review-03.md` + `.docs/review-03-addition.md` (патч «стек: Python»): полная ревизия, коммит `1022a45` на ветке `hw-01-agent-architecture`; вся harness-работа — на ветке `project-harness` (`43d9594`). Сессия сводит канон `docs/` к этому эталону, **не наоборот**.
- Решения уже приняты и пересматриваться не должны: **D-015** (hw-01 остаётся на AGE + мост «хранилище — заменяемая деталь за `find_join_paths()`»; канон — Neo4j), **D-016** (ретраи M1: попытка 2 — та же модель + текст ошибки, попытка 3 — уровень вверх; supersede «каждый retry вверх» из D-014), **D-017** (чистый Python-стек, Q5 снят; дефолты Q1–Q4/Q6–Q7 с TODO(confirm); тарифы зафиксированы по снимку каталога routerai.ru от 18.11.2026 — не пересчитывать по свежим).
- Груз синхронизации — [BL-002](../backlog/BL-002-docs-sync-review-03.md) (полный список по файлам), противоречия — FIX-006, эта сессия его закрывает.
- **Что сознательно НЕ делается:** `hw-01/README.md` не трогается (эталон); `.docs/**` — READ ONLY (D-011), review-03*.md — только входы, без статус-строк; код `src/**` не пишется (это не имплементационная сессия); возврат канона с Neo4j на AGE не выполняется.

### Правила для исполнителя

1. Канон — Neo4j (D-013/D-015): из ревью берутся **принципы** (реестр-SoT, k путей вместо кратчайшего как правила, снапшот за портом), не движок AGE. Факты AGE §F уже живут в hw-01 §3.1 — в ADR-003 достаточно ссылки и моста заменяемости.
2. Каждая правка ADR — новая версия в том же файле + синхронная строка в «Состав пакета» и «История» [arch-package.md](../../docs/arch-package.md) тем же заходом (правила ADR-README); ER-диаграмма — в том же заходе, что и DDL-контракт ребра.
3. Экономика — только зафиксированные значения (D-017): 0,61/0,62/0,67 ₽; 1000 ≈ 670 ₽; базлайн без retry 2,57 ₽; экономия 76%; 6,6К токенов на прогон; кап 2 ₽ с резервом 0,6 ₽. Старые 3,31 ₽/81% из живого текста убираются.
4. `settings_version` (ADR-006 §4) не дублируется: `graph_version` — производная ревизии реестра, пинится на прогон (state, кэш, аудит); синк-контракт и reconciliation-тест сохраняются.
5. Спорное (не покрывается D-015…D-017) — вопрос куратору/запись в DECISIONS, не тихая правка; обнаруженное вне скоупа — BL-NN.

## Предусловия

- [x] Ревизия ДЗ на месте: `git log --oneline -1 -- hw-01/README.md` → `1022a45` ✅; отклонение: `grep -c "История ревизий" hw-01/README.md` → `0`, а не 1 — блока «История ревизий» в ужатом артефакте нет (авторская ужимка 1022a45 сняла его вместе с §8/§9; FIX-006 упоминал доужатую версию). Суть предусловия (эталон по ревью-03 на месте) подтверждена содержанием: §2.1/§3.1/§3.2/§3.3/§3.5/§4.3/§6/§7 несут все решения ревью; зафиксировано в отчёте
- [x] `make check` зелёный (baseline): EXIT=0 (uv sync OK, ruff OK, 1 passed)
- [x] BL-002 открыт и является грузом: файл на месте, «Статус: открыт»
- [x] Фич-зависимостей нет (док-сессия) — states feature_list не менялись

## Задачи

### Task 1: ADR-006 → v1.3 — реестр `agent_relationships` как единственный SoT (R1–R7, принципы 1–3)

Переписать модель связей: FK — генератор кандидатов в очередь владельцу, не ребро; полный контракт ребра (DDL из BL-002: `edge_id … ci_report(jsonb)`); жизненный цикл `draft → CI → active` с проверками по факту (колонки/типы/уникальность/сироты/**измеренный fan-out против кардинальности**); `deprecated + replaced_by`; вьюха `active` для агента; запись — только владелец через ревью, агент read-only, изменения в аудит; `graph_version` (пин на прогон, согласование с `settings_version` §4); заявки `agent_edge_proposals`; bootstrap FK + майнинг SQL/BI-вьюх; data steward для кросс-доменных; cold start / time-to-edge / «владелец — узкое место»; SAP-оговорка с `MANDT`; кардинальность + `fanout_guard` → защита агрегатов (A3, вместе с ADR-004).

**✅ Самопроверка:**

```bash
grep -c "вычитываются из каталога автоматически" docs/adr/ADR-006-relationship-semantics.md docs/prd.md   # 0 и 0
grep -c -e edge_id -e mandatory_predicates -e fanout_guard -e graph_version -e agent_edge_proposals docs/adr/ADR-006-relationship-semantics.md   # каждое ≥ 1
```

---

### Task 2: ADR-007 → v1.2 + model-card + RISKS — ретраи M1 и экономика E1–E6 (D-016/D-017)

Политика попыток (1 — базовая; 2 — та же + текст ошибки; 3 — уровень вверх при достатке бюджета); счётчики на узел ≤ 3 + глобальные капы ≤ 8 вызовов / ≤ 90 s / ≤ 2 ₽; проверка остатка с резервом 0,6 ₽ перед эскалацией. Экономика: таблица узлов (0,0041/0,0074/0,0222/0,0084/0,5786 ₽), итоги, базлайн 2,57 ₽ (без retry, сравнение корректное), 76%, вклад равный (≈0,98 + ≈0,98 ₽), 6,6К токенов на прогон / 0,9–2,6К на вызов. Бенчмарки: Spider 2.0 / BIRD + LMArena (убрать SWE-bench/HumanEval как основу выбора). Structured output — pydantic `JoinPlan`/`QueryPlan`/`ResultCompact`, temperature 0. Circuit breaker — деградация (валидный SQL без объяснения / отложенная задача). M8 — рычаг `interpret_result`, вне базовой версии. model-card: снимок каталога 18.11.2026 (618 моделей, имена проверены), фиксация тарифов с датой. RISKS: R-C1 — 81% → 76%.

**✅ Самопроверка:**

```bash
grep -c -e "3,31" -e "81%" docs/adr/ADR-007-model-cascade.md docs/RISKS.md   # 0 и 0 (живой текст)
grep -c -e "2,57" -e "76%" -e "6,6К" docs/adr/ADR-007-model-cascade.md   # каждое ≥ 1
grep -c -e "SWE-bench" -e "HumanEval" docs/adr/ADR-007-model-cascade.md docs/model-card.md docs/prd.md   # 0 (живой текст)
grep -c "Spider 2.0" docs/adr/ADR-007-model-cascade.md docs/model-card.md   # ≥ 1
```

---

### Task 3: ADR-005 → v1.2 + security.md — рубежи S2–S4, аудит S8, отказ S9, инъекции S6

Четыре рубежа: (2) AST-валидатор — одна инструкция, запрет `SET/COPY/CALL/DO` и функций deny-list, обход CTE/UNION/подзапросов/LATERAL, `LIMIT` к внешнему запросу, диалект postgres, deny-list схем `pg_catalog/information_schema/<схемы графа>/agent_*`; (3) ACL колонок соединения из `agent_permissions` (нет PII-флага на ребре); (4) БД — настоящий рубеж: RLS + `SET LOCAL app.user_id`, `default_transaction_read_only`, `statement_timeout` на сессию, раздельные коннекты планировщик/исполнитель, pgbouncer transaction pooling — только `SET LOCAL`. Скелет в валидаторе (S1): хэш не изменён, `mandatory_predicates` на месте, джойны ⊆ `edge_id`. Аудит append-only (поля S8), вне state, PII-редакция. Отказ без раскрытия существования данных (Q3 default). Инъекции второго порядка: маркировка tool-output/retrieval как данных. Кэш не обходит гейт (S7): ключ `rights_hash + graph_version + assumptions`, кэшируется SQL (FR-13 prd — Task 6).

**✅ Самопроверка:**

```bash
grep -c -e "information_schema" -e "SET LOCAL" -e "RLS" docs/security.md docs/adr/ADR-005-rbac-as-data.md   # каждое ≥ 1
grep -c "skeleton_hash\|хэш скелета\|хэш JOIN-скелета" docs/security.md docs/adr/ADR-005-rbac-as-data.md   # сумма ≥ 2
```

---

### Task 4: ADR-004 → v1.2 — декомпозиция A2/A8/A9/A10

Промежуточные key-set без `LIMIT`, cap кардинальности 50 000 ключей, превышение → async / отказ / флаг `approximate`; T2 (тяжёлый `EXPLAIN`) → async / matview / приблизительный расчёт / отказ с рекомендацией — **не** декомпозиция; снять «план Б / по остатку времени» — T1 основной путь на боевом масштабе, заглушка — только отладочный стенд; лимиты `deadline 90 s / statement_timeout 30 s / max_sql_executions 4`, проверка остатка перед каждым выполнением и эскалацией; semi-join по массиву ключей / batch-fetch (без внешних аналогий).

**✅ Самопроверка:**

```bash
grep -c -e "по остатку времени" -e "план Б" docs/adr/ADR-004-query-decomposition.md docs/prd.md docs/ARCHITECTURE.md   # 0 (живой текст)
grep -c -e "50 000" -e approximate docs/adr/ADR-004-query-decomposition.md   # ≥ 1
```

---

### Task 5: ADR-003 (мост) + ADR-002 (провода M1)

ADR-003: принцип «граф — производный снапшот реестра с `graph_version`; хранилище — заменяемая деталь за `find_join_paths()`» + критерий переключения (BFS по adjacency) + ссылка на факты AGE (живут в hw-01 §3.1); path-поиск — k взвешенных путей с фильтрацией по свойствам рёбер + ранжирование топ-5 моделью, при равных — clarify (A6); `shortestPath` — имя функции Neo4j, не решающее правило планировщика. ADR-002: `recursion_limit` вторым предохранителем поверх per-node счётчиков; карту решений дополнить M1 (tier назначает код; попытка 2 — тот же tier).

**✅ Самопроверка:**

```bash
grep -c -e "k взвешенных\|k-shortest\|топ-5" docs/adr/ADR-003-single-postgres.md docs/diagrams/c4-l3-agent.md   # сумма ≥ 2
grep -c "recursion_limit" docs/adr/ADR-002-fsm-on-code.md docs/diagrams/state-agent.md   # сумма ≥ 2
```

---

### Task 6: prd.md → v1.8 — требования под новую политику

FR-7 — реестр-SoT (снять двухисточниковую FK-фразу); FR-8/FR-10 — ретраи M1; FR-13 — кэш через гейт (ключ `rights_hash + graph_version + assumptions`, кэшируется SQL, не результат); FR-11 — валидатор (скелет, deny-list, ACL колонок); FR-14/NFR — кап 2 ₽ с резервом 0,6 ₽, дедлайн 90 s, `max_sql_executions 4`; §6 — eval-критерии N4: **ACL-leak = 0 (hard gate)**, execution accuracy, join-correctness по `edge_id`, fan-out-агрегаты, честные отказы, точность уточнений, p95, ₽/запрос, эскалации, покрытие, time-to-edge; §7 KPI — согласовать; §8 допущения — пометки TODO(confirm) по Q1–Q4/Q6–Q7 (Q5 снят).

**✅ Самопроверка:**

```bash
grep -c "ACL-leak" docs/prd.md   # ≥ 1
grep -c -e "rights_hash" -e "graph_version" docs/prd.md   # ≥ 1
grep -c "вычитываются из каталога автоматически" docs/prd.md   # 0
```

---

### Task 7: Диаграммы — узлы, последовательность, развёртывание, ER

- `state-agent` v1.3: состав 15 узлов = hw-01 §2.1 (добавить `build_skeleton`, `compact_result`, probe `COUNT`; переименовать `generate_step` → `generate_step_sql`; `degrade`, `merge_results` на месте); два уровня предохранителей (per-node + `recursion_limit`); ретраи M1 на рёбрах; `interrupt()`-ветка clarify (checkpointer, TTL 10 мин).
- `c4-l3-agent` v1.4: k путей + ранжирование вместо shortestPath-правила; скелет от кода; компакция перед `interpret_result`.
- `sequence-query` v1.3: попытки M1, probe `COUNT`, `compact_result`, флаги усечения/approximate в ответе.
- `c4-l2-container` v1.8 (addition N1): `agent-api` (FastAPI + uvicorn, stateless Deployment + HPA, async), `agent-worker` (Deployment, Valkey Streams, arq — обоснование одной строкой), `POST /ask → 202 + job_id` + `GET /ask/{id}/stream` SSE, PostgreSQL StatefulSet + PVC (роли `agent_planner`/`agent_ro`/`registry_writer`, миграции Alembic Job), раздельные `psycopg_pool` по ролям, NetworkPolicy (egress только LLM-шлюз), OTel (fastapi/psycopg/httpx), аудит отдельно от трейсов, `asyncio.timeout(90)`; всё Python-native, minikube как целевой dev-контур.
- `er-data-model` v1.5: DDL контракта ребра (R1), `agent_edge_proposals`, `graph_version`, аудит-поля S8, pydantic-контракты `JoinPlan`/`QueryPlan`/`ResultCompact`, состав `AgentState` (N3).

**✅ Самопроверка:**

```bash
for n in resolve_access build_skeleton generate_step_sql merge_results compact_result degrade; do grep -l "$n" docs/diagrams/state-agent.md; done   # 6 строк-имен файлов
grep -c -e "FastAPI" -e arq docs/diagrams/c4-l2-container.md   # ≥ 2
grep -c -e agent_edge_proposals -e graph_version docs/diagrams/er-data-model.md   # ≥ 2
```

---

### Task 8: Свёртки, S00, feature_list, таблица сессий

- `ARCHITECTURE.md`: убрать stale «AGE» в constraint 5 (канон — Neo4j + мост); constraint 4 — эскалация по M1; Overview — Python-стек (FastAPI, arq, sqlglot, pydantic).
- `observability.md`: OTel-instrumentation fastapi/psycopg/httpx; аудит-таблица — отдельно от трейсов; PII-редакция.
- `S00_session.md` (канон, по правилу «правка контракта = правка S00 + согласование»): список узлов FSM (15), контракт checkpointer + `interrupt()` (TTL 10 мин), `recursion_limit`, Valkey-очередь задач, FastAPI/arq в стеке точек входа.
- `feature_list.json`: только поля behavior/description фич под новую политику (feat-004 — recursion_limit; feat-008 — interrupt/checkpointer; feat-009/010 — ретраи M1 и k путей; feat-011 — скелет/deny-list/ACL колонок; feat-014 — M1; feat-015 — вход compact_result; feat-016 — кэш через гейт + graph_version; feat-017 — cap 50 000/approximate, T2 → async). **states не менять.**
- `solution/sessions.md`: строки S07 (k путей вместо shortestPath), S09/S10 (M1), S12 (T2 → async), S14 (M1), S17 (T1 основной, cap 50 000).

**✅ Самопроверка:**

```bash
make check   # EXIT=0
node scripts/validate-harness.mjs .   # Overall: 100/100
python -c "import json; d=json.load(open('feature_list.json', encoding='utf-8')); print(all(f['state'] in ('not_started','active','passing') for f in d['features']))"   # True
```

---

### Task 9: Реестры и закрытие BL-002

`arch-package.md`: «Состав пакета» — новые версии (ADR-004 v1.2, ADR-005 v1.2, ADR-006 v1.3, ADR-007 v1.2, prd v1.8, security v1.6, state-agent v1.3, c4-l3 v1.4, c4-l2 v1.8, sequence v1.3, er v1.5, model-card/RISKS/observability по факту); «История» — строка «отработка ревью-03 + addition в каноне» с перечнем ID (A/S/R/M/E/N/C/F + N1/N2/N7, Q5 снят). Закрыть BL-002 (шапка `Статус: закрыт (2026-11-DD: синхронизировано в S20)` + строка реестра беклога). `progress.md`: Evidence (grep-сверка целиком), Files Modified, Current State.

**✅ Самопроверка:**

```bash
grep -c "отработка ревью-03\|ревью-03" docs/arch-package.md   # ≥ 1 (строка Истории)
grep "Статус:" solution/backlog/BL-002-docs-sync-review-03.md   # закрыт
```

## Критерии приёмки

- [x] В живом тексте `docs/` отсутствуют (grep-сверка — Evidence в progress.md): двухисточниковая FK-формулировка, «каждый retry … уровень вверх» как политика, `3,31`/`81%`, «по остатку времени»/«план Б», SWE-bench/HumanEval, «608», FOR ALL ENTRIES/Java/Spring — всё 0
- [x] Присутствуют и согласованы: `6,6К`/`76%`/`2,57`/`670`, `graph_version`, `build_skeleton`/`compact_result`/`generate_step_sql`/`merge_results`, `interrupt()`/checkpointer, `recursion_limit`, `ACL-leak`, `Spider 2.0`/`BIRD`, `50 000`/`approximate`, резерв `0,6 ₽`
- [x] Состав узлов идентичен в `state-agent.md` v1.3, `c4-l3-agent.md` v1.4 и hw-01 §2.1 (15 узлов; `generate_step` → `generate_step_sql` каскадом: диаграммы, S00, feature_list, sessions.md)
- [x] ADR-002 v1.2 / 003 v2.1 / 004 v1.2 / 005 v1.2 / 006 v1.3 / 007 v1.2 подняты версиями; «Состав пакета» и «История» arch-package обновлены тем же заходом (+adr/README, docs/README, diagrams/README); ER v1.5 согласован с DDL контракта ребра (ADR-006 §4.1 — единственный источник)
- [x] Канон остаётся на Neo4j (D-013) с мостом заменяемости (D-015, ADR-003 v2.1); стек канона — чистый Python (D-017), упоминаний Java/Spring не добавлено (grep 0)
- [x] BL-002 закрыт (шапка + раздел «Закрытие» + реестр беклога); `make check` → EXIT=0; `node scripts/validate-harness.mjs .` → Overall: 100/100
- [x] Фичи не тронуты по states (python-проверка → True); behavior/description feat-004/008/009/010/011/014/015/016/017 синхронизированы

## Ловушки

1. **Не тащить AGE из hw-01 в канон.** hw-01 описывает AGE (так требует ревью §F), канон — Neo4j; расхождение легально именно через мост «снапшот реестра за `find_join_paths()`» (D-015). Правка ADR-003 «назад на AGE» — ошибка.
2. **История версий ADR против grep-гейтов.** Если в файле ADR остаётся исторический блок со старыми числами (3,31/81%) — помечать его явно («снято в vN.N, см. Историю») и гейтить grep по живым разделам; лучше — старые числа из ADR убирать, историю вести в arch-package «История».
3. **`settings_version` vs `graph_version`.** Не плодить дубль сущностей: `graph_version` — производная ревизии реестра, пин на прогон; синк-контракт ADR-006 §4 и reconciliation-тест сохраняются (правило 4).
4. **Каскад переименования узлов.** `generate_step` → `generate_step_sql` и новые узлы должны пройти по всем носителям разом (state-agent, c4-l3, sequence, S00, feature_list, строки сессий) — иначе рассинхрон имён, ровно то, за что бьёт C4.
5. **Тарифы.** Каталог плавает (на 18.11.2026 glm-5.3-prime уже 180/565 против 299/939 в расчёте) — не «обновлять» экономику: расчёт зафиксирован решением автора (D-017), иначе канон разъедется с §7 hw-01.
6. **S00 — канон.** Контракты стыка меняются только явной правкой S00 с фиксацией (правило сессий.md «изменение контракта = правка S00 + согласование»), не походя в задаче по диаграммам.

## Отчёт по сессии (заполняется по факту)

- Сделано:
  - **Task 1 — ADR-006 → v1.3:** переписан §4: реестр — единственный SoT («FK-рёбра вычитываются из каталога автоматически» снято — FK теперь генератор кандидатов в очередь владельцу); полный DDL-контракт ребра R1 (edge_id … ci_report — единственный DDL-источник для диаграмм и миграций); жизненный цикл `draft → CI → active` (CI по факту, вкл. измеренный fan-out против кардинальности), `deprecated + replaced_by`, вьюха `active`; `graph_version` = производная ревизии реестра, пин на прогон (state/кэш/аудит), синк по `settings_version` и reconciliation сохранены (правило 4 — дубля нет); `agent_edge_proposals`; bootstrap FK+майнинг + data steward; cold start/time-to-edge; SAP-оговорка с `MANDT` (TODO(confirm)); fan-out guard → ADR-004. Гейты: «вычитываются…» → 0/0, edge_id/mandatory_predicates/fanout_guard/graph_version/agent_edge_proposals — каждое ≥ 1
  - **Task 2 — ADR-007 → v1.2 + model-card v1.1 + RISKS v1.1:** политика M1 (D-016) заменила «каждый retry вверх» из v1.1; капы per-node ≤ 3 + ≤ 8 вызовов/≤ 90 s/≤ 2 ₽; резерв 0,6 ₽; экономика E1–E6 зафиксирована (узловая таблица, 0,61/0,62/0,67; базлайн 2,57 — сравнение корректное; ≈76%; 6,6К/0,9–2,6К; 1000 ≈ 670 ₽); старые 3,31/81%/«с кэшем 30%»/«декомпозированный 0,65» убраны; бенчмарки Spider 2.0/BIRD + LMArena (SWE-bench/HumanEval/MATH/BFCL сняты); pydantic temperature 0; breaker → деградация; M8 — вне базовой версии; 608 → 618 (снимок 18.11.2026); model-card — фиксация тарифов с датой + стек FastAPI/arq + stale «README §8» → hw-01 §6; RISKS — R-C1 76%, R-D3 cold start без FK-автопула (метрики доля вопросов с активным путём/time-to-edge), R-P3 без «план Б». Гейты: все 4 зелёные
  - **Task 3 — ADR-005 v1.2 + security v1.6:** матрица рубежей 1–5 = hw-01 §3.2 (AST-валидатор расширен: одна инструкция, `SET/COPY/CALL/DO` + функции deny-list, обход CTE/UNION/подзапросов/LATERAL, `LIMIT` к внешнему, диалект postgres; deny-list схем; ACL колонок соединения; БД — RLS + `SET LOCAL app.user_id`, раздельные коннекты планировщик/исполнитель, pgbouncer — только `SET LOCAL`); скелет в валидаторе (S1); аудит S8 вне state; отказ S9 без раскрытия существования; инъекции S6; кэш S7 (ключ `rights_hash + graph_version + assumptions`, кэшируется SQL); key-set кап 5000 → 50 000. Вариант D (RLS) переквалифицирован в составную часть рубежа 5. Гейты: 14/5, 3+2
  - **Task 4 — ADR-004 → v1.2:** T2 ведёт не в декомпозицию, а в async/matview/approximate/отказ с рекомендацией (кольцо T2 v1.1 снято вместе с кольцом); T1 — основной путь на боевом масштабе, заглушка — оговорка стенда («план Б» снят из шапки, §4, §5); промежуточный key-set без `LIMIT`, кап 50 000, `approximate`; лимиты A10 (`deadline 90 s / statement_timeout 30 s / max_sql_executions 4`, проверка остатка); semi-join/batch-fetch без внешних аналогий (ABAP/FAE убраны из заголовка, §1, §2); fan-out guard A3 (предагрегация в CTE). Гейты: 0/0/0, 3
  - **Task 5 — ADR-003 v2.1 + ADR-002 v1.2:** принцип моста (граф — производный снапшот реестра с `graph_version`; хранилище — заменяемая деталь за `find_join_paths()`), критерий переключения — BFS по adjacency (в т.ч. в C0/точке отката), ссылка на датированные факты AGE (живут в hw-01 §3.1); path-поиск — k взвешенных путей + ранжирование топ-5, при равных — clarify; `shortestPath` — имя функции; двухисточниковая формулировка «FK-рёбра из каталога PG + бизнес-рёбра» снята в §4 и в оси §2. ADR-002: recursion_limit — второй предохранитель (§2, §4, §5), карта решений — политика M1 («попытка 2 — тот же tier»). Гейты: 4+5, 6+4
  - **Task 6 — prd → v1.8:** FR-2 (k путей + ранжирование), FR-4 (валидатор: скелет/deny-list/ACL колонок/диалект/LIMIT), FR-6 (рубежи 1–5), FR-7 (реестр-SoT — двухисточниковая фраза снята), FR-8/FR-10 (M1 + recursion_limit), FR-11 (+edge_id/graph_version, аудит S8 отдельно), FR-12 (T2 → async, key-set 50 000/approximate, лимиты A10; «аналог FOR ALL ENTRIES» и «план Б» сняты), FR-13 (кэш через гейт S7), FR-14 (резерв 0,6 ₽); NFR-2/3/4/6/8; §6 — сцены + eval-критерии N4 (ACL-leak = 0 hard gate, CI-гейт −3 п.п., red-team-инъекции, time-to-edge); §7 — 670 ₽; §8 — дефолты Q1–Q4/Q6–Q7 с TODO(confirm), Q5 снят, снимок 618/18.11.2026, ≤ 6,6К токенов; §9 матрица обновлена. Гейты: 4, 11, 0
  - **Task 7 — диаграммы:** state-agent v1.3 (15 узлов = hw-01 §2.1: +build_skeleton, +compact_result, probe COUNT, `generate_step` → `generate_step_sql`; M1 на рёбрах; interrupt()/checkpointer TTL 10 мин; recursion_limit; T2 → degraded_out; overflow — кап 50 000), c4-l3 v1.4 (k путей + ранжирование, скелет от кода, компакция, M1, рубежи 1–5), sequence v1.3 (find_join_paths k путей, build_skeleton, M1-попытки, probe, compact_result, флаги усечения/approximate, кап 50 000, «0,65 ₽» → трейс), c4-l2 v1.8 (N1: agent-api FastAPI/uvicorn + 202+SSE + asyncio.timeout(90), agent-worker arq — Valkey Streams, PG StatefulSet + миграции Alembic Job, роли agent_planner/agent_ro/agent_trace/registry_writer, раздельные пулы, NetworkPolicy egress LLM-only, OTel fastapi/psycopg/httpx, аудит S8 отдельно), er v1.5 (DDL ребра R1 = ADR-006 §4.1, agent_edge_proposals, graph_version в трейсе, agent_audit S8, pydantic JoinPlan/QueryPlan/ResultCompact + состав AgentState N3), c4-l1 v1.2 (618 моделей, 6,6К — вне списка задач, найдено на приёмке). Гейты: 6 строк / 6 / 10
  - **Task 8 — свёртки и провода:** ARCHITECTURE (Overview — Python-стек FastAPI/arq/sqlglot/pydantic; constraint 4 = M1+recursion_limit+капы, «кольцо T2» снято; constraint 5 — стале-AGE → мост D-015; constraints 1–3/6 дополнены), observability v1.4 (OTel-инструментирование, аудит S8 отдельно, graph_version в трейсе), S00 (контракт 1: 15 узлов + graph_version/skeleton_hash/флаги + два уровня предохранителей + interrupt/checkpointer; контракт 2: M1; контракт 4: +agent_edge_proposals/agent_audit/registry_writer, graph_version-производная; контракт 5: ключ кэша S7; стек: N1 — целевой контур, D-005 MVP не снят; отчёт дополнен правками 2026-11-18), feature_list (behavior/description feat-004/008/009/010/011/014/015/016/017 — states не тронуты, проверка True), sessions.md (S07/S09/S10/S12/S14/S17 + примечание без «план Б»). `make check` EXIT=0, validate 100/100
  - **Task 9 — реестры и закрытие:** arch-package → v1.8 (шапка; «Состав пакета» — 15 строк версий/описаний; «История» — строка v1.8 с перечнем ID A/S/R/M/E/N/C + N1/N7, Q5 снят; заодно матрица «вопрос → документ» без FOR ALL ENTRIES и пример соглашения на ADR-006); adr/README, docs/README, diagrams/README — v1.8/новые версии; BL-002 закрыт (шапка + «Закрытие» + реестр); progress.md — S20 в Done/Next/Blockers/Files/Evidence
- Отклонения от плана:
  - Предусловие «grep "История ревизий" hw-01/README.md → 1» фактически даёт 0: блока нет в ужатом артефакте (авторская ужимка 1022a45). Суть предусловия (эталон ревью-03 на месте) подтверждена git log + содержанием; отклонение зафиксировано в Предусловиях и здесь
  - c4-l1-context.md не входил в задачи, но на приёмке в нём нашлись «608 моделей» и «~3К токенов» — поднят до v1.2 (618 моделей, 6,6К), чтобы критерий «в живом тексте docs/ нет 608» выполнялся
  - В ADR-004 внешняя аналогия (ABAP FOR ALL ENTRIES) убрана глубже, чем требовал Task 4 (заголовок, §1, §2) — по духу ревью-03 C5 и эталону hw-01
  - adr/README.md, docs/README.md, diagrams/README.md не были в списке задач — синхронизированы тем же заходом (правило «индексы тем же заходом»)
- Перенесено в другие сессии:
  - Имплементация interrupt()/checkpointer — S08/feat-008 (behavior уже обновлён); arq-воркер/agent-api (N1) — реализация не запланирована в S01–S19 (MVP — CLI, D-005): контур N1 — целевой, вернётся решением владельца после V6 (или новой сессией) — фиксировать при планировании
  - TODO(confirm) по Q1–Q4/Q6–Q7 (масштаб, SAP, отказ, кап) — вопрос куратору, не блокер канона
  - RLS-политики на конкретных таблицах — S02 (первая миграция по er v1.5) или feat-005

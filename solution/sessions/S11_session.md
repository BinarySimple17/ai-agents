# S11 — validate_sql: sqlglot AST + ACL + скелет + джойны по графу (веха V4, ДЗ-05)

Этап: Веха V4 (ДЗ-05: главный рубеж безопасности FSM) | Владелец: агент | Зависимости: S05, S07, S10 | Оценка: 1.5 | Статус: ◐ в работе
Фичи: feat-011 — validate_sql (AST + ACL); зависимости feat-005 (закрывается S05), feat-007 (passing), feat-010 (закрывается S10)
Роль по фиче: закрывает feat-011 — финальная сессия, через make verify-feature

## ⚠️ Прочитай перед началом

Исполнитель: ИИ-агент. К моменту старта: S05 закрыта (state.permissions — карта прав),
S07 закрыта (SchemaGraphStore: рёбра снапшота по rel_id/edge_id), S10 закрыта
(skeleton + skeleton_hash + JoinPlan в state), S04 закрыта (FSM-рёбра validate-ветки
существуют как заглушки). Эта сессия — «главный рубеж» (hw-01 §3.2, ADR-005 v1.2):
AST-валидатор на sqlglot (deny-by-default) между generate_sql и исполнением.
Три исхода: **ok** → check_cost (транзит) → execute; **галлюцинация** (таблица/связь/синтаксис
не существуют в мире агента) → diagnose_error (retry оправдан); **чужие данные**
(существует, но вне прав) → degrade БЕЗ retry (модель не должна «уговорить» валидатор).

Границы скоупа: БЕЗ EXPLAIN-порога (check_cost — транзитная заглушка S04; порог T2 —
feat-012, выпускная), БЕЗ инъекций RLS (рубеж 4 уже в S13), БЕЗ аудита S8 (query_trace —
S03/S21 контур наблюдаемости), БЕЗ кэш-гейта (SQL из кэша — S16, тот же валидатор).

### Правила для исполнителя

1. Ветка `feat-011-validate-sql` от `milestone/v4` (D-031); merge — в `milestone/v4`.
2. Deny-by-default (hw-01 §3.2): разрешено ровно то, что явно перечислено — один
   SELECT, известные таблицы/колонки из permissions, джойны ⊆ edge_id плана,
   функции из allow-list; всё новое (схема, функция, конструкция) — отказ. Списки —
   именованные константы с комментарием «почему» (AGENTS-принцип).
3. Обход вложенности обязателен: CTE (WITH), UNION/UNION ALL, подзапросы, LATERAL,
   производные таблицы — каждое тело проверяется теми же правилами; внешняя
   обёртка LIMIT — к внешнему запросу (behavior фичи).
4. Различение причин (ADR-005 §4, S9): «таблицы нет в подграфе разрешённых НИ ДЛЯ
   КОГО в мире агента» ≠ «таблица существует в permissions-мире, но не для этого
   пользователя»: первое — hallucination-вердикт, второе — deny-вердикт без retry.
   Классификация — код по permissions + каталогу таблиц данных (не текст ошибки).
5. Валидатор — чистая функция над (IR, skeleton_digest, join_plan, permissions,
   row_limit): без IO и LLM; размещение — `domain/` + тонкая обвязка сервиса
   (канон-прецедент validate_join_plan S09); sqlglot — инфраструктурная зависимость
   парсинга (в services), не в domain-модели. Ruling R4 ARCH-GATE: services парсят
   sqlglot → нейтральный IR (VO в domain) → domain решает вердикт над IR; обход
   sqlglot в services — whitelist типов узлов, fail-closed на неизвестном типе
   (вердикт hallucination).

## Предусловия

- [x] Сессия СТАРТУЕТ: статус в шапке переведён в «◐ в работе» и ARCH-GATE резолвлен ДО кода (canon-check architect ✅ либо обоснованный N/A) — старт-гейт (`guard-subagents.sh --phase start`, D-027) проверяет только стартованные (◐) сессии; «[ ] не начата» — план, гейт её скипает
- [ ] S05, S10 закрыты: `uv run pytest src/tests/integration/test_resolve_access.py src/tests/unit/test_generate_sql.py -q` → passed (feat-005, feat-010 passing)
- [ ] S07 закрыта: `uv run pytest src/tests/integration/test_join_graph.py -q` → passed (feat-007 passing)
- [ ] `make check` зелёный (baseline репозитория)
- [ ] feat-005, feat-007, feat-010 в state=passing; feat-011 активирована в state=active до старта работ

## Задачи

### Task 1: AST-структура — один SELECT без запрещённого

sqlglot (dialect postgres): ровно одна инструкция; корень — Select (не
Insert/Update/Delete/Merge/Ddl/Set/Copy/Call/Do); запрет операций SET/COPY/CALL/DO
и функций deny-list (dblink, pg_read_file, pg_sleep и пр. — константа); обход
CTE/UNION/подзапросов/LATERAL; внешний LIMIT — валидатор добавляет/клампит его
к ≤ ROW_LIMIT=1000 (ruling R1 ARCH-GATE: SQL от assemble_sql всегда без LIMIT —
S10 skeleton.py:157; отсутствие LIMIT — не вердикт-отказ); внутренние LIMIT
подзапросов/CTE — допустимы (кап канала — обёртка исполнителя S13).

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_validate_sql.py -q   # DML/DDL/SET/COPY/CALL/DO, вторая инструкция, запрещённая функция, LIMIT-кейс — все отклонены
```

---

### Task 2: Схемы и объекты — deny-list + allow-list колонок

Схемы deny-list: pg_catalog, information_schema, служебные схемы графа, agent_*
(настройки/трейс недоступны SQL агента — behavior фичи). Таблицы и колонки ∈
permissions (включая ACL колонок соединения: ключ ребра без доступа к колонкам —
джойн недоступен целиком, правило S09). Псевдонимы/aliases резолвятся к исходным
именам перед проверкой.

`SELECT *` / `t.*` — разворачиваются в разрешённые колонки таблицы из permissions
(ADR-005 §4 п.2 — не отклоняются, ruling R2 ARCH-GATE); разворот ПОДСТАВЛЯЕТСЯ в
текст SQL к исполнителю — литеральная звезда у БД вернула бы все колонки (ACL-байпас);
нет ни одной разрешённой колонки → deny-вердикт.

BL-014 (S05 Minor-9; триаж 2026-11-22): строгий `'*'`-интерпретатор колонок и любую
правку R5-семантики делать через единую fail-closed нормализацию прав (адаптер
PgPermissionStore — единственный источник), не заводить собственный CSV-парсер —
иначе три реализации (адаптер / plan_joins_service / демо) разъедутся. Запись
BL-014 закрыть/обновить здесь.

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_validate_sql.py -q   # pg_catalog/agent_* — отказ; таблица вне прав — deny; колонка вне прав — deny; алиас раскрыт
```

---

### Task 3: Скелет и джойны — skeleton_hash, mandatory_predicates, edge_id

Скелет не изменён: парсинг SQL → извлечение join-структуры → сверка со скелетом
плана (по skeleton_hash и структуре: таблицы/порядок/тип/направление джойнов);
mandatory_predicates на месте (в WHERE внешнего запроса или эквивалентно);
джойны ⊆ edge_id активных рёбер JoinPlan. Изменённый скелет = hallucination-вердикт
(модель переписала джойны) → diagnose_error.

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_validate_sql.py -q   # переставленный JOIN/выкинутый mandatory_predicate/лишняя таблица — отклонены с правильным классом вердикта
```

---

### Task 4: Инъекции 2-го порядка + wiring в FSM

Строковые литералы в VALUES/WHERE — остаются литералами (не парсятся как SQL,
не расширяют поверхность — S6 hw-01); комментарии не обходят проверки. Wiring:
наполнение заглушки validate_sql в FSM — три исхода ведут в check_cost-транзит →
execute / diagnose_error / degrade (рёбра S04); вердикт — в state.validation +
FLAG_* из orchestration/routing.py (единый источник имён). Adversarial-галерея —
в тест-файл. Property-based сверка с парсером (ADR-005 §4 п.9, ruling R3): для
корпуса множество колонок валидатора == независимому обходу sqlglot (встроенный
walk как оракул) — расхождение = дыра в whitelist'е узлов. Корпус §4 п.8 явно:
UNION с чужой таблицей, подзапросы, t.*, каталоги, window OVER(PARTITION BY …),
LATERAL, DISTINCT ON, GROUPING SETS.

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_validate_sql.py src/tests/unit/test_graph.py -q   # галерея зелёная; FSM-ветвление по трём вердиктам
grep -rn "EXPLAIN ANALYZE" src   # пусто (запрет S00)
```

## Критерии приёмки

- [ ] Запрос с DML/DDL, второй инструкцией, запрещённой схемой/таблицей/колонкой, джойном вне edge_id плана, изменённым скелетом или отсутствующим mandatory_predicates не выполняется (behavior feat-011)
- [ ] Несуществующее → retry-ветка (diagnose_error), вне прав → degrade без retry (behavior feat-011) — классификация кодом, не текстом ошибки
- [ ] Валидный SELECT проходит дальше с внешним LIMIT ≤ 1000 (добавлен/скламплен валидатором — ruling R1) (behavior feat-011)
- [ ] CTE/UNION/подзапросы/LATERAL проверяются теми же правилами (behavior feat-011)
- [ ] Валидатор — deny-by-default: тест «неизвестная конструкция отклоняется» (A-класс ADR-005 §5: adversarial ≥ поверхности парсера)
- [ ] Фича закрыта: `make verify-feature F=feat-011` → PASS, state=passing с evidence

## Ловушки

1. **sqlglot AUTO-фильтры/нормализация могут молча переписать SQL** — сравнивайте
   структурно (AST-поля), не str-сравнением; skeleton_hash по каноническому виду
   (урок Ловушки-4 S10).
2. UNION внутри CTE и подзапрос в LATERAL — классические места пропуска; тесты на
   вложенность обязательны (Task 1) — это ровно то, что ловит ревью-03 S2.
3. deny-вердикт не раскрывает существование: текст для degrade формулирует S15
   («данные премии недоступны»); здесь — только класс вердикта + факт, без имён
   недоступных таблиц в payload пользователю (в трейс — можно, это служебный канал).
4. EXPLAIN в валидаторе не выполняется вовсе; EXPLAIN (без ANALYZE) — это check_cost
   (S12/выпускная), не эта сессия. `EXPLAIN ANALYZE` запрещён везде (S00-греп).
5. Разрешение имён: unqualified колонка резолвится по таблицам запроса — если
   неоднозначно или вне прав любой кандидат → отказ (fail-closed, не «повезёт с дефолтом»).
6. Кэш (S16) пойдёт через этот же валидатор — не закладывайте «дружественный путь»
   для доверенного SQL (гейт един для всех, S7).

## Гейты субагентов (заполняется по факту, до закрытия сессии)

- ARCH-GATE: ✅ needs-decision → rulings R1–R4 до кода (canon-check architect 2026-11-22): **R1 (blocker)** — внешний LIMIT: SQL от assemble_sql всегда БЕЗ LIMIT (S10: domain/skeleton.py:157 «LIMIT добавляет validate_sql», assemble_sql LIMIT не пишет) — строка плана «отсутствие → verdict требования» ложная посылка, выжгла бы happy-path на каждом запросе; валидатор ДОБАВЛЯЕТ/КЛАМПИТ внешний LIMIT ≤ ROW_LIMIT=1000 (канонная константа порта SQLExecutor, передаётся параметром в domain-функцию — domain не импортирует application); присутствующий ≤ 1000 сохраняется, > 1000 клампится, внутренние LIMIT подзапросов/CTE допустимы (кап канала — обёртка исполнителя S13); Task 1 плана исправлен. **R2** — `SELECT *`/`t.*` разворачиваются в разрешённые колонки таблицы из permissions (ADR-005 §4 п.2 — не отклоняются); разворот ПОДСТАВЛЯЕТСЯ в текст SQL к исполнителю (литеральная звезда у БД вернула бы все колонки — ACL-байпас); ни одной разрешённой колонки → deny; добавлено в Task 2. **R3** — property-based сверка с парсером (ADR-005 §4 п.9) в сессии: для корпуса множество колонок валидатора == независимому обходу sqlglot (встроенный walk как оракул — не общий с валидатором код); без новой зависимости (корпус = adversarial-галерея + генерируемые комбинации); добавлено в Task 4. **R4** — пин контракта service↔domain: services парсят sqlglot → нейтральный IR (VO в domain) → domain-функция решает вердикт над IR; обход sqlglot в services — whitelist типов узлов, fail-closed на неизвестном типе (вердикт hallucination); правило 5 плана закреплено. Notes приняты: вердикт — в state.validation + FLAG_* (orchestration/routing.py — единый источник имён); adversarial-корпус ADR-005 §4 п.8 перечислен явно в Task 4. Set aside: размещение валидатора — прецедент S09 канонен; скоуп/FSM-рёбра state-agent v1.7/BL-014 — канонны.
- REVIEW-GATE: ⏳ не завершён — волны 1–3 пройдены по полному циклу (диапазон 7abefc5..125a4d1): ревью-1 «No» (2C/4I/5M, байты на живом PostgreSQL) → fix-волна bf518b7 → re-review-2 «With fixes» → fix-волна-2 1ab30f7 → re-review-3 «With fixes» → fix-волна-3 125a4d1; все Critical/Important закрыты кусающими регрессионными тестами (21 кейс, красные до фиксов); re-review-4 (диапазон bf518b7..125a4d1) прерван лимитом API-ключа 100,40/100,00 ₽ — вердикт не получен, verify-feature/merge/close-session — продолжение сессии после ресета лимита (прецедент S05, D-032-пакет владельца не тронут)

## Отчёт по сессии (заполняется по факту)

- Сделано:
  - **Task 1 — форма AST (62a2b11)**: `domain/sql_validation.py` (VO IR: SqlAudit/SqlColumn/SqlJoin; чистый вердикт ok/hallucination/denied + enforced_limit; ALLOWED_FUNCTIONS deny-by-default) + `application/services/validate_sql_service.py` (парсер postgres, ровно одна инструкция, корень SELECT/set-операция, whitelist узлов exact-match fail-closed, обход CTE/UNION/подзапросы/EXISTS/LATERAL — каждый скоуп теми же правилами, резолюция алиасов по скоуп-цепочке с границей корреляции на CTE, `_enforce_limit` R1) + публичный алиас `normalize_sql_fragment` в domain/skeleton.py (единый источник нормализации R3 S10); 34 passed + 33 skip (Task 2/3 закоммичены skip-марками как зафиксированный контракт — директива владельца «после task 1 остановиться»).
  - **Merge milestone/v4 (10cb731)**: S28 harness/context-hygiene (make new-session/close-session, docs/AGENT-OPERATIONS.md) по команде владельца.
  - **Task 2 — ACL (a62b882, ruling R2 + BL-014)**: `_check_tables` (схема ≠ public / agent_* → denied; ∉ каталога мира → hallucination «не существует»; ∈ каталога ∉ permissions → denied «вне прав»; каталог — SoT существования), `_check_columns` (Ловушка 5: кандидаты-таблицы мира решают; ≥2 разрешённых → «квалифицируйте» hallucination, 1 → resolved, 0 → denied), `_check_stars` + `star_expansion_columns` (звезда не отклоняется — разворачивается; '*'-perms → суб-звезда, явный список → подстановка, пустой → deny), `_check_permissions_contract` (str-value → ValueError — CSV-парсер не заводится); IR — SqlSource(name, kind); сервис `_expand_stars` (подстановка в AST при ok — общий `_projection_stars` с IR); 23 skip-марки сняты.
  - **Task 3 — скелет (71506a2)**: `_check_skeleton` — skeleton_hash state↔VO (расхождение = подмена чекпоинта, ранний выход), FROM-источник, структура джойнов = рёбра JoinPlan (тип/таблица/пары ON в РЕАЛЬНЫХ именах — алиасы резолвятся сервисом в `_collect_where` и ON-парах; пары отсортированы), mandatory_predicates ⊆ WHERE (нормализованное вхождение — единый источник S10); корневые CTE/UNION/LATERAL = изменённый скелет, вложенные тела — те же правила; находка sqlglot 30.31: And/Or — наследники Func → `_STRUCTURAL_FUNCS` расширен (иначе WHERE с AND — фантом «функция and вне allow-list»); skip сняты, TestSkeletonIntegrity зелёный (фикстуры изолированы от ACL — лишний JOIN на разрешённой таблице).
  - **Task 4 — wiring FSM (ea6d11e)**: порт `PermissionStore.data_catalog()` (SoT agent_data_sources, BL-009; PgPermissionStore impl, fail-closed RightsUnavailableError) + `make_validate_sql_node` (детерминированный БЕЗ LLM; флаги parity с routing — тест; state.validation {outcome, issues, enforced_limit}; sql ПЕРЕЗАПИСЫВАЕТСЯ исполненным текстом (R1 LIMIT + R2 звёзды); hallucination — message из issues для ретрая M1, denied — фиксированный текст без имён таблиц (S9); fail-closed дефекты каркаса/стыка → denied-маршрут degrade без retry, деталь оператору validate_sql.unavailable); container node_overrides (catalog_provider на каждый запрос) + EVENT_VALIDATE_SQL_VERDICT/UNAVAILABLE (порт Tracer + observability-каталог) + карта владения AgentState (sql/validation) + S00 стык 3 синк; adversarial-галерея §4 п.8 (TestAdversarialGallery: UNION чужая таблица/каталоги/window OVER/LATERAL/DISTINCT ON/GROUPING SETS/t.*/инъекции-литералы/комментарии) + property-сверка R3 (TestPropertyOracleColumns: независимый sqlglot.walk-оракул — расхождение множеств колонок = дыра whitelist'а; 14 кейсов); 15 юнит-тестов узла (test_validate_sql_node: маршруты трёх исходов через routing.route_validate_sql, fail-closed-матрица, parity, трейс без SQL-текста, каталог на каждый запрос); FSM happy-path фикстуры интеграций скриптовых генераторов консистентны скелету.
  - **Fix-волна ревью (bf518b7)** — вердикт волны-1 «No» (2C/4I/5M, байты ревью на живом PostgreSQL): **C1** row-expansion — звезда в выражении проекции (``(t.*)``/``t.* AS x``/``(t.*)::text``) — форм-отказ (``_reject_expression_stars``: Star вне Count в любом поддереве проекции → SqlAuditError), whole-row-ссылка (``SELECT orders``) — ``SqlColumn.whole_row`` с требованием '*' на КАЖДОЙ таблице-кандидате (утечка закрыта: прежний код резолвил whole-row как «колонку» через '*'-соседа); **C2** лексическая видимость CTE (``_visible_ctes`` по позиции каждой ссылки: предыдущие элементы объемлющего WITH + внешние; эмпирика sqlglot 30.31: ключ args — ``with_``) — CTE-тень не прячет реальную таблицу от ACL (сцена «WITH orders AS…» над bonuses давала ok при denied-контроле), FROM→CTE не «FROM не изменён»; **I1** пользовательский текст hallucination фиксированный (имена таблиц/инфраструктуры только во внутреннем validation.issues — контекст ретрая подключит S14); **I2** табличная функция в FROM → форм-отказ (не ValidationError VO); **I3** ORDER BY по output-алиасу — не колонка БД (GROUP BY остался fail-closed — приоритет input в PG); **I4/M4** exp.Interval в whitelist + квантор 'all' в allow-list (sqlglot парсит как Anonymous); **M2/M3/M5** — звезда в тесте узла (кусает по-настоящему), docstring-и «реализовано», issues_count в трейсе (+observability-каталог синх); 13 регрессионных кейсов — ПАДАЛИ до фикса (байты подтверждены прогоном), make check 756 passed
  - **Fix-волна-2 (1ab30f7)** — re-review-2 «With fixes»: **Critical (регрессия I3-фикса bf518b7)** — unqualified имя ВНУТРИ выражения ORDER BY PostgreSQL резолвит в input-колонку (ревью проверило на живой БД: «ORDER BY country + 1» берёт customers.country), а пропуск bf518b7 applied ко всему поддереву order-арга → непозволенная колонка утекала через порядок строк, путь fill → assemble_sql → validate открыт; фикс — пропуск только НЕПОСРЕДСТВЕННО упорядоченного голого unqualified Column (``Ordered.this`` ∈ output_aliases), выражения проходят ACL; **Important** — correlated whole-row: unqualified имя, не совпавшее с локальными refs, но совпавшее с ref объемлющего скоупа (``SELECT (SELECT orders FROM customers LIMIT 1) …``) — whole_row с внешним кандидатом ('*' обязателен); **Minor** — сканер звёзд не пересекает границу Select/Subquery; мёртвое поле SqlAudit.cte_names удалено; по пути починен порядок проверок Count/other-scope в сканере (регрессия прямого count(*) — поймана тестами). 3 регрессионных кейса — красные на bf518b7 (байт подтверждён прогоном), make check 759 passed.
  - **Fix-волна-3 (125a4d1)** — re-review-3 «With fixes»: **Critical (остаток I3-пропуска)** — PG сопоставляет output-алиас С УЧЁТОМ РЕГИСТРА (quoted «Country» ≠ unquoted Country → берётся input-колонка customers.country вне прав; проверено ревью на живой БД), а валидатор сравнивал оба имени в нижнем регистре — пропускал ACL (исполняемый байпас fill → assemble → validate); фикс — сопоставление по ЭФФЕКТИВНЫМ именам (quoted Identifier — как есть, unquoted — lower); **Important** — correlated whole-row fallback ЗАМЕНЯЛ локальных кандидатов внешним ref: при коллизии PG читает ЛОКАЛЬНУЮ колонку, schema-blind валидатор обязан требовать '*' на КАЖДОЙ таблице-кандидате — fallback ДОПОЛНЯЕТ (дедуп), не заменяет; **Minor** — позитивный кейс '*' на обеих. 5 тестов (2 красных на 1ab30f7 — байт подтверждён прогоном), make check 764 passed.
  - **Итог**: make check 764 passed (EXIT=0); ruff чистый; grep «EXPLAIN ANALYZE» пуст (S00-запрет); verify-feature → см. evidence в feature_list.json.
- Отклонения от плана:
  - Задача предлагала «джойны ⊆ edge_id» — сверка рёбрами плана реализована сравнением SqlJoin-кортежей с `_expected_joins` (тип/таблица/пары ON/порядок) — эквивалентно, но строже: ловит и перестановку, и смену типа/ключей (§ задачи «сверка со скелетом по skeleton_hash и структуре: таблицы/порядок/тип/направление»).
  - Интеграционные тесты FSM (test_container_wiring/test_clarify_resume/test_resolve_access) — скриптовый SQL генераторов заменён с однотабличного на скелет-консистентный JOIN: после включения реального валидатора однотабличный SELECT честно дал бы изменённый скелет (hallucination-маршрут вместо done).
  - UNION-ветки корня: содержимое классифицируется до структурного вердикта (структура = hallucination), как в плане; галерейный кейс «закрытая строка + ; DROP TABLE» — форм-отказ (вторая инструкция), не «литерал съел инъекцию» — уточнена формулировка теста.
  - В фикс-коммит bf518b7 через ``git add -A`` попала посторонняя запись беклога ``solution/backlog/BL-018-production-frontend-spa.md`` (решение владельца D-032 о продакшен-фронтенде после ДЗ-06; появилась в worktree в ходе сессии, к feat-011 отношения не имеет; содержание цело). История не переписывается (pushed) — атомарность зафиксирована здесь; последующие коммиты — строго в скоупе.
- Перенесено в другие сессии:
  - BL-014 — остаток (unify lenient-CSV-парсеров plan_joins_service/демо с адаптером) — запись обновлена, остаётся открытой вне S11.
  - S14 — consume issues из state.validation при ретрае diagnose_error (контекст последней ошибки уже пишет узел).
  - S13 — внутренние LIMIT подзапросов не клампятся валидатором (кап канала — обёртка исполнителя, ruling R1) — там и сверить.


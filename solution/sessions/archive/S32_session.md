# S32 — fan-out guard: валидатор не пускает двойной счёт (feat-023)

Этап: Веха V4 (ДЗ-05) · долг аудита, окно 01–07.12.2026 | Владелец: агент | Зависимости: S31 | Оценка: 1 | Статус: ✅ 2026-11-27
Фичи: feat-023 — Fan-out guard: защита от двойного счёта (validate_sql); их dependencies уже в passing
Роль по фиче: закрывает feat-023 — финальная сессия, через make verify-feature

## ⚠️ Прочитай перед началом

Исполнитель: ИИ-агент; куратор: пользователь. Предмет — аудит 2026-11-23, E3a [Высокая]:

- `fanout_guard` хранится в реестре (`agent_relationships.fanout_guard`; `true` у `e_orders_items`
  и `e_products_items` — миграция 0003), читается адаптером в `EdgeSemantics`
  (`neo4j_graph_store.py:357`), но ни один узел его не использует. В промпт ранжирования уходит
  только `cardinality` (`plan_joins_service.py:548`).
- Канон принял инвариант без реализации: ADR-006 §4.6 и ADR-004 — «агрегация поверх fan-out ребра
  без предагрегации в CTE не пропускается (инвариант `plan_joins`/`validate_sql`)»; hw-01 §3.3;
  prd §6 — «0 случаев двойного счёта». ADR назвал двойной счёт «самым частым классом тихих ошибок
  Text-to-SQL».

**Нюанс, проверенный по коду (учесть на ARCH-GATE).** Корневая структура FROM/JOIN фиксирована
скелетом (`_check_skeleton`: CTE/UNION в корне = изменённый скелет), модель наполняет только
SELECT / WHERE / GROUP BY / ORDER BY. Значит «предагрегация в CTE» в корне для модели невыразима.
Выразимые средства: `GROUP BY` по ключу стороны «1», агрегация в подзапросе проекции (`Subquery`,
`Select` — в whitelist узлов), `COUNT(DISTINCT …)`. Отказ guard'а обязан предлагать выразимое
исправление, иначе при temperature 0 модель повторит тот же SQL до fuse.

Границы скоупа: БЕЗ декомпозиции (feat-017, выпускная), БЕЗ предранжирования планов кодом по
fan-out, БЕЗ eval-сета агрегатов (BL-025).

### Правила для исполнителя

1. Ветка `feat-023-fanout-guard` от `inter/hw05-hw06`; merge — в `inter/hw05-hw06` (трек пред ДЗ-06; `milestone/v4` — неприкосновенная точка сдачи ДЗ-05; поправка S41/п.1(в), D-042 — было «от `milestone/v4`, merge — в `milestone/v4` (D-031)»).
2. Разделение как в S11 (ruling R4): детекция — в services (извлечение в IR), политика — в
   `domain/sql_validation.py`. `SqlAudit` расширяется аддитивно: агрегаты корневого скоупа (функция,
   колонка, таблица-кандидат) и колонки `GROUP BY` корневого скоупа.
3. Правило v1 (минимальное звуковое; уточнить на ARCH-GATE). Запрос отклоняется, если в корневом
   скоупе есть чувствительный агрегат (`sum`, `avg`, `count` без `distinct`) над колонкой стороны
   «1» ребра плана с `fanout_guard = true` и в `GROUP BY` нет ключа джойна стороны «1». Проходят:
   `min`/`max`/`count(distinct …)`; агрегаты только по колонкам стороны «n»; агрегат целиком внутри
   подзапроса; план без fan-out рёбер.
4. Исход — `hallucination` (retry оправдан) с детерминированной подсказкой, в которой только
   выразимые варианты и нет имён недоступных таблиц (S9).
5. `fanout_guard` приходит тем же каналом, что `mandatory_predicates`: `path_semantics` →
   `JoinPlan`/`Skeleton` (аддитивное поле, дефолт `False` — старые чекпойнты валидируются); в Neo4j
   не дублируется (ADR-003).
6. Планёр: в payload ранжирования рядом с `cardinality` передаётся `fanout_guard` ребра.
7. Фича feat-023 активируется (`not_started → active`) в первый же день сессии и закрывается через
   `make verify-feature F=feat-023`.

## Предусловия

- [x] S31 закрыта (общие файлы валидатора — порядок исполнения): `grep -n "| S31 |" solution/sessions.md` → ✅ (строка :72, «✅ 2026-11-25», файл в archive/)
- [x] `make check` зелёный (baseline репозитория) — EXIT=0 до переворота шапки (гейт скипал запланированную сессию)
- [x] feat-011 и feat-007 в state=passing; feat-023 переведена в `active` на старте (первое действие сессии, правило 7)
- [x] Инфра поднята и актуальна: `make infra-up && make infra-migrate && make sync-graph && make index-knowledge` → exit 0
- [x] Сессия СТАРТУЕТ: статус в шапке переведён в «◐ в работе» и ARCH-GATE резолвлен ДО кода (canon-check architect ✅ либо обоснованный N/A) — старт-гейт (`guard-subagents.sh --phase start`, D-027) проверяет только стартованные (◐) сессии; «[ ] не начата» — план, гейт её скипает

## Задачи

### Task 1: IR — агрегаты и `GROUP BY` корневого скоупа (services)

`validate_sql_service.py`: в `SqlAudit` добавляются `aggregates` и `group_columns` (имена с
квалификаторами, переписанными на реальные имена источников — та же резолюция, что у
`where_conjuncts`). Property-проверка против парсера на корпусе из 15–20 SQL: агрегаты корневого
скоупа совпадают с ручной разметкой; агрегаты подзапросов в корневой набор не попадают.

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_validate_sql.py -q -k "audit"   # новый корпус зелёный; прежние тесты без регрессий
```

### Task 2: Политика fan-out (domain)

`validate_sql` получает fan-out рёбра плана и применяет правило v1; нарушение — строка в
`issues_hallucination` с подсказкой («агрегация по заказам через позиции размножает строки:
сгруппируй по ключу заказа или посчитай в подзапросе»). Тесты — новый
`src/tests/unit/test_fanout_guard.py`, по одному на каждый пункт `behavior` feat-023.

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_fanout_guard.py -q   # отклонено: SUM/AVG/COUNT по стороне «1»; прошло: GROUP BY по ключу, COUNT(DISTINCT), MIN/MAX, сторона «n», подзапрос, план без fan-out
```

### Task 3: Проводка

`JoinPlan`/`Skeleton` несут `fanout_guard` рёбер; `validate_sql_node` передаёт их в
`service.validate`; ранжирование — `fanout_guard` в payload. Проверить живой resume после clarify:
у `ValkeyCheckpointer` явный allowlist msgpack-типов (S27), новое поле VO не должно дать «сырой
dict» на load.

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_validate_sql_node.py src/tests/unit/test_graph.py src/tests/unit/test_plan_joins.py src/tests/unit/test_valkey_checkpointer.py -q
printf "по ключу заказа\n" | uv run --env-file .env python -m apps.cli "Статистика" --fsm --token dev-analyst-token   # interrupt → resume того же прогона
```

### Task 4: Документы (тот же коммит, что и код)

ADR-006 §4.6 (пометка «реализовано: `validate_sql`, feat-023»), ADR-004 (строка про fan-out),
`prd.md` FR-12, RISKS (риск «двойной счёт» с митигацией), `docs/quality-document.md`. Рёбра FSM не
меняются — `state-agent.md` без новой версии.

**✅ Самопроверка:** `git grep -n "fanout_guard" -- docs/adr docs/prd.md docs/RISKS.md` — в каждом файле есть ссылка на реализацию.

### Task 5: Слой 3 — живой прогон (LM Studio)

Вопрос, где модель пишет `SUM(orders.total)` при джойне с `order_items` (например: «Какова общая
сумма заказов с их позициями?»). Ожидание: `validate_sql` отклоняет → retry с подсказкой →
корректный SQL → `done`. Трейс и команду — в отчёт.

**✅ Самопроверка:**

```bash
uv run --env-file .env python -m apps.cli "<вопрос>" --fsm --token dev-analyst-token 2> /tmp/s32.err; grep -c "validate_sql: исход hallucination" /tmp/s32.err   # ≥ 1; далее retry и done либо честный отказ
```

## Критерии приёмки

- [x] `behavior` feat-023 выполняется построчно — тест на каждый пункт (test_fanout_guard.py 32 + TestAuditAggregates 15 + пины в test_plan_joins/test_valkey_checkpointer)
- [x] `make verify-feature F=feat-023` → PASS, evidence записан в `feature_list.json`
- [x] FSM-путь без регрессий: `test_validate_sql.py`, `test_validate_sql_node.py`, `test_graph.py` зелёные (make check 905 passed); живой resume после clarify работает (прогон 2: interrupt → resume с piped-ответом → plan→generate→validate→execute)
- [x] Живой прогон: двойной счёт не исполнен, retry с подсказкой виден в трейсе (validate_sql: исход hallucination, 1 замечание → diagnose → retry → ok; gv 397, оба guard-ребра в плане; done не достигнут — предсуществующий Decimal-баг interpret, BL-059, вне скоупа)
- [x] ADR-006, ADR-004, prd, RISKS синхронны с кодом (v1.10/v1.4/v1.10/R-Q6 — в том же коммите; self-check grep выполнен)
- [x] `make check` EXIT=0 (905 passed после волны и каждой fix-волны); вердикт ARCH-GATE записан до кода; CI на milestone/** только (D-035) — рабочая ветка CI не триггерит, локальный make check — рубеж

## Ловушки

1. **Подсказка предлагает CTE, которого модель написать не может** (корень фиксирован скелетом) —
   три ретрая впустую и fuse. Только выразимые варианты.
2. **Ложное срабатывание хуже пропуска для доверия**: `GROUP BY` по ключу стороны «1» (сумма по
   заказу) — легальный паттерн и обязан проходить; проверить на few-shot из `docs/few_shot.md`.
3. **`COUNT(*)` без колонки под агрегатом** при джойне orders → order_items считает строки позиций
   и корректен: правило v1 его не отклоняет (нет колонки стороны «1» под агрегатом) — осознанно.
4. **Старые чекпойнты** в Valkey с планами без нового поля: дефолт `False` и валидация на load.
5. **`verify-feature` требует `state=active`**: забыли активировать на старте — `BLOCKED` на закрытии.
6. **Модель повторяет тот же SQL при temp 0**, если подсказка не даёт выхода, — подсказка
   детерминированная и конкретная; если живой прогон показывает три одинаковые регенерации — это
   дефект подсказки, а не модели.

## Гейты субагентов (заполняется по факту, до закрытия сессии)

- ARCH-GATE: ✅ compliant (canon-check architect до кода, 2026-11-27; план/файл: S32_session.md + src/domain/sql_validation.py, join_plan.py, services/validate_sql_service.py, plan_joins_service.py). Вердикт: слой — ровно ruling R4 S11/S42 (детекция services → IR SqlAudit.aggregates/group_columns → политика domain._check_fanout; guard через JoinPlan, уже в сигнатуре validate_sql). Rulings: **R1** — разделение слоёв compliant, прецедент has_into. **R2** — правило v1 уточнено до строгого v1′ (предложено исполнителем, подтверждено архитектором): правило 3 плана в буквальной записи пропускает двойной счёт — SUM(orders.total) остаётся ×N_i даже с GROUP BY по ключу стороны «1» (в группе N_i копий total; ключ коллапсирует группу, не убирает копии); принято: SUM и COUNT-без-distinct над колонкой стороны «1» guard-ребра → отклоняются ВСЕГДА; AVG стороны «1» → отклоняется только без ключа джойна стороны «1» (все колонки составного ключа) в GROUP BY; MIN/MAX/COUNT(DISTINCT)/агрегаты стороны «n»/агрегат в подзапросе/план без guard — проходят; COUNT(*) вне правила (ловушка 3); подсказки детерминированные, только выразимое (подзапрос-предагрегация; для зерна «на заказ» — колонка без агрегации); требование к Task 1: оконные (SUM OVER) и HAVING-агрегаты входят в IR, колонки под агрегатом рекурсивно (CASE — few-shot :30), корпус с SUM OVER/HAVING/CASE/DISTINCT. **R3** — кодирование: JoinPlanEdge аддитивно +{fanout_guard: bool = False, fanout_one_side: str = ""} (bare-имя стороны «1», непусто при guard=True); _build_plan выводит из cardinality ('1:n'→left, 'n:1'→right, 'n:m'/'1:1'+guard → ValueError fail-closed, прецедент BL-050(c)); инвариант guard→one_side ∈ {from,to} — в validate_join_plan (план-время, defense-in-depth по аналогии skeleton_hash); Skeleton не трогается (guard не рендерится в SQL, носитель один — «/Skeleton» из правила 5 вычеркнуто); чекпойнты: bool+str — базовые типы msgpack, allowlist S27 не расширяется, дефолты валидируют старые чекпойнты. **R4** — group_columns = tuple[SqlColumn, ...] с резолюцией как у columns; позиционные/выражения GROUP BY не собираются → консервативный reject (выразимый фикс — «группируй по ключу явно», ловушка 1); мэтчинг ключа для AVG: имя == ключевой колонке И one_side ∈ кандидаты; lenient-мэтч не открывает обход (SUM безусловен, неоднозначность уронит PG). **R5** — исход hallucination (ошибка содержания SQL, не прав — ADR-005 §4, прецедент _check_skeleton «не вне прав, а выдумка»); ранжирование — аддитивный ключ «fanout_guard» рядом с cardinality, предранжирование кодом не появляется; residual v1 (агрегаты в подзапросах проверяются только на корневом скоупе-потребителе, guard=false-рёбра — ответственность владельца, позиционные/выражения GROUP BY) — пин в RISKS + pin-тесты. Findings: [warning] правило 3 плана несостоятельно буквально — закрыт R2; [warning] сид-ребро e_campaigns_orders cardinality 'n:1' при left=campaigns против конвенции left-is-«1» остальных рёбер (вне blast-radius: guard=false) → BL в backlog + конвенция вывода one_side в правке ADR-006 §4.6 (Task 4); [note] exp.Window в whitelist — SUM(...) OVER () обязан попадать в IR (R2-требование к Task 1, adversarial-кейс); [note] правка ADR-006 §4.6 согласует канон с выразимым (подзапрос/GROUP BY), не повторяет «CTE» (невыразимо в корне — скелет). Находки закрыты/решены rulings ДО кода — D-027 удовлетворён.
- REVIEW-GATE: ✅ Ready to merge: Yes — цепочка трёх ревью и трёх fix-волн по диапазону bf4aec1..4fe09d4 (HEAD кода; закрывающий коммит — журналы): ревью-1 bf4aec1..222fbdb «With fixes» 0C/2I/6M (bite тестов подтверждён ревьюером независимо копией пакета с удалённым _check_fanout: 15 failed/9 passed) → fix-волна f248573 (I-1 подсказка всех колонок составного ключа, I-2 честный residual+pin+BL-060, M-3..M-6 кодом, M-7/M-8 park) → re-review-2 222fbdb..f248573 «With fixes» 0C/0I/3M (все 2I/4M подтверждены закрытыми; M-6 не открыла обходов — закрыла латентный молчаливый no-op) → fix-волна-2 1f2fc90 (M(re)-1 честность DISTINCT-формулировок + пин-пара, M(re)-2 симметрия нормализации обеих сторон, M(re)-3 residual-указатели в ADR-006) → re-review-3 f248573..1f2fc90 «With fixes» 0C/1I/3M с ЯВНОЙ санкцией закрывающего коммита («правка одной строки… достаточно включить её в закрывающий коммит S32, и цепочка закрывается Yes») → fix-волна-3 4fe09d4 (I(re3)-1 синк строки реестра беклога, M(re3)-2 условие «кратность снимается» [GROUP BY по ключу — снимает; без — недосчёт], M(re3)-3 юнит-пин нормализации обеих сторон +2 пина AVG/SUM DISTINCT) — правки ровно в предписанном объёме, нового кода нет, отдельный цикл re-review не заводился (санкция вердикта re-review-3). make check EXIT=0 после каждой волны (844→897→900→902→905 passed).

## Отчёт по сессии (заполняется по факту)

- Сделано:
  - **Старт/предусловия**: по команде владельца «стартуй s32». Pre-start ветка: feat-validator-deny-into (S42) влита в `inter/hw05-hw06` fast-forward'ом (7ef7b4d→bf4aec1, merge напрямую по D-042 — ветка отревьюена «Ready to merge: Yes»); рабочая ветка `feat-023-fanout-guard` от bf4aec1. Предусловия 5/5: S31 ✅; baseline `make check` EXIT=0 (844 passed, сессия ещё ⬜ — гейт скипал); feat-011/007 passing; инфра up/migrate/sync-graph/index-knowledge exit 0; feat-023 → active (правило 7), шапка ◐ до кода.
  - **ARCH-GATE до кода**: canon-check architect → ✅ compliant, rulings R1–R5 (записаны в файле сессии). Ключевой R2 — правило 3 плана в буквальной записи пропускало двойной счёт (SUM стороны «1» ×N_i даже с GROUP BY по ключу); принят строгий v1′ (SUM/COUNT-без-distinct — всегда; AVG — без ключа в GROUP BY; предложено исполнителем, подтверждено архитектором). Finding 2 (сид-ребро e_campaigns_orders 'n:1' против конвенции) → BL-058.
  - **Task 1 (IR, services)**: `SqlAudit` аддитивно +aggregates/+group_columns, VO `SqlAggregate(func, columns, distinct)`; `_collect_aggregates` (корневой скоуп: проекция/HAVING/ORDER BY, оконные SUM(…) OVER — узел агрегата под exp.Window доходит сам; колонки под CASE рекурсивно; DISTINCT — `this=exp.Distinct`, sqlglot 30.x, arg «distinct» пуст — проверено прогоном; подзапросные/CTE-агрегаты исключены) + `_collect_group_columns` (голые колонки; позиционные/выражения — консервативный reject, R4) + `_column_ir` (общая резолюция с `_collect_aggregates`). Корпус TestAuditAggregates 15 — самопроверка `pytest -k "audit"` зелёная.
  - **Task 2 (политика, domain)**: `_check_fanout` + `_fanout_edge_key` + `_fanout_sensitivity` + `_group_key_present`; битый инвариант ребра — fail-closed. Тест `test_fanout_guard.py` 24 (behavior feat-023 построчно) — самопроверка зелёная. Bite: stash 4 source-файлов → 15 failed/9 passed на до-правочном коде.
  - **Task 3 (проводка)**: `JoinPlanEdge` +{fanout_guard, fanout_one_side} (ruling R3); `validate_join_plan` + инвариант guard→one_side ∈ {from,to}; `_fanout_one_side` из cardinality (n:m/1:1+guard → ValueError, прецедент BL-050(c)); `_build_plan` заполняет (развернутое направление — тест); payload ранжирования +fanout_guard (правило 6); Skeleton не тронут (R5a); чекпойнтер-регрессия (guard-поля roundtrip + legacy дефолтами — ловушка 4); `validate_sql_node` без правок (join_plan уже в сигнатуре).
  - **Task 4 (docs, тот же коммит)**: ADR-006 v1.10 (§4.6 «Реализовано» + v1′ + конвенция one_side из cardinality), ADR-004 v1.4 (строка лимитов — выразимые средства вместо «CTE»), prd v1.10 (FR-4 + guard, матрица), RISKS R-Q6 (двойной счёт + residual пины), quality-document (domain/ + plan_joins строки), BL-058 (файл+реестр). Self-check `git grep fanout_guard docs/...` — все файлы ссылаются на реализацию.
  - **Task 5 (живой прогон, LM Studio qwen3.8-9b, gv 397)**: план orders→order_items/products (оба guard-ребра); «Покажи каждый заказ с его позициями и суммой заказа»: `validate_sql: исход hallucination` (1 замечание) → diagnose → generate retry → `validate_sql ok` → execute (syntax-fail → diagnose → эскалация M1 → ok) → compact_result → interpret_result. Критерий Task 5 («двойной счёт не исполнен, retry с подсказкой виден в трейсе») выполнен; self-check-команда `grep -c "validate_sql: исход hallucination"` = 1.
  - **Fix-волны по ревью**: f248573 (ревью-1: I-1 подсказка всех колонок составного ключа — dead-end retry; I-2 честный residual + pin вложенного двойного счёта + BL-060; M-3..M-6 кодом; M-7/M-8 park) → 1f2fc90 (re-review-2: M(re)-1 честность DISTINCT-формулировок + пин-пара, M(re)-2 симметрия нормализации обеих сторон, M(re)-3 residual-указатели в ADR-006 §4.6+статус) → 4fe09d4 (re-review-3: I(re3)-1 синк строки реестра беклога, M(re3)-2 условие «кратность снимается», M(re3)-3 юнит-пин нормализации; +2 пина AVG/SUM DISTINCT). Bite fix-волн: stash домена → 2 failed→27 passed; откат нормализации ловится юнит-пином.
  - **REVIEW-GATE** (цепочка трёх ревью и трёх fix-волн, вердикт — в гейте ниже): ревью-1 bf4aec1..222fbdb «With fixes» 0C/2I/6M → fix-волна f248573 → re-review-2 «With fixes» 0C/0I/3M → fix-волна-2 1f2fc90 → re-review-3 «With fixes» 0C/1I/3M с санкцией закрывающего коммита → fix-волна-3 4fe09d4 (синк реестра беклога, условие residual DISTINCT, юнит-пин нормализации) — правки в предписанном объёме, отдельный re-review-4 не заводился (санкция вердикта). make check после каждой волны: 897→900→902→905 passed.
  - Итого: 4 код-коммита (222fbdb волна, f248573/1f2fc90/4fe09d4 fix-волны), тестов 844→905 (+61), файл-сессия/журналы — закрывающим коммитом.
- Отклонения от плана:
  - **Task 4 «prd.md FR-12» → фактически FR-4**: fan-out guard — контракт валидатора (FR-4), в FR-12 (декомпозиция) guard упомянут только через ADR-004-трассировку; правка FR-4 + матрица FR-4 (+ADR-006 §4.6), строка FR-12 не тронута (ADR-004 обновлён — трассировка осталась корректной). Осознанно, суть требования (prd синхронен с кодом) выполнена.
  - **Task 5: done не достигнут** — после корректного validate/execute/compact прогоны падают на interpret_result: `TypeError: Object of type Decimal is not JSON serializable` (json.dumps компакта без default; numeric из psycopg; воспроизведено дважды; ранее не ловилось — прежние живые вопросы давали COUNT/int). Критерий Task 5 выполнен (trap+retry видны до interpret); дефект вне скоупа feat-023 (интерпрет-контур S15 не трогается — D-037) → BL-059 (P1). Атрибуция hallucination fan-out-классу — по конструкции (белый ящик корпуса; текст замечания в трейс не пишется — S9).
  - **Правило 3 плана исполнено в строгой редакции v1′** (ARCH-GATE R2, план допускал «уточнить на ARCH-GATE»): GROUP BY-условие в буквальной записи неспасительно для SUM/COUNT.
  - Правило 5 «path_semantics → JoinPlan/**Skeleton**»: Skeleton-часть вычеркнута (ruling R5a — guard не рендерится в SQL, вторая копия = дрейф).
  - Task 1 просил «property-проверку против парсера на корпусе 15–20» — реализовано 15 именованными кейсами с ручной разметкой (все заявленные формы закрыты); параметризованный sweep не заводился (ревью: «вкусовое улучшение, не находка»).
- Перенесено в другие сессии:
  - BL-058 (сид-ребро e_campaigns_orders cardinality против конвенции left-is-«1» — решение владельца: правка сида миграцией V5/оговорка конвенции; сет CI-гейт активации BL-026) — ARCH-GATE finding 2.
  - BL-059 (P1: interpret_result Decimal-крэш — блокирует live-done для numeric-результатов; отдельная сессия).
  - BL-060 (fan-out guard v2: nested-скоупы — единственный пропуск класса; DISTINCT-формы; мэтчер стороны «n» — revisit при первом кейсе/с BL-025).

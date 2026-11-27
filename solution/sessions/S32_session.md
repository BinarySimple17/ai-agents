# S32 — fan-out guard: валидатор не пускает двойной счёт (feat-023)

Этап: Веха V4 (ДЗ-05) · долг аудита, окно 01–07.12.2026 | Владелец: агент | Зависимости: S31 | Оценка: 1 | Статус: [ ] не начата
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

- [ ] S31 закрыта (общие файлы валидатора — порядок исполнения): `grep -n "| S31 |" solution/sessions.md` → ✅
- [ ] `make check` зелёный (baseline репозитория)
- [ ] feat-011 и feat-007 в state=passing; feat-023 переведена в `active` на старте
- [ ] Инфра поднята и актуальна: `make infra-up && make infra-migrate && make sync-graph && make index-knowledge`
- [ ] Сессия СТАРТУЕТ: статус в шапке переведён в «◐ в работе» и ARCH-GATE резолвлен ДО кода (canon-check architect ✅ либо обоснованный N/A) — старт-гейт (`guard-subagents.sh --phase start`, D-027) проверяет только стартованные (◐) сессии; «[ ] не начата» — план, гейт её скипает

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

- [ ] `behavior` feat-023 выполняется построчно — тест на каждый пункт
- [ ] `make verify-feature F=feat-023` → PASS, evidence записан в `feature_list.json`
- [ ] FSM-путь без регрессий: `test_validate_sql.py`, `test_validate_sql_node.py`, `test_graph.py` зелёные; живой resume после clarify работает
- [ ] Живой прогон: двойной счёт не исполнен, retry с подсказкой виден в трейсе
- [ ] ADR-006, ADR-004, prd, RISKS синхронны с кодом
- [ ] `make check` EXIT=0, CI зелёный (S30); вердикт ARCH-GATE записан до кода

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

- ARCH-GATE: [✅ <verdict: compliant | violations | needs-decision> + <план/файл> | N/A — <причина>]
- REVIEW-GATE: [✅ <Ready to merge? вердикт> + <BASE7>..<HEAD7> | N/A — <причина>]

## Отчёт по сессии (заполняется по факту)

- Сделано:
  - [Task: что реально сделано, коммиты, число тестов/ассертов, live-прогон]
- Отклонения от плана:
  - [что сделали не так, как в задаче, и почему; каждое отклонение — осознанное]
- Перенесено в другие сессии:
  - [ID сессии — что перенесено и почему; если в беклог — BL-NN-файл в solution/backlog/ (см. backlog/README.md)]

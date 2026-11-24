# S35 — Рубеж 5 в БД: RLS по списку таблиц и автосинк графа

Этап: Веха V4 (ДЗ-05) · долг аудита, окно 14–20.12.2026 | Владелец: агент | Зависимости: S31 | Оценка: 1 | Статус: [ ] не начата
Фичи: — (сессия без фичи, states не меняются; затронут behavior feat-002 и feat-013 — их verification-команды прогоняются, результат в отчёт, прецедент S24)
Роль по фиче: — (сессия без фичи, states не меняются)

## ⚠️ Прочитай перед началом

Исполнитель: ИИ-агент; куратор: пользователь. Предмет — аудит 2026-11-23, E2 и E3b:

- **E2.** «Пять рубежей» (ADR-005): рубежи 2–4 лежат в одном валидаторе; независимый рубеж 5 (БД)
  проверяет только read-only и непустой `app.user_id` — миграция 0004 ставит политику
  `USING (current_setting('app.user_id', true) IS NOT NULL)` на `employees` и `bonuses`. Обход
  валидатора означает чтение чужих таблиц. Рекомендация аудита №2: RLS на всех бизнес-таблицах по
  списку разрешённых таблиц, который выставляет исполнитель, — тогда при обходе валидатора чужие
  таблицы вернут 0 строк (колонки остаются за валидатором).
- **E3b.** Синк графа только ручной (`make sync-graph`, `apps/sync_graph.py`); ADR-006 §4.3 говорит
  «счётчик будит синк», в коде никто не будит — прогон может идти по устаревшему снимку рёбер.

Обе правки добавляющие: FSM и тул-контур продолжают работать, граница только сужается.

Границы скоупа: БЕЗ policy-движка рубежа 5 (резерв ADR-005, `AccessPolicy`), БЕЗ колоночного PII в
БД (за валидатором), БЕЗ автоматического синка по расписанию (только сверка на старте прогона),
БЕЗ правки применённой миграции 0004 (только новый файл — D-028).

### Правила для исполнителя

1. Ветка `harness/rls-scope-graph-sync` от `milestone/v4`; merge — в `milestone/v4` (D-031).
2. **Миграция — новый файл 0011.** `ENABLE ROW LEVEL SECURITY` на 7 бизнес-таблицах; одна политика
   на таблицу: `FOR SELECT TO agent_ro USING ('<таблица>' = ANY(string_to_array(current_setting('app.allowed_tables', true), ',')))`.
   Политика 0004 снимается и заменяется: PERMISSIVE-политики складываются через ИЛИ, вторая
   политика рядом с 0004 расширила бы, а не сузила доступ. Условие «user_id выставлен» сохраняется
   внутри новой политики. `ENABLE` без `FORCE` — прецедент 0004 (владелец-мигратор не режется).
3. **Список таблиц — `RunContext.scope`** (зарезервирован с S13: «scope — карта прав»).
   Заполняет код узла или тула из проверенных прав: `scope={"tables": sorted(permissions)}`.
   Исполнитель выставляет `app.allowed_tables` тем же bind-механизмом, что `app.user_id`.
   Scope отсутствует или пуст — RLS даёт 0 строк (fail-closed), не «всё».
   Вопрос ARCH-GATE: список = все разрешённые таблицы пользователя или только таблицы
   проверенного SQL. Рекомендация v1 — права: граница RLS совпадает с границей прав и не зависит
   от формата вердикта валидатора.
4. **Проба COUNT** (`execute_sql_node._run_probe`) идёт тем же каналом и тем же `ctx` — тоже под RLS.
5. **Автосинк графа:** порт `SchemaGraphStore.ensure_synced()` — сверяет ревизию реестра
   (`settings_version('relationships')`, чтение под `agent_planner`) с ревизией снимка; расходятся —
   синк. Вызов на старте прогона (точку входа определить на ARCH-GATE: обёртка в `composition`
   до `answer_question.run`, чтобы слой приложения не знал о графовой памяти). Синк упал — прогон
   не начинается по устаревшему снимку молча: честный отказ (S00-8). Ручной `make sync-graph` остаётся.
6. Документы в том же коммите: `er-data-model.md` (строка RLS), `security.md` §2.1, ADR-005 §4
  (рубеж 5), ADR-006 §4.3 (кто будит синк), S00 контракт 4, `docs/quality-document.md`.

## Предусловия

- [ ] S31 закрыта: тул-контур передаёт права в `RunContext` — `grep -n "| S31 |" solution/sessions.md` → ✅
- [ ] `make check` зелёный (baseline репозитория)
- [ ] feat-002, feat-005, feat-007, feat-011, feat-013 в state=passing (сверка `feature_list.json`)
- [ ] Инфра поднята: `make infra-up`; миграции применены: `make infra-migrate`
- [ ] Сессия СТАРТУЕТ: статус в шапке переведён в «◐ в работе» и ARCH-GATE резолвлен ДО кода (canon-check architect ✅ либо обоснованный N/A) — старт-гейт (`guard-subagents.sh --phase start`, D-027) проверяет только стартованные (◐) сессии; «[ ] не начата» — план, гейт её скипает

## Задачи

### Task 1: Миграция 0011 — RLS по списку таблиц

Новый файл `src/migrations/versions/<дата>_0011_rls_allowed_tables.py`; `downgrade` возвращает
политику 0004. Тесты `src/tests/integration/test_infra_schema.py`: все 7 бизнес-таблиц под RLS,
ровно одна политика `agent_ro` на таблицу, политики `agent_ro_requires_user_id` больше нет.

**✅ Самопроверка:**

```bash
make infra-migrate && make infra-migrate                       # идемпотентно
uv run pytest src/tests/integration/test_infra_schema.py -q    # новые проверки + прежние 13
```

Чистая комната (в отчёт): свежая БД → `alembic upgrade head` → те же проверки зелёные. Команды с
удалением томов — только по согласованию с куратором.

### Task 2: Исполнитель и `RunContext.scope`

`postgres_executor.py`: `set_config('app.allowed_tables', …, true)` из `ctx.scope`; пустой или
отсутствующий scope — пустая строка (0 строк, fail-closed). Unit-тесты `test_postgres_executor.py`
(значение подстановки), integration `test_execute_sql.py`: без scope — 0 строк; scope `{'orders'}`
— `orders` читается, `customers` — 0 строк; `employees` без прав — 0 строк.

**Обязательная регрессия** (падает на до-фиксовом коде): сырой SQL `SELECT * FROM bonuses` под
`agent_ro` с выставленным `app.user_id`, но без прав — до фикса строки возвращались, после — 0 строк.

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_postgres_executor.py src/tests/unit/test_domain_entities.py src/tests/integration/test_execute_sql.py -q
```

### Task 3: Проводка scope

`execute_sql_node.py` (scope из `state.permissions`), `RunSqlTool` (S31), проба COUNT — тот же `ctx`.
Тесты wiring: `test_execute_sql_node.py`, `test_run_sql_tool.py`, `test_container_wiring.py`.

**✅ Самопроверка:** `uv run pytest src/tests/unit/test_execute_sql_node.py src/tests/unit/test_run_sql_tool.py src/tests/unit/test_container_wiring.py -q`.

### Task 4: Автосинк графа

Порт + адаптер `Neo4jGraphStore.ensure_synced()` + вызов на старте прогона. Integration
`src/tests/integration/test_join_graph.py`: ревизия реестра выросла (правка `agent_relationships` в
тесте) → следующий прогон синкает сам, `graph_version` вырос; ревизии равны — синка нет (шпион на
число записей). Синк недоступен → честный отказ, не устаревший снимок.

**✅ Самопроверка:**

```bash
make infra-up && uv run pytest src/tests/integration/test_join_graph.py src/tests/integration/test_plan_joins_db.py -q
```

### Task 5: Документы (тот же коммит, что и код)

`er-data-model.md` → следующая версия (RLS-строка), `security.md` §2.1, ADR-005 §4, ADR-006 §4.3,
S00 контракт 4 (порты: `ensure_synced`), `ARCHITECTURE.md`, `docs/quality-document.md`,
`behavior` feat-002/013 в `feature_list.json`.

**✅ Самопроверка:** `git grep -n "allowed_tables" -- docs solution/sessions/S00_session.md | wc -l` > 0 — политика описана в каноне.

### Task 6: Слой 3 — живые прогоны

Happy-path hw-05 («Сколько заказов у клиентов из РФ?», u_logist) — ответ не изменился.
Отрицательная сцена: прямое подключение под `agent_ro` с `app.user_id`, но без `app.allowed_tables` →
0 строк по бизнес-таблицам.

**✅ Самопроверка:**

```bash
uv run --env-file .env python -m apps.cli "Сколько заказов у клиентов из РФ?" --fsm --token dev-logist-token 2>/dev/null | grep -c "100 заказов"   # 1
```

## Критерии приёмки

- [ ] Обход валидатора (сырой SQL под `agent_ro` с чужой таблицей) возвращает 0 строк — integration-тест, падающий на до-фиксовом коде
- [ ] Без scope — 0 строк на всех бизнес-таблицах; со scope — только разрешённые
- [ ] FSM и тул-контур без регрессий: verification feat-002/005/011/013 прогнаны; happy-path hw-05 даёт прежний ответ
- [ ] Синк графа происходит сам при устаревшем снимке; при равных ревизиях вызова нет; недоступный синк — честный отказ
- [ ] Миграция — новый файл, идемпотентна, чистая комната зелёная; er/security/ADR-005/ADR-006/S00 синхронны
- [ ] `make check` EXIT=0, CI зелёный (S30); вердикт ARCH-GATE записан до кода

## Ловушки

1. **PERMISSIVE-политики складываются через ИЛИ:** вторая политика рядом с 0004 открыла бы
   доступ шире прежнего. Одна политика на таблицу.
2. **Кого ещё режет RLS:** политика `TO agent_ro`; `agent_planner` и сервисный DSN (владелец) не
   должны пострадать — но `agent_planner` с SELECT-грантом на бизнес-таблицу (если появится) молча
   получит 0 строк: проверить гранты 0002 после миграции тестом.
3. **Миграции и сид идут под владельцем** — RLS их не режет (`ENABLE` без `FORCE`); случайно
   поставленный `FORCE` сломает `test_infra_schema` и демо-данные.
4. **Синк на старте — обращение к Neo4j при каждом прогоне:** сверка ревизий — один дешёвый запрос;
   полная загрузка — только при расхождении.
5. **Тул-контур после S31** получает права в `RunSqlTool` — scope должен выставляться там же, где
   права, иначе тул-контур останется с пустым scope и вернёт 0 строк молча.
6. **DSN — `127.0.0.1`, не `localhost`** (Windows/IPv6-ловушка S02).

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

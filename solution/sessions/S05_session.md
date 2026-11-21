# S05 — resolve_access: права из таблиц настроек + DevTokenAuth (веха V4, ДЗ-05)

Этап: Веха V4 (ДЗ-05: первый узел FSM — детерминированный, без LLM) | Владелец: агент | Зависимости: S02, S04 | Оценка: 1 | Статус: ◐ в работе (2026-11-21)
Фичи: feat-005 — resolve_access; зависимости feat-002 (passing), feat-004 (закрывается S04 до старта)
Роль по фиче: закрывает feat-005 — финальная сессия, через make verify-feature

## ⚠️ Прочитай перед началом

Исполнитель: ИИ-агент. К моменту старта: S02 закрыта (схема v1.5: `agent_permissions`,
`agent_user_roles`, `settings_version` с триггерными счётчиками, роли), S04 закрыта
(каркас FSM: узел resolve_access — заглушка, ребро `rejected` существует). Эта сессия
наполняет первый узел конвейера: `user_id → роли → права` из таблиц настроек —
`state.permissions = {tables: {table: [columns]}}` (контракт 3 S00) — единственный
источник для подграфа, retrieval-фильтра и AST-валидатора (S11). Плюс порт
`AuthProvider` + `DevTokenAuth` (MVP, S00 таблица портов): identity приходит
проверенной — аутентификация вне скоупа.

Границы скоупа: БЕЗ RLS-канала исполнения (S13, миграция 0004 — уже живёт в
SQLExecutor), БЕЗ валидатора SQL (S11 потребляет permissions), БЕЗ кэша прав в Valkey
(если не доказано needed — см. правило 4: сначала settings_version-сверка), БЕЗ
OIDC (прод-реализация порта — после MVP).

### Правила для исполнителя

1. Ветка `feat-005-resolve-access` от `milestone/v4` (D-031); merge — в `milestone/v4`.
2. Fail-closed (S00 правило 8): неизвестный user_id / непроверенный токен →
   `rejected`-исход БЕЗ вызова LLM; любая недоступность БД настроек → отказ, не «пустые права = всё разрешено».
3. Права читаются из таблиц настроек на каждый запрос (ADR-005 §4); per-user кэш —
   только с обязательной сверкой `settings_version` (контракт 3 S00): версия выросла → кэш не используется.
4. Чтение таблиц настроек — под ролью по миграциям S02 (agent_planner читает настройки;
   вопрос роли чтения подтвердить в ARCH-GATE — дефолт: существующие гранты, новая миграция только при нехватке).
5. Узел — детерминированный код, без LLM (behavior feat-005); молчит о недоступном:
   карта прав не содержит лишнего даже в трейс (S9-политика отказов).

### Rulings ARCH-GATE (canon-check architect 2026-11-21 — verdict violations, закрыты до кода)

- **R1 (Q1, гранты):** грантов agent_planner достаточно (`GRANT SELECT ON
  agent_permissions, agent_user_roles, agent_data_sources, settings_version` —
  миграция 0002:80-83), новой миграции НЕТ. Подключение — `settings.agent_planner_dsn`
  (c4-l2: пул planner «retrieval, настройки»).
- **R2 (Q2, порт):** порт чтения прав — `PermissionStore` (НЕ `AccessPolicy` —
  ADR-005:42 резервирует это имя под будущий policy-движок RLS/region);
  `application/ports/permission_store.py` + адаптер
  `infrastructure/persistence/pg_permission_store.py` (образец пары —
  edge_proposal_store). Строка в таблице портов S00 — тем же коммитом
  (канон-дисциплина S00).
- **R3 (Q3, счётчики):** кэш прав сверяет ОБА счётчика `'permissions'` +
  `'user_roles'` одним SELECT (выдача/отзыв роли инкрементирует `'user_roles'` —
  миграция 0001:177; сверка только 'permissions' — дыра revoke, нарушает ADR-005 §4).
  Тест Task 3: правка agent_permissions И выдача/отзыв роли — оба перечитывают права.
  In-memory per-user кэш в MVP правомерен (ADR-005 §4: пользователей ≤ 10, сброс
  целиком при росте версии).
- **R4 (Q4, identity-проводка):** верификация — в точке входа: CLI вызывает
  `AuthProvider.verify(token)` (composition поставляет DevTokenAuth) ДО `graph.invoke`;
  verified `user_id` кладётся в state (поле `user_id` в `AgentState`,
  `initial_state(question, user_id)`); сырой `--user` verify не обходит. Узел
  resolve_access fail-closed на пустой/отсутствующей identity. Синк стыка 1 S00
  тем же коммитом.
- **R5 (Q5, колонки):** CSV-нормализация на границе адаптера (прецедент ruling I4
  S09: str-CSV → list, fail-closed ValueError на мусор); `'*'` остаётся литералом
  `'*'` в списке колонок — разворот в конкретные колонки требует DDL-интроспекции,
  интерпретация `'*'` — зона S11/S09, не S05.
- **R6 (Q6, rejected/пустая карта):** unknown user_id (нет строк в agent_user_roles)
  и непроверенный токен — оба `FLAG_IDENTITY_FAILED` → существующее ребро
  `terminal_rejected` (routing.py:123-127, роутинг не трогаем). «Роли есть, прав нет»
  — легальный deny-по-умолчанию: пустая карта, продолжение по графу (отдельный флаг
  НЕ нужен; подграф/retrieval/валидатор в S09/S11 закроют всё сами).
  `graph_version=0` (снапшот не загружен) пишет S05; реальный пин снапшота — S07.

## Предусловия

- [x] Сессия СТАРТУЕТ: статус в шапке переведён в «◐ в работе» и ARCH-GATE резолвлен ДО кода (canon-check architect ✅ либо обоснованный N/A) — старт-гейт (`guard-subagents.sh --phase start`, D-027) проверяет только стартованные (◐) сессии; «[ ] не начата» — план, гейт её скипает
- [ ] S02 закрыта: `make infra-up && make infra-migrate && uv run pytest src/tests/integration/test_infra_schema.py -q` → passed
- [ ] S04 закрыта: `uv run pytest src/tests/unit/test_graph.py -q` → passed (feat-004 passing)
- [ ] `make check` зелёный (baseline репозитория)
- [ ] feat-002, feat-004 в state=passing; feat-005 активирована в state=active до старта работ

## Задачи

### Task 1: Порт AuthProvider + DevTokenAuth

`application/ports/auth_provider.py`: `verify(token) -> identity` (ADR-005; MVP —
`infrastructure/persistence/dev_token_auth.py`): dev-токен → user_id из .env-списка
демо-пользователей, никаких обращений к внешним IdP; пустой/чужой токен → отказ
(fail-closed). identity — только из проверенного токена (контракт 3 S00).
Проводка identity — ruling R4: verify вызывается в точке входа (CLI) ДО
`graph.invoke`, verified `user_id` → поле `user_id` в `AgentState`
(`initial_state(question, user_id)`); сырой `--user` verify не обходит.

**✅ Самопроверка:**

```bash
uv run pytest src/tests/integration/test_resolve_access.py -q   # раздел Auth: валидный dev-токен → identity; невалидный → отказ без LLM-вызова
```

---

### Task 2: Сервис resolve_access — карта прав из настроек

`application/services/resolve_access_service.py`: (1) `agent_user_roles` → роли user_id;
(2) `agent_permissions` → таблицы + колонки + scope; (3) слияние ролей → единая карта
`{tables: {table: [columns]}}` в `state.permissions` + `graph_version=0` (снапшот не
загружен — ruling R6; реальный пин — S07); unknown user_id → вердикт rejected. SQL
чтения — параметризованный, через порт `PermissionStore` (ruling R2), под ролью из
правила 4; колонки — CSV-нормализация на границе адаптера, `'*'` литералом (ruling
R5); пустая карта (роли есть, прав нет) — легальный deny-по-умолчанию, не ошибка
(ruling R6).

**✅ Самопроверка:**

```bash
make infra-up && uv run pytest src/tests/integration/test_resolve_access.py -q   # known u_analyst/u_logist → полная карта по сидам 0003; unknown → rejected
```

---

### Task 3: Актуальность прав — settings_version

Кэш прав per-user (в-memory процесса достаточно для MVP; Valkey — только если доказано)
со сверкой ОБЕИХ версий `settings_version` — `'permissions'` + `'user_roles'` одним
SELECT (ruling R3: выдача/отзыв роли инкрементирует 'user_roles'; сверка только
'permissions' — дыра revoke) на каждый запрос: триггер инкрементирует счётчик при
правке строк настроек → следующий запрос перечитывает права (behavior feat-005:
«правка строк настроек действует со следующего запроса»).

**✅ Самопроверка:**

```bash
make infra-up && uv run pytest src/tests/integration/test_resolve_access.py -q   # правка agent_permissions между запросами → вторая карта отличается (счётчик вырос)
```

---

### Task 4: Подключение в FSM + rejected-ребро

Наполнение заглушки S04: узел resolve_access вызывает сервис, пишет permissions в
state; routing НЕ трогаем — rejected-вердикт это `FLAG_IDENTITY_FAILED` (unknown
user_id И непроверенный токен, ruling R6) → существующее ребро `terminal_rejected`
(отказ без LLM, payload — вежливый текст без раскрытия причин настройки).
Интеграционный тест через собранный Container (не напрямую сервис) — проверка, что
FSM-путь жив.

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_graph.py src/tests/integration/test_resolve_access.py -q   # оба зелёные; порядок узлов не сломан
```

## Критерии приёмки

- [ ] Для known user_id узел возвращает полную карту прав в state; для неизвестного — deny-исход с валидным отказом (behavior feat-005)
- [ ] Права актуальны на момент запроса: правка строк настроек действует со следующего запроса (behavior feat-005)
- [ ] `state.permissions = {tables: {table: [columns]}}` — формат контракта 3 S00, без локальных расширений
- [ ] Identity — только из проверенного токена; fail-closed на каждом шаге (контракт 3 S00)
- [ ] Узел не делает LLM-вызовов (behavior feat-005) — проверить тестом-шпионом
- [ ] Фича закрыта: `make verify-feature F=feat-005` → PASS, state=passing с evidence

## Ловушки

1. **Не путать два контура прав:** resolve_access (мир агента: подграф/retrieval/валидатор)
   и RLS-канал исполнения (S13, `SET LOCAL app.user_id`) — независимые рубежи; здесь
   RLS не трогаем вовсе.
2. `settings_version` читается той же ролью, что и настройки; если роль не имеет
   гранта на счётчики — это находка для ARCH-GATE (новая миграция GRANT), не тихий
   обход через суперюзера.
3. Кэш прав без сверки версии = устаревшие права после правки настроек — главный
   риск фичи; тест на «правка между запросами» обязателен (Task 3).
4. Сиды 0003: u_analyst (широкие права), u_logist (узкие) — использовать как готовые
   сценарии; демо-сцена deny «премии без прав HR» — на них же (S27).
5. Гейт strict: строки ARCH/REVIEW заполняются по факту; «N/A — сессия не начата» = FAIL.

## Гейты субагентов (заполняется по факту, до закрытия сессии)

- ARCH-GATE: ✅ verdict violations → rulings R1–R6 закрыты до кода (canon-check architect 2026-11-21, план S05_session.md: гранты 0002 достаточны — R1; порт `PermissionStore` + S00-строка — R2; ОБА счётчика settings_version + тест revoke — R3; identity-проводка verify→state — R4; CSV/'\*' на границе — R5; rejected/пустая карта/graph_version=0 — R6)
- REVIEW-GATE: [✅ <Ready to merge? вердикт> + <BASE7>..<HEAD7> | N/A — <причина>]

## Отчёт по сессии (заполняется по факту)

- Сделано:
  - [Task: что реально сделано, коммиты, число тестов/ассертов, live-прогон]
- Отклонения от плана:
  - [что сделали не так, как в задаче, и почему; каждое отклонение — осознанное]
- Перенесено в другие сессии:
  - [ID сессии — что перенесено и почему; если в беклог — BL-NN-файл в solution/backlog/ (см. backlog/README.md)]

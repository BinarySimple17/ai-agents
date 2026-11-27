# S33 — Самокоррекция без пустых ретраев: классы, подсказки, кап, stdout

Этап: Веха V4 (ДЗ-05) · долг аудита, окно 07–14.12.2026 | Владелец: агент | Зависимости: S32 | Оценка: 1.5 | Статус: ✅ 2026-11-28
Фичи: — (сессия без фичи, states не меняются; затронуты behavior feat-011 и feat-014 — их verification-команды прогоняются, результат в отчёт, прецедент S24)
Роль по фиче: — (сессия без фичи, states не меняются)

## ⚠️ Прочитай перед началом

Исполнитель: ИИ-агент; куратор: пользователь. Предмет — аудит 2026-11-23, часть A (Средняя), пять находок одного контура — цикла самокоррекции:

- **A3.** Решение «ретрай или degrade» частично принимает модель: в LLM-контуре
  `retry_futile = llm_class in _FUTILE_CLASSES` (`diagnose_error_service.py`, 289–294), класс возвращает
  сама модель; узел использует это для назначения ретрая (`diagnose_error_node.py`, 152).
- **A4.** Ошибка «объекта нет» с уровня БД классифицируется как `syntax` (`postgres_executor.py`,
  70–71: любой `ProgrammingError` → `SYNTAX`), хотя `DiagnoseClass.SEMANTICS` определён именно так.
  Подсказка диагностики не сообщает фактических колонок, при temperature 0 регенерация
  детерминированно повторяется. Обе degrade-сцены hw-05 упёрлись ровно в кап 8 из 8: три
  одинаковые диагностики (614→167 токенов), попытки генерации 2 и 3 совпадают по токенам.
- **A5.** При правах `'*'` валидатор пропускает любую колонку таблицы (`sql_validation.py`,
  341–346 и 387–392): `orders.client_id` прошёл `validate_sql` и упал только в БД.
- **E6a.** Кап 8 LLM-вызовов (`settings.py:137`) равен худшему пути политики M1 без запаса.
- **A7.** Сырой текст ошибки БД уходит в stdout: `execute_sql_node.py:101` кладёт `str(exc)` в
  `error.message`, `cli.py:818–822` печатает его («column orders.client_id does not exist LINE 1:
  SELECT …» — трейс `hw-05/fsm-degrade-execute-retries-stdout.txt`).

Опора: S31 (единый рубеж: валидатор и права — общий модуль FSM-контура; тул-контур с тех пор
удалён — BL-048/S39), S32 (детерминированная подсказка fan-out — здесь этот механизм
становится общим). A6 (ребро перепланирования) — отдельно, в S34, и опирается на каталог колонок из
этой сессии.

Границы скоупа: БЕЗ нового ребра FSM (S34), БЕЗ усиления RLS (S35), БЕЗ смены тира в
`interpret_result` (M8 — BL-030), БЕЗ правки `hw-01/README.md` (артефакт сданного ДЗ-1).

### Правила для исполнителя

1. Ветка `harness/retry-quality` от `inter/hw05-hw06`; merge — в `inter/hw05-hw06` (трек пред ДЗ-06; `milestone/v4` — неприкосновенная точка сдачи ДЗ-05; поправка S41/п.1(в), D-042 — было «от `milestone/v4`, merge — в `milestone/v4` (D-031)»).
2. **Решение о ретрае — только код** (AGENTS.md: MUST NOT позволять LLM управлять переходами).
   Модель в `diagnose_error` не может задать бесполезный класс: `permissions` и `connection`
   убираются из enum тула `report_diagnosis` — их ставит только детерминированная карта по классу
   ошибки; `retry_futile` считается кодом.
3. **Детерминированные классы не тратят LLM.** Где причина известна по коду (нет колонки, таблицы
   или функции; права; read-only; соединение), подсказку строит код из того, что коду известно:
   права пользователя и каталог колонок таблиц плана. LLM-разбор остаётся для `timeout`, `empty`
   (проба COUNT) и `syntax` без объекта.
4. **Существование колонок.** При правах `'*'` колонка сверяется с каталогом колонок мира —
   аддитивный метод порта `PermissionStore.columns_catalog(tables)` (чтение
   `information_schema.columns` под `agent_planner`, fail-closed). `agent_data_sources` остаётся
   источником существования таблиц (BL-009). Колонки нет — `hallucination` (retry), а не ошибка БД.
5. **Два канала по S9.** Пользовательский `error.message` — фиксированный текст узла; факты (сырой
   текст БД, реальные колонки, счётчики) — внутренний ключ `error["detail"]` → diagnose → подсказка
   регенерации. CLI печатает `message` и только если исход не дал текста ответа.
6. **Кап вызовов.** Канон ≤ 8 (ADR-007 §4.1, state-agent, hw-01 §6). После Task 1–3 вычислить худший
   путь M1 по факту и закрепить тест-инвариантом «кап ≥ худший путь + запас ≥ 1 вызов». Если 8 не
   влезает — решение на ARCH-GATE: поднять кап (правка канона в том же коммите) или подтвердить 8
   расчётом. Оставить противоречие молча нельзя.

## Предусловия

- [x] S32 закрыта: `grep -n "| S32 |" solution/sessions.md` → ✅; feat-023 в passing
- [x] `make check` зелёный (baseline репозитория) — EXIT=0 на 48032c6 перед merge
- [x] feat-005, feat-011, feat-013, feat-014 в state=passing (сверка `feature_list.json`)
- [x] Инфра поднята: `make infra-up` (нужны живые ошибки БД и `information_schema`)
- [x] Сессия СТАРТУЕТ: статус в шапке переведён в «◐ в работе» и ARCH-GATE резолвлен ДО кода (canon-check architect ✅ + rulings R1–R2) — старт-гейт (`guard-subagents.sh --phase start`, D-027) проверяет только стартованные (◐) сессии; «[ ] не начата» — план, гейт её скипает

## Задачи

### Task 1: Классы ошибок — порт, исполнитель, карта диагностики (A4, часть 1)

`SQLFailureClass` + `SEMANTICS = "semantics"` (аддитивно); `_classify`: `psycopg.errors.UndefinedColumn`,
`UndefinedTable`, `UndefinedFunction` → `SEMANTICS` раньше общей ветки `ProgrammingError`;
`_DETERMINISTIC_MAP` + `"semantics"`. Класс ошибки — один на контур исполнения (FSM —
единственный контур: тул-контур удалён, BL-048/S39).

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_postgres_executor.py src/tests/unit/test_diagnose.py -q   # unit: классы по типам исключений
make infra-up && uv run pytest src/tests/integration/test_execute_sql.py -q                # integration: несуществующая колонка → semantics
```

### Task 2: Каталог колонок и права `'*'` (A5)

- Порт: `PermissionStore.columns_catalog(tables: frozenset[str]) -> dict[str, frozenset[str]]`;
  адаптер `PgPermissionStore`; фейк в `src/tests/unit/fakes.py`.
- Валидатор: `'*'` разрешает только существующие колонки; остальное — `hallucination`
  «колонка X отсутствует у таблицы T — доступные: …» (имена только из мира пользователя). Правка —
  в общей части ACL (`_check_columns`) для FSM-входа (контур исполнения один — FSM;
  оговорка прежней редакции про свободный вход снята S40: тул-контур удалён — BL-048/S39,
  D-039/D-040 предметно не действуют).
- **Регрессия (падает на до-фиксовом коде):** права `{'orders': ['*']}`, SQL с `orders.client_id`
  → было `ok`, стало `hallucination`; исполнитель не вызывается.

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_validate_sql.py -q -k "star or catalog"   # FSM-вход: новые тесты
uv run pytest src/tests/unit/test_validate_sql.py -q                        # регрессия общей части ACL (актуализация S40: test_validate_free_sql.py удалён вместе с тул-контуром — BL-048/S39; регрессы общей части живут в FSM-корпусе)
uv run pytest src/tests/integration/test_execute_sql.py src/tests/integration/test_resolve_access.py -q
```

### Task 3: Решение о ретрае — только код; подсказка без LLM (A3, A4)

- `diagnose_error_service.py`: enum тула без бесполезных классов; `retry_futile` — по
  детерминированной карте; для `semantics`, `permissions`, `read_only`, `connection` подсказку
  строит код из `error["detail"]` и каталога; LLM — только `timeout` / `empty` / прочий `syntax`.
- `execute_sql_node.py`: `error = {"class", "message": <фиксированный текст по классу>, "detail": str(exc)}`.
- `generate_sql_node.py`: retry-контекст получает внутренний `detail` и подсказку.
- **Регрессия A3 (падает на до-фиксовом коде):** мок-LLM возвращает класс `permissions` для
  retry-класса — до фикса ретрай снимался выходом модели, после — класс вне enum не принимается,
  решение принадлежит коду.

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_diagnose.py src/tests/unit/test_execute_sql_node.py src/tests/unit/test_generate_sql.py -q   # semantics: LLM не вызывается (шпион), подсказка содержит реальные колонки
```

### Task 4: Кап вызовов (E6a)

Тест-инвариант `src/tests/unit/test_llm_call_budget.py`: перебор худших путей M1 (clarify ×1,
plan_joins ≤ 3, generate_sql ≤ 3, interpret ×1, degrade ×1, diagnose-LLM ≤ 2) — максимум вызовов
≤ `fsm_max_llm_calls − 1`. Не выполняется — канон-правка ADR-007 §4.1, state-agent и дефолт
`settings.py` по ruling ARCH-GATE (в `hw-01/README.md` не переносить — расхождение в FIXES).

**✅ Самопроверка:** `uv run pytest src/tests/unit/test_llm_call_budget.py -q` — зелёный; значение капа и расчёт — в отчёте.

### Task 5: stdout без сырой ошибки БД (A7)

`cli.py` `_run_fsm`: строка «Ошибка:» печатается только для исходов без текста ответа; текст —
фиксированный `message`. **Регрессия A7:** на до-фиксовом коде stdout degrade-исхода содержит
`LINE 1:` и фрагмент SQL, после — нет.

**✅ Самопроверка:** регрессия A7 — живым прогоном Task 6 (`grep -c "does not exist\|LINE 1:"` → 0).
Юнит-теста `_run_fsm` в наборе нет; `test_agent_flow.py`/`test_tool_loop.py` сняты вместе
с тул-контуром (BL-048/S39) — их сцены ретирены, замены не требуется; Task 6 — живые прогоны
сцен FSM hw-05.

### Task 6: Слой 3 — живые прогоны на сценах hw-05

Переснять `hw-05/fsm-degrade-validate-retries-*` и `hw-05/fsm-degrade-execute-retries-*` (артефакт
текущей вехи). Ожидание: LLM-вызовов на отказ меньше, сырой ошибки в stdout нет; если сцена стала
завершаться `done` — зафиксировать как наблюдаемое.

**✅ Самопроверка:**

```bash
uv run --env-file .env python -m apps.cli "Какие клиенты участвовали в рекламных кампаниях?" --fsm --token dev-logist-token > /tmp/s33.out 2> /tmp/s33.err
grep -c "does not exist\|LINE 1:" /tmp/s33.out    # 0
grep -c "diagnose" /tmp/s33.err                     # меньше, чем в прежнем трейсе (было 3)
```

### Task 7: Документы (тот же коммит, что и код)

`observability.md` (словарь событий diagnose/validate), `security.md` (каналы S9), ADR-005
(каталог колонок в перечне рубежей), S00 контракт 1 (`error.detail`), ADR-007 §4.1 (если кап
менялся), `docs/quality-document.md`, `behavior` feat-011/014 в `feature_list.json`, FIX-011.

**✅ Самопроверка:** `git grep -n "retry_futile" -- src/text_to_sql_agent | grep -c "llm_class"` → 0 (решение не читает класс модели).

## Критерии приёмки

- [x] A3: модель не может задать бесполезный класс (схема тула + enforcement `_parse_diagnosis` ValueError — N3), `retry_futile` считает код; регрессия `test_retry_futile_is_code_decision_not_model` падает на до-фиксовом коде (bite-стэш: test_diagnose 5 failed)
- [x] A4: «колонки нет» с уровня БД → класс `semantics`; подсказка детерминированная из фактов (issues → detail → каталог, грант-фильтр I-1 fix-волны); LLM для класса не вызывается (шпион `llm_calls == 0`)
- [x] A5: `'*'` не пропускает несуществующую колонку — `hallucination`/`denied` на валидации до исполнителя (TestStarColumnsCatalog; bite-стэш: 119 failed на до-фиксе); GRANT-дрейф → дефект стыка fail-closed (R1, `columns_catalog_miss`)
- [x] E6a: тест-инвариант `test_llm_call_budget` зелёный (N4 — достижимые пути: норма 4 / волна 7 / hw-05 ≈5 ≤ кап−1; штормы 21/25 режутся намеренно); кап 8 подтверждён расчётом, канон дополнен (ADR-007 §4.1 ruling E6a) — settings.py/state-agent не тронуты
- [x] A7: в stdout нет сырой ошибки БД и SQL (`grep -c` → 0 на обоих переснятых трейсах); факты — внутренние каналы `detail`/`probe` + 2 юнит-пина узлов
- [x] Трейсы hw-05 пересняты (execute-retries → done, validate-retries → degrade 3×0-LLM); verification feat-011 (148 passed) / feat-013 (17 integration live) / feat-014 (32 passed) прогнаны; `make check` EXIT=0, 934 passed — CI на рабочих ветках не триггерится (D-035: локальный рубеж — полный make check)
- [x] FIX-011 закрыт; вердикт ARCH-GATE записан до кода (canon-check 2026-11-27, rulings R1–R2)

## Ловушки

1. **Подсказка раскрывает чужие имена**: «доступные колонки» — только из прав пользователя и
   каталога разрешённых таблиц; для таблиц вне прав ответ остаётся фиксированным отказом.
2. **Та же подсказка при temp 0 = тот же SQL**: подсказка обязана нести то, чего модель не видела
   (реальные имена), иначе три ретрая повторяются.
3. **`error.message` читают несколько мест** (retry-контекст генератора, CLI, чекпойнт): при
   добавлении `detail` сверить карту владения `AgentState` и parity-тесты.
4. **Классы исключений psycopg без соединения**: `psycopg.errors.UndefinedColumn("…")` строится в
   юнит-тесте; живой запрос — в integration.
5. **Число тестов в отчётах** — фактическое из прогона, не «898/899» из прошлых записей.
6. **Канон капа продублирован** в ADR-007, state-agent, hw-01 §6 и `settings.py`: менять все в
   одном коммите либо не менять ни одного.

## Гейты субагентов (заполняется по факту, до закрытия сессии)

- ARCH-GATE: ✅ compliant после rulings R1–R2 (canon-check architect 2026-11-27, до кода: план канонен, 7/7 пунктов без нарушений границ; вердикт needs-decision закрыт двумя рулингами ниже; notes N1–N4 приняты в реализацию)
  - **R1 (Q1 — пустой каталог колонок ≠ пустые права)**: таблица с `'*'`-правом отсутствует в `columns_catalog` → дефект стыка, fail-closed: domain поднимает ValueError, узел маршрутизирует как дефект (denied → degrade БЕЗ retry) + `validate_sql.unavailable` (деталь оператору); НЕ hallucination — GRANT-дрейф (таблица в мире/правах без GRANT SELECT agent_planner) не должен циклить ретраи. Интеграционный инвариант: columns_catalog покрывает весь data_catalog() непусто. Прецедент: `_defect` (validate_sql_node.py:182), `RightsUnavailableError` fail-closed.
  - **R2 (Q5 — точка выборки каталога колонок)**: по прецеденту `catalog_provider` (validate_sql_node.py:57-64 — мир читается на КАЖДЫЙ визит, стык 3 «не кэш узла»): `columns_catalog_provider(tables)` — callable в узле validate_sql, вызов ТОЛЬКО при `'*'` в правах (`any("*" in cols)`); domain получает каталог параметром (без IO, AGENTS.md границы); НОВОГО поля state нет, S00-контракт 1 расширяется только `error.detail`. Стоимость — одно соединение agent_planner на визит валидации в '*'-сцене (тот же ценовой класс, что и data_catalog()).
  - **N1**: ссылки плана «cli.py:818–822» устарели — файл 219 строк, цель правки `_run_fsm`, печать ошибки :206-210.
  - **N2**: док-стринг postgres_executor.py:31 «неизвестный объект» синхронить в коммите Task 1 (Undefined* уходит в SEMANTICS).
  - **N3**: A3-энфорсмент — в коде приёмки выхода (`_parse_diagnosis` → ValueError «класс вне домена»), не в схеме тула; регрессия обязана падать на до-фиксовом коде.
  - **N4**: инвариант капа (Task 4) считает ДОСТИЖИМЫЕ пути M1 (исчерпание attempts узла ведёт в терминальный исход, не в продолжение), не наивную сумму перечисления (11 > 8 — артефакт сложения несовместимых веток).
  - Отложено (архитектор, вне скоупа S33): явный список колонок с несуществующей колонкой → остаётся denied (консервативно); кэширование columns_catalog по счётчику permissions (прецедент R3) — оптимизация, revisit при нагрузке.
- REVIEW-GATE: ✅ Ready to merge? **Yes** (цепочка: ревью-1 `48032c6..00fc386` «With fixes» 0C/3I/5M → fix-волна-1 `a0cdefc` [3I+4M кодом, M-5 → BL-063] → re-review-2 `00fc386..a0cdefc` «Yes» 0C/0I/4M [bite подтверждён ревьюером независимо: мутация «сигнатура есть, фильтр снят» ловится PII-утверждениями, снятие/переупорядочивание except-веток — R1-пинами; make check 934 passed воспроизведён независимо] → fix-волна-2 `38f3211` [4 минора в предписанном объёме: докстринги/bite-формулировки/доменный has_star_grant]; диапазон сессии `48032c6..38f3211`)

## Отчёт по сессии (заполняется по факту)

- Сделано:
  - **Волна `00fc386`** (код+тесты+docs одним атомарным коммитом) — все 7 задач плана:
    - **Task 1 / A4**: `SQLFailureClass.SEMANTICS`; `postgres_executor._classify`: UndefinedColumn/UndefinedTable/UndefinedFunction → SEMANTICS до ветки ProgrammingError (N2-докстринг снят); `_DETERMINISTIC_MAP += semantics`.
    - **Task 2 / A5 (R2)**: порт `PermissionStore.columns_catalog` + `PgPermissionStore` (information_schema.columns под agent_planner, fail-closed) + фейк; узел validate_sql — `columns_catalog_provider` по прецеденту catalog_provider (вызов только при '*', стык 3 «не кэш узла»); domain `_check_columns` переписан (см. отклонение 3 про refined-правило); R1 — '*'-таблица без записи каталога → дефект стыка fail-closed (см. отклонение 4).
    - **Task 3 / A3**: enum тула без futile-классов; энфорсмент в `_parse_diagnosis` (класс вне {syntax, timeout, empty} → ValueError, N3); LLM-вердикт `retry_futile=False` по построению; semantics — контур кода (подсказка из фактов, 0 LLM-вызовов, шпион в тестах).
    - **Task 4 / E6a (N4)**: `test_llm_call_budget.py` — 5 тестов: инвариант по ДОСТИЖИМЫМ путям (норма 4, волна 7, hw-05 ≈5 — все ≤ кап−1; шторм-пути 21/25 достижимы и режутся гвардом намеренно — intent-пины); кап 8 не поднят, settings.py/state-agent не тронуты; канон дополнен — ADR-007 §4.1 ruling E6a.
    - **Task 5 / A7**: execute_sql — `{class, message=фиксированный по классу, detail=str(exc)}`; generate_sql — retry-контекст сеет `detail`, не `message`; cli — «Ошибка:» только при исходе без текста ответа. Self-check `git grep retry_futile | grep llm_class` → 0.
    - **Task 6**: трейсы hw-05 пересняты: сцена execute-retries → исход **done** (валидатор ловит галлюцинацию колонки до БД, diagnose 0-LLM); сцена validate-retries → degrade, 3 diagnose × «вызовов 0» (было 3×1), execute_sql в маршруте нет, `grep -c "does not exist\|LINE 1:"` stdout → 0. README hw-05 — постскриптум «до/после».
    - **Task 7**: observability v1.7, security v2.2, ADR-005 v1.5, ADR-007 v1.4, S00 контракт 1 (error.detail + retry_futile код-only), quality-document, FIX-011 закрыт, behavior feat-011 (148 passed) / feat-014 (32 passed) — states не менялись (сессия без фичи).
  - **Fix-волна-1 `a0cdefc`** по ревью-1 (вердикт «With fixes» 0C/3I/5M): I-1 — грант-фильтр раздела «доступные колонки» (ловушка 1: '*'→каталог, явный список→список, вне прав→скрыть; каталог только при пустых issues) + 2 bite-теста; I-2 — карта владения AgentState +detail; I-3 — TestColumnsCatalogRulings (3 пина R1/R2 на узле, FakePermissionStore оживлён); M-1 — ColumnsCatalogMissError/columns_catalog_miss; M-2 — 24→25 (тест+ADR+quality); M-3 — типизация шва; M-4 — s9_messages.py; M-5 → BL-063.
  - **Fix-волна-2 `38f3211`** по re-review-2 (вердикт «Yes» 0C/0I/4M — миноры в предписанном объёме): докстринг `_semantics_hint` к фактическому поведению (issues подавляют всё; detail+каталог дополняют друг друга), страховочный комментарий о латентном инварианте узла, 3×«ValueError»→«ColumnsCatalogMissError», bite-формулировка теста — буквально воспроизводимая, доменный предикат `has_star_grant` (дубликаты сняты). Счётчик тестов не изменился (934).
  - **Верификация**: `make check` EXIT=0, **934 passed** (baseline 905 + 29). Bite-прогоны на до-фиксовом коде (stash): волна — test_diagnose 5 failed, test_validate_sql 119 failed/29 passed, postgres_executor collection-error, 2 пина двухканальности failed; fix-волна — 4 failed/1 passed (R2-пин тривиален на старом коде — узел шов не вызывал); ревьюер воспроизвёл bite независимо (мутация без фильтра → 3 содержательных падения).
- Отклонения от плана:
  - **Task 4 — инвариант по N4, не «кап ≥ худший путь + запас»**: architect (consult, ruling E6a) — инвариант считается по достижимым путям: норма+волна+запас; шторм-пути (21 done / 25 degrade) превышают кап НАМЕРЕННО (честный budget_stop). Sketch-«11» из консультации невоспроизводим расчётом (артефакт сложения несовместимых веток) — в ADR записаны вычисленные значения. Кап 8 не менялся.
  - **R1 — пункт вне буквы плана**: пустая запись каталога при '*'-праве (GRANT-дрейф) ≠ «колонки нет» — решено ruling ARCH-GATE R1 ДО кода: ValueError → дефект стыка (degrade БЕЗ retry, `validate_sql.unavailable`), не hallucination-цикл.
  - **Правило A5 refined**: план «'*' разрешает только существующие; остальное — hallucination» уточнено: каталог опровергает кандидата; hallucination только когда ВСЕ кандидаты опровергнуты; есть неопровергнутый кандидат → denied (консервативное смешение) — иначе ломался корпус mixed-кандидатов и зацикливался retry ложным «несуществует».
  - **2 пина двухканальности сверх плана** (execute_sql_node/generate_sql): bite-прогон выявил, что A7-контракт узлов не был запинен юнит-тестами (план полагался на живой прогон) — добавлены в волну.
  - **hw-05/README.md** не был в перечне Task 7 — постскриптум «до/после» добавлен осознанно (артефакт вехи описывает переснятые сцены).
  - **I-1 fix-волной** (по ревью): подсказка изначально строила раздел из сырого каталога (таблицы плана гарантированы только на уровне таблиц) — колоночный грант фильтрует имена; принято предложение ревьюера целиком + специфичность источников цепочкой.
- Перенесено в другие сессии:
  - **BL-061** (P3) — кэш columns_catalog по счётчику permissions (отложено архитектором, revisit при нагрузке).
  - **BL-062** (P3) — явный список колонок не сверяется с каталогом (отложено архитектором; консервативный denied сохранён).
  - **BL-063** (P3) — capsys-пин stdout-политики `_run_fsm` (ревью-1 M-5; живой регресс Task 6 остаётся свидетельством).
  - **A6 (ребро перепланирования)** — S34 по плану батча, опирается на каталог колонок этой сессии.
  - Открытый крайний случай: clarify-путь 6+rest=9 > кап 8 — принят задокументированным (ruling E6a: гвард срезает честно), без BL.

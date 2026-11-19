# S21 — Консольная наблюдаемость CLI: этапы агента через порт Tracer (BL-004)

Этап: Наблюдаемость (вне ДЗ-вех, BL-004) | Владелец: агент | Зависимости: S03, S13 | Оценка: 0.5 | Статус: ◐ в работе
Фичи: — (сессия без фичи, states фич не меняются — прецедент S20)
Роль по фиче: —

## ⚠️ Прочитай перед началом

Исполнитель работает после S13 (тул-контур CLI: `apps/cli.py` — function-calling
цикл) и D-021 (провайдер уже эмитит `llm.*`-события через порт `Tracer` в JSONL).
Задача — сделать ход работы агента видимым в консоли: вызовы модели, вызовы тулa
`run_sql` с SQL, **результат с данными ≤5 строк**, ретраи, деградацию, события
шлюза (ретраи/breaker/бюджет).

**Решение (D-022):** этапы агента — события namespace `agent.*` через существующий
порт `Tracer` (`record(Trace(event, payload))`); консоль — новый адаптер
`ConsoleTracer` в infrastructure (рендер в stderr). Никаких print-ов в цикле:
OTel-адаптер позже переиспользует те же события. Эскалации M1 (S09/S10/S14)
появятся на консоли автоматически, когда появятся в коде — события generic.

**Сознательно НЕ делается:** потоковый вывод токенов (S19, стриминг); вывод
Observation в контекст модели не меняется (OBSERVATION_MAX_ROWS=20 остаётся);
цвета/ncurses (Windows-консоль, plain text); редактирование `llm.*`-событий
провайдера.

### Правила для исполнителя

1. Этапы — ТОЛЬКО в stderr (`print(..., flush=True)`); stdout остаётся чистым
   для ответа (пайпинг ответа не должен ломаться).
2. Наблюдение на консоли — данные оператора-человека: строки показываем (≤5),
   но в контекст модели они не попадают и не дублируются (S6 не затрагивается).
3. Все лимиты дисплея — именованные константы модуля (правило кода AGENTS.md).
4. LLM в тестах — только моки (S00 правило 7); ConsoleTracer тестируется через
   capsys.
5. События `agent.*` несут `trace_id` из RunContext (корреляция §3 observability).

## Предусловия

- [ ] S03 закрыта: `make check` → зелёный (провайдер + трейсер в наборе)
- [ ] S13 закрыта: `uv run pytest src/tests/unit/test_tool_loop.py -q` → 17 passed
- [ ] `make check` зелёный (baseline репозитория)
- [ ] Зависимостей-фич нет (сессия без фичи); ветка `s21-cli-stage-observability` от `dev`

## Задачи

### Task 1: Адаптеры наблюдаемости — ConsoleTracer + TeeTracer

Файлы: `src/text_to_sql_agent/infrastructure/observability/console_tracer.py`
(новый), `.../composite_tracer.py` (новый).

- `ConsoleTracer` — рендер событий в stderr, `flush=True`, plain text без цветов:
  - `agent.start` → `▶ Вопрос: <вопрос>`;
  - `agent.round` → `─ Ход N`;
  - `llm.complete` → `· <model> · <in>→<out> ток · <latency>с · <cost>₽`
    + ` [ответ из reasoning]` при `text_from_reasoning=True`;
  - `agent.tool_call` → `→ run_sql: <SQL одной строкой, ≤120 симв.>`;
  - `agent.observation` ok → `← <row_count> строк (усечён: да/нет)` + таблица:
    строка заголовка и **до 5 строк данных**, вид `│ v │ v │`, ячейка ≤20 симв.,
    ширина колонки ≤24; ошибка → `← ОШИБКА <error_class>: <message ≤120>`;
  - `agent.empty_retry` → `! пустой ответ — повтор (осталось N)`;
  - `agent.cap_exhausted` → `… кап ходов — финальный вызов без тулов`;
  - `llm.retry` / `llm.timeout` / `llm.reject` / `llm.breaker_open` /
    `llm.budget_stop` → короткие `!`-строки с ключевыми полями payload;
  - `agent.degrade` → `✗ деградация: <причина>`; `agent.answer` → `✓ ответ (<источник>)`;
  - неизвестное событие → generic `· <event>` (устойчивость к будущим `agent.*`).
  Все лимиты дисплея — именованные константы модуля.
- `TeeTracer(*tracers)` — fan-out во все трейсеры; сбой одного не рвёт остальные
  (best-effort, как JsonlTracer: except Exception → warnings.warn).

**✅ Самопроверка:**

```bash
uv run python -c "from text_to_sql_agent.infrastructure.observability.console_tracer import ConsoleTracer; from text_to_sql_agent.infrastructure.observability.composite_tracer import TeeTracer"  # импорты без ошибок
```

---

### Task 2: События `agent.*` в цикле + флаг `--quiet`

Файл: `src/apps/cli.py`.

- `run_tool_loop(..., tracer: Tracer | None = None)` (и `_run_loop`) — опциональный
  параметр; существующие вызовы совместимы (tracer=None — поведение прежнее).
- Эмись событий в точках цикла: start (вопрос + `trace_id`), round перед каждым
  `llm.complete` (порядковый номер вызова), tool_call (имя + SQL из arguments),
  observation (ok: `columns` + `rows_preview` = первые 5 строк + `row_count`,
  `truncated`; ошибка: `error_class` + `error`), empty_retry (остаток), cap_exhausted,
  degrade (обе ветки: пустые ответы и LLMProviderError), answer (источник:
  `text` / `final_no_tools` / `degraded` / `gateway_degraded`).
- `main()`: argparse `--quiet` — глушит консольные этапы (`console_trace=False`);
  по умолчанию этапы включены. Ответ по-прежнему только на stdout.

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_tool_loop.py -q   # прежние 17 passed (совместимость)
```

---

### Task 3: Сборка — build_container(console_trace=...)

Файл: `src/text_to_sql_agent/composition/container.py`.

- `build_container(settings=None, *, console_trace: bool = False)`: при True —
  `tracer = TeeTracer(JsonlTracer(settings.trace_jsonl_path), ConsoleTracer())`,
  иначе как прежде; инжект в провайдер и `Container.tracer` — один инстанс на всё.
- `apps/cli.py` импортирует только composition (граф зависимостей не расширяется).

**✅ Самопроверка:**

```bash
uv run python -c "from text_to_sql_agent.composition.container import build_container; import inspect; assert 'console_trace' in inspect.signature(build_container).parameters"  # параметр на месте
```

---

### Task 4: Тесты

Файлы: `src/tests/unit/fakes.py` (+ `RecordingTracer`), `src/tests/unit/test_tool_loop.py`
(сценарии событий), `src/tests/unit/test_console_tracer.py` (новый).

- `RecordingTracer` — собирает список `Trace` (unit-двойник порта).
- `test_tool_loop.py`: последовательность событий сценариев — tool-call-ход
  (start→round→tool_call→observation→round→answer) с payload (sql, columns,
  rows_preview ≤5, row_count, truncated); error-наблюдение (error_class);
  empty-retry; cap_exhausted + final_no_tools; деградация CircuitOpenLLM.
- `test_console_tracer.py`: точные строки рендера (capsys, stderr); таблица 5
  строк и обрезка: >5 строк → ровно 5 + «…»-поведение; ячейка ≤20; SQL ≤120;
  generic для неизвестного события; TeeTracer fan-out в оба и устойчивость к
  сбою одного.

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_tool_loop.py src/tests/unit/test_console_tracer.py -q   # все зелёные
```

---

### Task 5: Документация

- `docs/observability.md` — таблица событий `agent.*` (event → payload), раздел про
  ConsoleTracer/TeeTracer, правило «этапы — stderr, stdout — только ответ».
- `hw-02/README.md` («Прогон») — пример вывода с этапами и данными ≤5 строк.
- `DECISIONS.md` — D-022 (решение, контекст=BL-004, альтернативы: print-ы в цикле —
  отвергнуто; отдельный stdout-канал логов — отвергнуто; последствия: эскалации
  M1/S09/S10/S14 появятся автоматически; ARCH-GATE N/A-обоснование).
- `docs/quality-document.md` — строка `apps/cli` дополнена наблюдаемостью.

**✅ Самопроверка:**

```bash
grep -c "agent\." docs/observability.md   # таблица agent.* присутствует
grep -c "D-022" DECISIONS.md              # решение записано
```

## Критерии приёмки

- [ ] `make check` зелёный (все тесты, включая новые)
- [ ] Живой прогон `uv run python -m apps.cli "Сколько заказов у клиентов из Москвы?"`: видны ходы, вызовы модели с токами/латентностью, SQL, ошибка syntax, проба схемы с данными (≤5 строк), финальный ответ
- [ ] `--quiet` глушит этапы; stdout = только ответ (пайпинг `| tail -1` отдаёт ответ)
- [ ] `.harness/traces/trace.jsonl` содержит `agent.*`-события с `trace_id` и `rows_preview`
- [ ] Фичи закрыты или переданы: сессия без фичи — states `feature_list.json` не менялись (сверка diff)

## Ловушки

1. Этапы в stdout сломают пайпинг ответа — только stderr; проверять `| tail -1`.
2. verify-feature требует state=active: забытая активация фичи на старте первой сессии по ней → `BLOCKED` на закрытии (состояния не перескакивают); активируйте фичу в первый же день, а не перед коммитом (в этой сессии фичи нет — не применимо, но правило помним)
3. Промежуточная сессия многосессионной фичи: без allowlist (`bash scripts/clean-state-check.sh . <id>`) чистое состояние будет FAIL — фича сознательно остаётся `active`; не потеряйте передачу следующей сессии через `progress.md` и «Текущее состояние» индекса (здесь фичи нет — clean-check должен пройти без allowlist)
4. Гейты субагентов — механическая проверка: `guard-subagents.sh` требует заполненные строки ARCH-GATE/REVIEW-GATE при `make clean-check` / `make session-end` / `make verify-feature`; не удаляйте раздел и не переименовывайте маркеры — незаполненный гейт = FAIL (намеренно); N/A с внятной причиной — легальный способ учесть гейт без вызова
5. `rows_preview` в событии — это данные БД в JSONL-трейсе: файл локальный (`.harness/`), в git не попадает; но не класть в payload больше 5 строк — консоль и трейс не должны раздуваться

## Гейты субагентов (заполняется по факту, до закрытия сессии)

- ARCH-GATE: N/A — новый адаптер существующего порта Tracer (infrastructure реализует порт; apps импортирует только composition), новых зависимостей и пересечений границ модулей нет; обоснование в D-022
- REVIEW-GATE: [заполняется по факту]

## Отчёт по сессии (заполняется по факту)

- Сделано:
- Отклонения от плана:
- Перенесено в другие сессии:

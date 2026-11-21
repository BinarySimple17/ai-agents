# S15 — compact_result + interpret_result + degrade + BudgetGuard (веха V4, ДЗ-05)

Этап: Веха V4 (ДЗ-05: финальные узлы FSM — объяснение и честный отказ) | Владелец: агент | Зависимости: S05, S14 | Оценка: 1 | Статус: [ ] не начата
Фичи: feat-015 — interpret_result + degrade; зависимости feat-005 (закрывается S05), feat-014 (закрывается S14)
Роль по фиче: закрывает feat-015 — финальная сессия, через make verify-feature

## ⚠️ Прочитай перед началом

Исполнитель: ИИ-агент. К моменту старта: S05 закрыта (permissions — для degrade-словаря
«что доступно взамен»), S14 закрыта (execute_sql пишет rows в state; retry-цикл и probe
живы), S03 закрыта (бюджетный аккумулятор best-effort + шов budget_remaining_roubles —
S09/S10 использовали). Эта сессия закрывает прогон: `compact_result` (код) сворачивает
строки в компактный вид, `interpret_result` (glm-5.3-prime, strong) пишет финальный
текст: SQL + допущения (usage_notes рёбер из JoinPlan) + флаги усечения/approximate +
стоимость. Degrade — отказ по правам бизнес-понятиями. Здесь же BudgetGuard становится
полноценным портом (S00: порт вводит S15) — оркестраторный учёт ₽ по прогону,
заменяющий шов S09/S10.

Границы скоупа: БЕЗ approximate/matview-механики (T2 — feat-012/017, выпускная;
флаг approximate в payload готов, но появляется только у выпускной), БЕЗ matview/async
исходов, БЕЗ Streamlit-формата (двухслойные форматы ADR-008 — выпускная), БЕЗ
eval-метрик (ДЗ-06).

### Правила для исполнителя

1. Ветка `feat-015-interpret-result` от `milestone/v4` (D-031); merge — в `milestone/v4`.
2. Вход interpret_result — ТОЛЬКО компактный вид от compact_result (row_count +
   ≤ 20 строк + агрегаты + флаги, ~1150 токенов): сырые строки в промпт не попадают
   (behavior фичи); лимиты — именованные константы с комментарием.
3. «Неполное не выдаётся за полное» (state-agent v1.4, контракт budget_stop): частичный
   результат — только с явным incomplete-флагом в payload и тексте; бюджет исчерпан —
   честный отказ или incomplete, не «уверенное объяснение неполных цифр» (R-Q2-класс).
4. Degrade (S9 hw-01): код фиксирует, чего не хватает (по permissions), LLM формулирует
   отказ бизнес-понятиями + доступный вариант; БЕЗ имён недоступных таблиц, БЕЗ
   подтверждения существования данных («данные премии недоступны», не «таблица
   bonuses скрыта» и не «в bonuses есть строки, но нельзя»).
5. BudgetGuard — порт (Protocol) + реализация на аккумуляторе S03: учёт ₽ по факту
   usage всех вызовов прогона, проверка до каждого LLM-вызова узла, incomplete при
   исчерпании; шов budget_remaining_roubles в S09/S10 переводится на порт (механика
   не меняется — меняется точка вызова).

## Предусловия

- [ ] Сессия СТАРТУЕТ: статус в шапке переведён в «◐ в работе» и ARCH-GATE резолвлен ДО кода (canon-check architect ✅ либо обоснованный N/A) — старт-гейт (`guard-subagents.sh --phase start`, D-027) проверяет только стартованные (◐) сессии; «[ ] не начата» — план, гейт её скипает
- [ ] S05, S14 закрыты: `uv run pytest src/tests/integration/test_resolve_access.py src/tests/unit/test_diagnose.py -q` → passed (feat-005, feat-014 passing)
- [ ] `make check` зелёный (baseline репозитория)
- [ ] feat-005, feat-014 в state=passing; feat-015 активирована в state=active до старта работ

## Задачи

### Task 1: compact_result — код

`application/services/compact_result_service.py` + VO ResultCompact (domain): из
rows → row_count, первые ≤ 20 строк (усечение с флагом truncated), агрегаты по
числовым колонкам (sum/avg/min/max — код), флаги state. Без LLM. Бюджет промпта —
константа; превышение — усечение с флагом, не «молча больше».

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_interpret.py -q   # компакция: 100 строк → row_count=100 + 20 строк + truncated; агрегаты верны
```

---

### Task 2: interpret_result — финальный текст (strong)

`application/services/interpret_result_service.py`: вход ResultCompact + sql +
assumptions (clarify) + usage_notes рёбер (JoinPlan) + стоимость (BudgetGuard);
strong-роль; в ответе обязательны: текст, SQL, список допущений (usage_notes
раскрываются человеческим языком — флагман артефакта ДЗ-04 продолжает жить),
флаги усечения/approximate/incomplete, стоимость. pydantic-выход (ADR-008),
температура 0.

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_interpret.py -q   # состав ответа на моках; usage_notes в допущениях; флаги отражены
```

---

### Task 3: degrade + incomplete

Degrade-узел: вход — вердикт причин (от S11/S05); выход — отказ бизнес-понятиями +
доступный вариант (код подбирает по permissions, LLM формулирует). Инварианты
текстов: без имён недоступных таблиц, без подтверждения существования — adversarial-тесты
на формулировки (Ловушка-2). Incomplete: BudgetGuard-исчерпание → частичный ответ
только с флагом; finish_reason=length с текстом → incomplete (хвост S19), не «как есть».

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_interpret.py -q   # deny-сцена «премии»: нет имён таблиц/подтверждений; budget_stop → incomplete-флаг
```

---

### Task 4: BudgetGuard — порт + перевод швов

`application/ports/budget_guard.py` (Protocol) + реализация поверх аккумулятора S03
(reset на старте прогона — уже делает S04): check-before-call для всех узлов с LLM,
расход по usage (usage_missing → консервативная оценка, прецедент S03), остаток с
резервом 0,6 ₽ для эскалаций. Перевод вызовов budget_remaining_roubles (S09/S10)
на порт; поведение тестов не меняется (рефакторинг точки вызова, не механики).

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_interpret.py src/tests/unit/test_plan_joins.py src/tests/unit/test_generate_sql.py -q   # шов заменён, поведение зелёное
```

---

### Task 5: Подключение в FSM — терминалы done/degrade

Наполнение заглушек: compact_result → interpret_result → done-терминал; degrade-узел →
done-терминал (деградация — валидный исход с payload); budget_stop-терминал с
incomplete. Полный happy-path через Container на моках: вопрос → … → ответ со
стоимостью (уже близко к сборке S27 — здесь на фейках).

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_graph.py src/tests/unit/test_interpret.py -q   # done/degrade/budget_stop терминалы с корректным payload
```

## Критерии приёмки

- [ ] Успех: текст + SQL + допущения + флаги усечения/approximate + стоимость; вход interpret — компактный вид ~1150 токенов, не сырые строки (behavior feat-015)
- [ ] Отказ по правам: «данные премии недоступны» + доступный вариант, без имён недоступных таблиц и без подтверждения наличия данных (behavior feat-015)
- [ ] Бюджет исчерпан: честный отказ или incomplete-флаг; частичный результат — только с incomplete (behavior feat-015, контракт budget_stop)
- [ ] BudgetGuard — порт из S00; шов budget_remaining_roubles переведён; резерв 0,6 ₽ сохранён (D-016)
- [ ] usage_notes рёбер раскрываются в допущениях ответа (преемственность флагмана ДЗ-04)
- [ ] Фича закрыта: `make verify-feature F=feat-015` → PASS, state=passing с evidence

## Ловушки

1. **Adversarial-тесты формулировок — не формальность:** модель любит «таблица
   bonuses защищена» — прогоняйте тексты deny-ответов через запрещённые маркеры
   (имена таблиц из недоступного множества, слова «существует/есть строки») —
   тест-словарь, а не глазами.
2. Стоимость — из usage фактических вызовов прогона (BudgetGuard), не пересчёт по
   матрице; usage_missing (локальный шлюз) → консервативная оценка с пометкой, не 0 ₽.
3. Компактный вид: усечение строк молча = ложный ответ; флаг truncated обязателен
   и в payload, и в тексте ответа (R-Q2-класс: неполное не выдаётся за полное).
4. interpret_result не видит сырой SQL-ошибки и стектрейсов — только свои входы;
   любое «объяснение» причин сбоя — из state-вердиктов, не из исключений.
5. strong-роль — самая дорогая: BudgetGuard-проверка ДО вызова interpret обязательна
   (иначе последняя миля пробивает кап); тест на «остатка нет → отказ без вызова».
6. Гейт strict: строки ARCH/REVIEW — по факту; «N/A — сессия не начата» = FAIL.

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

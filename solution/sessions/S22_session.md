# S22 — Харнесс: неизбежные гейты сессии (start-фаза guard-subagents.sh)

Этап: harness | Владелец: ИИ-агент (куратор — пользователь) | Зависимости: S00 | Оценка: 0.5 | Статус: [x] ✅ 2026-11-19
Фичи: — (harness-сессия, states фич feature_list.json не меняются)
Роль по фиче: — (без фичи; гейты ARCH-GATE/REVIEW-GATE обязательны как для любой сессии)

## ⚠️ Прочитай перед началом

Запрос владельца: «усилить харнесс в части проверки файла сессии, старта сессии и завершения
сессии — скриптом проверять, что в файле сессии присутствует чекбокс архитектурного контроля
перед стартом и ревью после окончания; при старте проверять консистентность файла сессии и
добавлять недостающие чекбоксы; архитектурный контроль пред стартом и ревью в конце должны
быть неизбежными».

Проблема (состояние до): гейты ARCH-GATE/REVIEW-GATE проверяются только на финале
(`make clean-check` / `session-end` / `verify-feature` → `guard-subagents.sh`); на старте
файл сессии не проверяет никто (`init.sh` о сессиях не знает, `make session-start` — echo).
Дыры: ARCH-GATE можно не заполнять до самого конца (canon-check «задним числом»); раздел
гейтов в файле сессии может отсутствовать; работа вовсе без файла сессии проходит
clean-check (guard делает fallback на последнюю архивную сессию с заполненными гейтами).

Что сознательно НЕ делается: полная проверка «каждый коммит ↔ сессия» (linked commit ↔ session)
— вне скоупа; поведение `verify-feature.sh` не меняется (guard в strict end-фазе как был);
end-фаза guard'а не мутирует файл сессии (автодобавление — только в start-фазе).

### Правила для исполнителя

1. Все пути к сессиям в guard-subagents.sh — через `SESSIONS_DIR="${SESSIONS_DIR:-solution/sessions}"` (тестируемость); self-test использует фикс турный `SESSIONS_DIR`.
2. Обратная совместимость вызовов обязательна: `bash scripts/guard-subagents.sh`, `bash scripts/guard-subagents.sh "" feat-XXX` ведут себя как раньше (дефолтная фаза — `end`).
3. Свежий клон без активной сессии должен оставаться зелёным (`./init.sh` — feat-001 verification): старт-фаза без файла сессии — WARN, exit 0.
4. Self-test покрывает все кейсы из Критериев приёмки (изначально 10, расширены до 17 по ревью-1 — см. Отчёт/Отклонения); добавление кейса = правка этого файла в том же коммите.

## Предусловия

- [x] `make check` зелёный на ветке (baseline): `git switch dev && bash init.sh` → exit 0 (проверено 2026-11-19 до ветвления)
- [x] Карты владения соблюдены: `scripts/**`, `init.sh`, `Makefile`, `AGENTS.md` — harness-зона, правка согласована с куратором (запрос владельца)
- [x] Guard-subagents.sh прочитан целиком; точки интеграции: `init.sh`, `Makefile` (session-start/session-end/clean-check), `verify-feature.sh` (strict), `archive.sh` (архивация), TEMPLATE (шаблон раздела)

## Задачи

### Task 1: guard-subagents.sh — фазы start/end + автодобавление чекбоксов

- `--phase start|end` (дефолт `end`); парсинг до позиционных аргументов.
- `SESSIONS_DIR="${SESSIONS_DIR:-solution/sessions}"` для всех поисков (корень + `archive/`).
- Start-фаза: поиск сессии только в корне (без archive-fallback); нет файла → WARN + подсказка «создайте из TEMPLATE», exit 0 (рестартуемость свежего клона); файл помечен закрытым (`Статус:.*✅` в первых 20 строках), но лежит в корне → WARN «make archive».
- Start-фаза, самовосстановление (идемпотентно, каждая вставка репортится `[FIX]`): нет раздела `^## Гейты субагентов` → вставить блок (перед `## Отчёт по сессии`, иначе в конец); нет строк `- ARCH-GATE:` / `- REVIEW-GATE:` → дописать канонические плейсхолдеры (ARCH перед REVIEW).
- Start-фаза, проверки: ARCH-GATE — жёсткая (`check_gate`, плейсхолдер/отсутствие = FAIL → make check красный до canon-check ✅/N/A); REVIEW-GATE — мягкая (soft-режим check_gate: `[WARN]`, FAIL не инкрементируется).
- End-фаза: поведение без изменений, мутаций нет.

**✅ Самопроверка:**

```bash
bash scripts/guard-subagents.sh --phase start   # на S22 с N/A-ARCH: OK, exit 0
bash -n scripts/guard-subagents.sh               # синтаксис
```

### Task 2: self-test scripts/tests/test-guard-subagents.sh

Фикстурный bash-тест (mktemp-дерево + `SESSIONS_DIR`): 17 кейсов / 53 assert-а (изначально 10, расширены по ревью-1) — start без сессии (exit 0, WARN); start без раздела → автодобавление + exit 1 + дословное равенство плейсхолдеров TEMPLATE; идемпотентность; N/A-ARCH + плейсхолдер-REVIEW → exit 0; ✅/✅+SHA → exit 0; незаполненный ARCH → exit 1; end-фаза: дефолт-фаза ✅/✅+SHA → exit 0, плейсхолдер и ✅-без-SHA → exit 1, без мутаций; strict «не начата» → exit 1; закрытая-в-корне → WARN, exit 0; ветки самовосстановления 11–14; `--phase bogus` → exit 2; start+feature без фичевой сессии → WARN без auto-detect; «<» в легальной строке не красит.

**✅ Самопроверка:**

```bash
bash scripts/tests/test-guard-subagents.sh   # все кейсы зелёные, exit 0
```

### Task 3: init.sh + Makefile — гейт старта исполняемым

- `init.sh`: после гигиены git-хуков — `bash scripts/guard-subagents.sh --phase start` (fail-fast: exit 1 гейта красит `make check` ДО uv-шагов); после Python-верификации — `bash scripts/tests/test-guard-subagents.sh` (guard, от которого зависит make check, верифицируется на каждом прогоне).
- `Makefile`: `session-start` — echo-ритуал (добавить шаг «создать/проверить файл сессии») + вызов `bash scripts/guard-subagents.sh --phase start`.

**✅ Самопроверка:**

```bash
bash init.sh          # старт-гейт + self-test + uv-верификация, exit 0
bash -n init.sh && make -n session-start   # синтаксис/рецепт
```

### Task 4: доки в том же коммите

- `AGENTS.md`: Startup Workflow шаг 4 — init.sh включает гейт старта (консистентность + автодобавление + красный make check без ARCH-GATE); шаг 8 — пометка «guard-enforced на make check (start-фаза)».
- `solution/sessions/TEMPLATE_session.md`: раздел гейтов — «ARCH-GATE — до старта работ (make check красный до заполнения), REVIEW-GATE — до закрытия сессии»; ловушка №4 — про `--phase start`.
- `scripts/README.md`: фазы гварда, автодобавление, self-test.
- `DECISIONS.md`: D-023 (фазовый гейт сессии; альтернативы: WARN-only на старте — отвергнуто как неизбежность; отдельный make session-start без init.sh — отвергнуто: его можно не запустить).

**✅ Самопроверка:**

```bash
grep -n "phase start" AGENTS.md scripts/README.md Makefile init.sh   # провода на месте
grep -n "D-023" DECISIONS.md                                          # решение записано
```

### Task 5: финальная верификация + REVIEW-GATE

- Живой прогон сценариев на временной копии (Layer 3): файл без раздела → `--phase start` дозаполняет и красный → заполняю ARCH → зелёный.
- Отрицательный контроль: временно очистить ARCH-GATE в S22 → `make check` обязан упасть; вернуть.
- code-reviewer по диапазону merge-base…HEAD; Critical → регрессионные тесты; вердикт в REVIEW-GATE.
- progress.md, статусы (шапка S22 + реестр + «Текущее состояние»), `make archive`, `make clean-check`, коммит.

**✅ Самопроверка:**

```bash
make check        # зелёный (включая старт-гейт и self-test)
make clean-check  # зелёный (end-гейт: обе строки заполнены)
```

## Критерии приёмки

- [x] `bash scripts/tests/test-guard-subagents.sh` — все кейсы зелёные (53 assert-а, 17 кейсов, Layer 2 гварда)
- [x] `make check` зелёный: старт-гейт против S22 с N/A ARCH-GATE + self-test + 145 passed
- [x] Отрицательный контроль: ARCH-GATE с незаполненной строкой → старт-гейт FAIL (exit 1) с ритуалом canon-check; файл восстановлен
- [x] Автодобавление: файл сессии без раздела/строк дозаполняется `[FIX]`-вставками идемпотентно (self-test кейсы 2/3/11–14)
- [x] Свежий клон (нет активной сессии): `./init.sh` зелёный с WARN (self-test кейс 1)
- [x] End-фаза без мутаций (md5-контроль, кейс 8) и без изменений поведения (кейсы 7/8b/9)
- [x] Доки синхронны (AGENTS.md, TEMPLATE, scripts/README.md, D-023, quality-document) — в том же коммите
- [x] Фичи закрыты или переданы: — (harness-сессия, states фич не менялись; `feature_list.json` не тронут)

## Ловушки

1. CRLF на Windows: `*.md` нормализуются `.gitattributes` в LF в репо, но рабочий файл может быть CRLF — grep-проверки по префиксу строки (`^- ARCH-GATE:`) устойчивы (`\r` в конце строки), не «чинить» лишним sed. `[FIX]`-вставки гварда пишутся LF-строками в CRLF-файл → смешанные EOL — не дефект: git нормализует при коммите (ревью S22 Minor-11).
2. `set -e` в init.sh: exit 1 гварда роняет init.sh сразу — это задумано (fail-fast), сообщение гварда обязано объяснять ритуал (canon-check → ✅/N/A → make check зеленеет).
3. awk-вставка блока перед `## Отчёт по сессии`: `-v`-переменные awk интерпретируют `\n` в значениях — блок не должен содержать бэкслеши.
4. Гейты субагентов — механическая проверка: `guard-subagents.sh` теперь двухфазный; `--phase start` (из init.sh/make session-start) требует заполненный ARCH-GATE ДО старта работ и сам дозаполняет недостающие чекбоксы; end-фаза (clean-check/session-end/verify-feature) — как раньше; не удаляйте раздел и не переименовывайте маркеры.

## Гейты субагентов (заполняется по факту, до закрытия сессии)

<!-- Обязательный раздел: bash scripts/guard-subagents.sh — start-фаза (make check /
     make session-start) требует заполненный ARCH-GATE ДО старта работ и дозаполняет
     недостающие строки; end-фаза (make clean-check / make session-end / make verify-feature)
     проверяет обе строки. Каждая строка заменяется целиком на ОДИН вариант: ✅ + evidence,
     либо N/A — причина. Критерии N/A — AGENTS.md. Диспетчеризация: architect canon-check —
     план сессии до старта; code-reviewer — диапазон BASE..HEAD после зелёного make check. -->

- ARCH-GATE: N/A — harness-сессия: скрипты `scripts/`, `init.sh`, `Makefile` и процессные доки (AGENTS.md/TEMPLATE/scripts/README.md); границы модулей продукта, зависимости, схема/ER, дизайн-решения (ADR-объём) не затрагиваются — старт-гейт пройден обоснованным N/A до первого коммита кода
- REVIEW-GATE: ✅ Принято (code-reviewer, 75f297f..HEAD-коммит S22, ревью по рабочему дереву до коммита; ревью-1 «С оговорками» 0C/2I/9M → fix-волна: I1 start+feature без провала в auto-detect (WARN+exit 0, негативный кейс 16), I2 покрытие всех веток самовосстановления (кейсы 11–14), minors M3–M11 → scoped re-review «Принято» (оба Important закрыты; остатки R1–R8 — однострочная доксинхронизация, закрыта в этом же коммите; 53 assert-а / 17 кейсов)

## Отчёт по сессии (заполняется по факту)

- Сделано:
  - Task 1–2: `guard-subagents.sh` — двухфазный (start/end, дефолт end — обратная совместимость); `SESSIONS_DIR` для тестируемости; start-фаза: самовосстановление (`ensure_gate_section` — перед «Отчётом», `ensure_gate_lines` — ARCH до REVIEW; `[FIX]`-репорты), ARCH-GATE жёсткий, REVIEW-GATE мягкий (soft-режим `check_gate`), нет активной сессии — WARN exit 0, закрытая-в-корне — WARN «make archive»; end-фаза — прежнее поведение, без мутаций. Self-test `scripts/tests/test-guard-subagents.sh` — 17 кейсов / 53 assert-а (обе фазы, все ветки самовосстановления, идемпотентность, strict, совместимость, дословное равенство плейсхолдера TEMPLATE)
  - Task 3: `init.sh` — старт-гейт fail-fast после гигиены git-хуков (до uv-шагов); self-test после compileall. `Makefile` — `session-start` исполняемый (echo-ритуал с «make check» + start-гейт)
  - Task 4: AGENTS.md (Startup шаги 4/8), TEMPLATE (раздел гейтов двухфазный + Ловушки-4), scripts/README.md (фазы, self-test, примечание про глушение вывода clean-check'ом и смешанные EOL), DECISIONS.md D-023, quality-document (строка `harness/`)
  - Task 5: верификация — self-test 53/53; `make check` зелёный (старт-гейт + ruff + 145 passed + self-test); негативный контроль (незаполненный ARCH → FAIL с ритуалом, файл восстановлен); живой сценарий на фикстуре (без раздела → `[FIX]` → красный → заполнение → зелёный); REVIEW-GATE: ревью-1 «С оговорки» (0C/2I/9M) → fix-волна (I1: start+feature без провала в auto-detect + WARN; I2: кейсы 11–16 на все ветки самовосстановления; minors: exit 2 на битый `--phase`, плейсхолдер только «[»-форма, проверка записи, ограничение области мутации, константы порогов, «make check» в echo) → scoped re-review **«Принято»** (остатки R1–R8 — доксинхронизация, закрыта в этом же коммите)
- Отклонения от плана:
  - Self-test вырос с 10 до 17 кейсов (53 assert-а вместо ~30): ревью-1 (Important-2) показало непокрытость мутирующих веток `ensure_gate_lines` — добавлены кейсы 11–16 и 2b/17; осознанное усиление по находке ревью
  - `make session-start` оставлен вызовом гейта (не `bash init.sh`): ревью Minor-9 учтено возвратом «make check» в echo-ритуал — полная верификация остаётся на шаге 4 Startup Workflow
- Перенесено в другие сессии:
  - BL-006 (P3): полная механическая связка «коммит ↔ файл сессии» — дыра «работа вовсе без файла сессии» закрыта частично (start-WARN + end-fallback), D-023 это фиксирует; усиление (pre-commit ID сессии / clean-check против грязного worktree без сессии) — при росте команды

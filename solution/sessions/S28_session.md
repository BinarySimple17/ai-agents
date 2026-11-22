# S28 — harness: контекстная гигиена — скрипты ритуалов

Этап: harness (между вехами, по директиве владельца) | Владелец: агент | Зависимости: S22, S23 | Оценка: 0.5 | Статус: ◐ в работе
Фичи: — (сессия без фичи, states не меняются)
Роль по фиче: — (сессия без фичи, states не меняются)

## ⚠️ Прочитай перед началом

Исполнитель: ИИ-агент (куратор — пользователь). К моменту старта: S22/S23 закрыты
(гвард гейтов двухфазный, D-023/D-027), milestone/v4 — интеграционная ветка вехи V4.
Сессия снижает контекстные расходы агента (диагностика состава контекста: tool results
и thinking-масса доминируют): механика ритуалов — в скрипты, формат отчётов сабагентов —
в определения агентов, pytest-вывод — компактный по умолчанию.

Границы скоупа: БЕЗ правок product-кода `src/` (слои/порты/узлы не трогаются), БЕЗ
изменения семантики гейтов D-023/D-027, БЕЗ hooks-обрезки stdout (решение владельца),
reasoning level — настройка владельца в ZCode, вне репо.

### Правила для исполнителя

1. Скрипты ритуалов — fail-closed: любая pre-flight проверка не прошла → мутаций нет; сбой ПОСЛЕ мутаций (archive/commit) — восстановление вручную по docs/AGENT-OPERATIONS.md.
2. Self-test hook `CLOSE_SESSION_CLEANSTATE=0` — только для `scripts/tests/test-ritual-scripts.sh`; в ручных прогонах не задавать.
3. `*.sh`/Makefile — строго LF (`.gitattributes`); node-блоки скриптов — EOL-preserving по отношению к правимым файлам.

## Предусловия

- [x] S22/S23 закрыты: `ls solution/sessions/archive/ | grep -E "S22|S23"` → файлы в archive/
- [x] `make check` зелёный (baseline репозитория) → 625 passed
- [x] Сессия СТАРТУЕТ: статус в шапке → «◐ в работе», ARCH-GATE резолвлен ДО кода (см. «Отклонения от плана» — файл сессии создан post-factum по замечанию ревью)

## Задачи

### Task 1: pytest-вывод — компактный по умолчанию

pyproject `[tool.pytest.ini_options]`: `addopts = "--tb=short"`. Без `-q`: init.sh уже
передаёт `-q`, повторный `-q` кумулятивен (-qq глотает итоговую строку «N passed/skipped»,
на которой завязан test_join_graph_skip_guard — регрессия найдена прогоном и закрыта).

**✅ Самопроверка:**

```bash
make check   # EXIT=0, 625 passed; вывод тестов — компактный, итоговая строка на месте
```

### Task 2: формат отчётов сабагентов — evidence обязателен

`~/.zcode/agents/architect.md` и `~/.zcode/agents/code-reviewer.md`: находка =
`file:line` + цитата ≤ 1 строки; без evidence пункт не принят (урок S11: без цитат
главный агент перечитывал skeleton.py/ADR-005 после canon-check).

**✅ Самопроверка:**

```bash
grep -c "цитату" "C:/Users/Professional/.zcode/agents/architect.md"   # ≥ 1 (вне репо)
```

### Task 3: AGENTS.md ужат + docs/AGENT-OPERATIONS.md

Branch Hygiene и End of Session → полный объём в docs/AGENT-OPERATIONS.md; в AGENTS.md —
правила + ссылки. End of Session ведёт через `make close-session`. Синхрон ссылок:
guard-subagents.sh (без привязки к «шагу 8»), docs/README.md, docs/quality-document.md,
solution/README.md, Makefile (session-start), scripts/README.md.

**✅ Самопроверка:**

```bash
make check   # старт-гейт и self-test гварда зелёные после правки сообщений
```

### Task 4: скрипты ритуалов + self-test

`scripts/new-session.sh` (копия TEMPLATE + строка реестра, harness-конвенция «без фич»,
валидации S/TITLE) и `scripts/close-session.sh` (pre-flight fail-closed: гейты end-фазы,
отчёт, консистентность фич с ролью, грязь вне allowlist, clean-state → мутации → archive →
commit -F MSG) + Makefile targets + `scripts/tests/test-ritual-scripts.sh` (20 assert-ов,
worktree-песочница, подключён в init.sh).

**✅ Самопроверка:**

```bash
bash scripts/tests/test-ritual-scripts.sh   # 20 passed, 0 failed
make check                                  # EXIT=0
```

## Критерии приёмки

- [x] `make check` зелёный: 625 passed, self-test гварда 85 assert-ов, self-test ритуальных скриптов 20 assert-ов
- [x] Полный цикл close-session проверен end-to-end в одноразовом worktree: гейты → статус ◐→✅ → реестр → «Текущее состояние» (одна строка) → archive → clean-state 7/7 → commit
- [x] Fail-closed подтверждён: planned-сессия, грязь вне allowlist, красный clean-state, пустой MSG — отказы ДО мутаций (self-test + ручной прогон)
- [x] Ревью code-reviewer: 1C/7I закрыты фикс-волной с регрессионными кейсами в self-test (re-review — перед закрытием сессии, вердикт в REVIEW-GATE)
- [x] Фичи: сессия без фич — states feature_list.json не меняются (clean-check без allowlist)

## Ловушки

1. Повторный `-q` у pytest кумулятивен: `addopts = "-q"` + `-q` в init.sh = `-qq` — итоговая строка «N passed» исчезает, skip-гвард (парсит вывод) краснеет. Держать `--tb=short` в addopts, `-q` — только на CLI.
2. verify-feature требует state=active: забытая активация фичи на старте первой сессии по ней → `BLOCKED` на закрытии (состояния не перескакивают); активируйте фичу в первый же день, а не перед коммитом
3. Промежуточная сессия многосессионной фичи: без allowlist (`bash scripts/clean-state-check.sh . <id>`) чистое состояние будет FAIL — фича сознательно остаётся `active`; не потеряйте передачу следующей сессии через `progress.md` и «Текущее состояние» индекса
4. Гейты субагентов — механическая проверка, двухфазная (D-023/D-027): start-фаза `guard-subagents.sh` (из `make check`/`init.sh` и `make session-start`) кусает только сессию, которую СТАРТУЮТ — статус шапки «◐ в работе» (запланированные «[ ] не начата» скипаются, закрытые «✅» — напоминание про make archive); для стартованной — ARCH-GATE до кода: `make check` красный, пока строка не получит ✅ verdict или обоснованный N/A; «N/A — сессия не начата» у ◐ = FAIL (противоречит статусу); недостающие строки-чекбоксы она дописывает сама ([FIX]); end-фаза (из `make clean-check` / `make session-end` / `make verify-feature`) требует обе строки у закрываемой сессии. Не удаляйте раздел и не переименовывайте маркеры — незаполненный гейт = FAIL (намеренно); N/A с внятной причиной — легальный способ учесть гейт без вызова
5. Windows-консоль (cp866) коверкает русский вывод make-эхо — это отображение, файлы остаются UTF-8; проверять содержимое grep'ом, не глазами в терминале

## Гейты субагентов (заполняется по факту, до закрытия сессии)

- ARCH-GATE: N/A — harness-скрипты и docs: границы модулей продукта (src/ слои/порты/узлы), зависимости, схема/ER и дизайн-решения продукта не затрагиваются; новый тул-код — вне код-архитектуры (scripts/, прецедент S22/S23), решение владельца
- REVIEW-GATE: [✅ <Ready to merge? вердикт> + <BASE7>..<HEAD7> | N/A — <причина>]

## Отчёт по сессии (заполняется по факту)

- Сделано:
  - Task 1 — pyproject addopts `--tb=short` (без `-q`: кумулятивный -qq ломал skip-гвард — регрессия найдена тестом).
  - Task 2 — определения агентов (~/.zcode/agents/, вне репо): цитата ≤ 1 строки в находках, пункт без evidence не принят.
  - Task 3 — AGENTS.md ужат (Branch Hygiene, End of Session → docs/AGENT-OPERATIONS.md); синхрон ссылок: guard-subagents.sh, docs/README.md, docs/quality-document.md, solution/README.md, Makefile, scripts/README.md.
  - Task 4 — scripts/new-session.sh + scripts/close-session.sh (pre-flight fail-closed) + Makefile targets + scripts/tests/test-ritual-scripts.sh (20 assert-ов, в init.sh); BL-017 (EOL archive.sh — вне скоупа).
  - Верификация: make check EXIT=0 (625 passed + 85 + 20 assert-ов); e2e полного цикла close-session в worktree — чисто.
- Отклонения от плана:
  - Файл сессии создан post-factum (работа велась в консультационном контуре с владельцем, не в трекаемой сессии) — замечание ревью I7; ARCH-GATE зафиксирован задним числом как N/A с основанием (harness-скрипты, границы продукта не тронуты). Прецедент учтён: следующие harness-работы стартуют с файлом сессии ДО кода.
  - Механика End of Session переехала в make close-session в процессе сессии (изначально — ручные шаги) — осознанное расширение по директиве владельца «сделай всё».
- Перенесено в другие сессии:
  - BL-017 — EOL-унификация archive.sh (P3, косметика, решение владельца).

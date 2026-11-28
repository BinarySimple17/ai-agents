# S43 — harness: гигиена контекста (D-043) — чат на волну, целевые чтения

Этап: harness, между ДЗ-05 и ДЗ-06 (D-042, ветка от `inter/hw05-hw06`) | Владелец: агент (команда владельца 2026-11-28 «оформи») | Зависимости: S41 (план батча, правило веток D-042) | Оценка: 0.5 дня | Статус: ✅ 2026-11-28
Фичи: — (сессия без фичи, states не меняются)
Роль по фиче: — (сессия без фичи, states не меняются)

## ⚠️ Прочитай перед началом

Исполнитель: агент в новом чате (по правилу продолжения из Startup Workflow). Контекст:
аудит телеметрии ZCode 2026-11-28 показал, что расход лимита токенов ≈ (шаги × размер
контекста): марафон-чат 411 сообщений с ~300k контекста на хвосте сжёг 37.1M input за
день при 148k output; стартовый список чтения по ритуалу ≈375 КБ (~95k токенов) на каждый
новый чат, из них progress.md — 146 КБ. Владелец утвердил пакет правок операционных доков:
правило «чат на волну» + продолжение ◐-сессии без полного ритуала + сужение стартового
чтения progress.md + буллет гигиены в Working Rules + раздел с шаблонами диспетчеризации
в AGENT-OPERATIONS + решение D-043.

**Границы скоупа (сознательно НЕ делается):** код продукта и тесты не трогаются
(исключение — `scripts/audit-harness.sh`: диагностический скрипт аудита, синк текстов
проверок под новую структуру progress.md — расширение скоупа командой владельца
2026-11-28); `feature_list.json` не правится (states не меняются); ужимание AGENTS.md
целиком и чистка скилл-листа — отложены владельцу отдельно (D-043, alternatives);
`.docs/` READ ONLY (D-011) не затрагивается; merge S33 → `inter/hw05-hw06` и пуш
веток — только по команде владельца.

### Правила для исполнителя

1. Файлы сессии: `AGENTS.md`, `docs/AGENT-OPERATIONS.md`, `DECISIONS.md`,
   `solution/sessions.md` (граф зависимостей), `progress.md` (Task 4),
   `session-handoff.md` и `scripts/audit-harness.sh` (синк ссылок) — расширение
   скоупа командой владельца 2026-11-28. Остальное — вне скоупа.
2. AGENTS.md — системный промпт каждой будущей сессии: правки минимальные по объёму,
   каждый добавленный байт платится на каждом запросе; полный операционный объём —
   в AGENT-OPERATIONS (он не прикрепляется к контексту автоматически).
3. Маркеры гейтов (`ARCH-GATE:`, `REVIEW-GATE:`) и раздел «Гейты субагентов» не
   переименовывать — их парсит `guard-subagents.sh`.
4. Пуш и merge в `inter/hw05-hw06` — не выполнять (команда владельца, D-042/D-035).

## Предусловия

- [x] S33 закрыта; пересечение с файлами сессии уточнено (находка ревью I-2): AGENTS.md/AGENT-OPERATIONS.md — не пересекаются (`git diff --name-only inter/hw05-hw06 harness/retry-quality | grep -E "^AGENTS\.md$|AGENT-OPERATIONS"` → 0); progress.md и solution/sessions.md — ПЕРЕСЕКАЮТСЯ → конфликт при merge S33 в inter ожидаем, при резолве сохранить записи S33 (Current State/Notes/реестр); merge — отдельная команда владельца
- [x] База ветки: `git merge-base HEAD inter/hw05-hw06` → `48032c6` (S32 финализация; последняя полная верификация точки — make check EXIT=0, 905 passed, 4fe09d4)
- [x] Рабочее дерево чистое перед стартом: `git status --porcelain` → пусто (2026-11-28; после `make new-session` — только `solution/sessions/S43_session.md` + строка реестра)
- [x] `make check` зелёный (baseline репозитория): на точке базы 48032c6 верифицирован при закрытии S32 (4fe09d4); полный прогон на этой ветке — Task 4
- [x] Фичи-зависимости — N/A: сессия без фичи (states не меняются)
- [x] Сессия СТАРТУЕТ: статус в шапке переведён в «◐ в работе» и ARCH-GATE резолвлен ДО кода (обоснованный N/A ниже — операционные доки, архитектура не трогается) — старт-гейт (`guard-subagents.sh --phase start`, D-027) проверяет только стартованные (◐) сессии; «[ ] не начата» — план, гейт её скипает

## Задачи

### Task 1: AGENTS.md — правило продолжения, шаг 7, буллет гигиены

Что именно: (а) в Startup Workflow после строки «Before writing code…» — абзац
«Продолжение в новом чате (D-043)»; (б) шаг 7 — progress.md целиком (≈13 КБ после
ужатия Task 4); (в) Working Rules — буллет «Гигиена контекста
(D-043)» после «Leave clean state».

**✅ Самопроверка:**

```bash
grep -c "D-043" AGENTS.md                       # ≥ 3 (продолжение, шаг 7, Working Rules)
grep -n "Продолжение в новом чате" AGENTS.md    # 1 вхождение в Startup Workflow
grep -n "Notes for Next Session" AGENTS.md      # в шаге 7
```

### Task 2: docs/AGENT-OPERATIONS.md — раздел «Гигиена контекста» + шаблоны

Что именно: новый раздел перед «Скрипты ритуалов» — обоснование (аудит 2026-11-28),
6 правил, шаблон диспетчерского чата и шаблон промпта исполнителя.

**✅ Самопроверка:**

```bash
grep -n "Гигиена контекста и чат-диспетчеризация" docs/AGENT-OPERATIONS.md   # раздел есть
grep -c "^### Шаблон" docs/AGENT-OPERATIONS.md    # 2 (диспетчер, исполнитель)
```

### Task 3: DECISIONS.md D-043 + граф зависимостей реестра

**✅ Самопроверка:**

```bash
grep -n "^## D-043" DECISIONS.md                  # запись есть, после D-042
grep -n "S43" solution/sessions.md                # строка реестра + граф
```

### Task 4: progress.md — упразднение дублей (команда владельца 2026-11-28, расширение скоупа)

Что именно: секции What's Done / What's In Progress / Next Steps / Files Modified This
Session / Evidence of Completion удалены — дубли реестра `sessions.md`, плановых файлов
сессий, git-истории и evidence `feature_list.json` (146 → ~13 КБ); остаются Current
State, Blockers / Risks, Decisions Made, Notes for Next Session. Синк ссылок на секции:
AGENTS.md (Feature State Rules, DoD, Observability, End of Session), docs/AGENT-
OPERATIONS.md (End of Session), session-handoff.md, scripts/audit-harness.sh.

**✅ Самопроверка:**

```bash
grep -c "^## " progress.md    # 4 (Current State, Blockers, Decisions, Notes)
wc -c progress.md             # < 20000
grep -rn "Next Steps\|Evidence of Completion\|Files Modified" AGENTS.md docs/AGENT-OPERATIONS.md session-handoff.md scripts/audit-harness.sh progress.md | grep -v "Session ID:" | wc -l   # 0 живых ссылок (включая шапку progress.md — находка ревью I-1; исключение — строка цепочки Current State, историческая запись об упразднении, находка re-review-2: якорь на строку, не на свободный текст)
```

### Task 5: верификация, ревью, закрытие

**✅ Самопроверка:**

```bash
make check                                        # EXIT=0 (guard start-фазы + pytest)
# диспетчеризация code-reviewer по диапазону 48032c6..HEAD, фиксы, REVIEW-GATE
make close-session S=S43 MSG=<файл-сообщение>     # pre-flight + архив + коммит
```

## Критерии приёмки

- [x] AGENTS.md содержит правило «Продолжение в новом чате (D-043)», шаг 7 на ужатый progress.md и буллет «Гигиена контекста» в Working Rules
- [x] docs/AGENT-OPERATIONS.md содержит раздел «Гигиена контекста и чат-диспетчеризация (D-043)» с шаблонами диспетчера и исполнителя
- [x] DECISIONS.md содержит D-043 (decision + context + alternatives + consequences)
- [x] Граф зависимостей реестра дополнен (S43←S41, вне вех)
- [x] progress.md ужат до уникальных секций (Current State, Blockers/Risks, Decisions Made, Notes for Next Session), ссылки в AGENTS.md/AGENT-OPERATIONS/session-handoff/audit-harness.sh синхронизированы
- [x] `make check` EXIT=0 после правок; REVIEW-GATE ✅ по диапазону 48032c6..a41f4d3 (цепочка ревью ниже)
- [x] Фичи закрыты или переданы: — (сессия без фичи, states не меняются; рестартуемость — полный `./init.sh` внутри clean-state протокола close-session)

## Ловушки

1. Имя ветки `harness/context-hygiene` занято старой веткой S28 (469e6ca, полностью влита в `inter`, не удалена — решение владельца) → сессия живёт на `harness/token-hygiene`
2. verify-feature требует state=active: забытая активация фичи на старте первой сессии по ней → `BLOCKED` на закрытии (состояния не перескакивают) — N/A: сессия без фичи
3. AGENTS.md платится каждым запросом каждой будущей сессии — не раздувать правки; полный операционный объём выносить в docs/AGENT-OPERATIONS.md
4. Гейты субагентов — механическая проверка, двухфазная (D-023/D-027): start-фаза `guard-subagents.sh` (из `make check`/`init.sh` и `make session-start`) кусает только сессию, которую СТАРТУЮТ — статус шапки «◐ в работе» (запланированные «[ ] не начата» скипаются, закрытые «✅» — напоминание про make archive); для стартованной — ARCH-GATE до кода: `make check` красный, пока строка не получит ✅ verdict или обоснованный N/A; «N/A — сессия не начата» у ◐ = FAIL (противоречит статусу); недостающие строки-чекбоксы она дописывает сама ([FIX]); end-фаза (из `make clean-check` / `make session-end` / `make verify-feature`) требует обе строки у закрываемой сессии. Не удаляйте раздел и не переименовывайте маркеры — незаполненный гейт = FAIL (намеренно); N/A с внятной причиной — легальный способ учесть гейт без вызова

## Гейты субагентов (заполняется по факту, до закрытия сессии)

- ARCH-GATE: N/A — правятся только операционные доки (AGENTS.md Startup Workflow/Working Rules, docs/AGENT-OPERATIONS.md новый раздел, DECISIONS.md, граф реестра); границы модулей, зависимости, схема/ER, FSM и код продукта не затрагиваются (2026-11-28, до правок)
- REVIEW-GATE: ✅ Ready to merge? **Yes** (цепочка: ревью-1 `48032c6..e175846` «With fixes» 0C/2I/4M → fix-волна-1 `9ff2ec3` [I-1 шапка progress.md на уникальные секции + grep-расширение самопроверки; I-2 честная запись ожидаемого конфликта progress.md при merge S33→inter; M-1..M-4 кодом] → re-review-2 `9ff2ec3` «No» 0C/1I/1M [рассинхрон канона «целиком после ужатия» в 3 местах] → fix-волна-2 `a41f4d3` [заголовок D-043, п. (1) Decision, шаблон исполнителя; якорь исключения самопроверки на строку цепочки] → re-review-3 `9ff2ec3..a41f4d3` **«Yes»** 0C/0I/1M [минор-косметика → BL-064]; диапазон сессии `48032c6..a41f4d3`)

## Отчёт по сессии (заполняется по факту)

- Сделано:
  - Task 1–3 (волна `e175846`): D-043 гигиена контекста — AGENTS.md (абзац «Продолжение в новом чате» в Startup с оговоркой шагов 4/8, шаг 7, буллет «Гигиена контекста» в Working Rules + синк ссылок Feature State Rules/DoD/Observability/End of Session), docs/AGENT-OPERATIONS.md (новый раздел «Гигиена контекста и чат-диспетчеризация»: обоснование аудита 2026-11-28, 6 правил, шаблон диспетчерского чата и шаблон промпта исполнителя), DECISIONS.md D-043, реестр (строка S43 + граф S43←S41)
  - Task 4 (расширение скоупа командой владельца 2026-11-28): progress.md ужат **145 887 → 13 648 байт** — упразднены секции-дубли What's Done / What's In Progress / Next Steps / Files Modified This Session / Evidence of Completion (история — в git и файлах сессий; содержимое дублируется реестром, архивными файлами сессий и evidence feature_list.json); остались уникальные Current State / Blockers / Risks / Decisions Made / Notes for Next Session; синк ссылок: session-handoff.md, scripts/audit-harness.sh (3 текстовые проверки)
  - Task 5: make check EXIT=0 ×2 (824 passed / 81 skipped — честные скипы без Docker-инфры, сбор 905 = дерево inter@48032c6; Contracts 10 kept/0 broken; self-tests guard 85 + ritual 29); ревью-контур — цепочка в REVIEW-GATE выше (итог «Yes»)
- Отклонения от плана:
  - Скоуп расширен командой владельца в ходе сессии (реорганизация progress.md + session-handoff + audit-harness.sh) — зафиксировано в правилах сессии и D-043
  - Имя ветки `harness/context-hygiene` занято старой веткой S28 (влита в inter, не удалена — решение владельца) → сессия на `harness/token-hygiene`
  - Ревью-2 потребовало второй фикс-волны (унификация канона «progress.md целиком после ужатия» в 3 местах)
- Перенесено в другие сессии:
  - Minor re-review-3 (косметика перечня секций в AGENT-OPERATIONS:25, «не долг волны») → BL-064; ужимание AGENTS.md целиком и чистка скилл-листа — решение владельца отдельно (альтернатива (б) D-043); merge `harness/retry-quality` (S33) → inter, затем `harness/token-hygiene` (S43) и пуш — очередь владельца (конфликт progress.md ожидаем, резолв — сохранить записи S33)

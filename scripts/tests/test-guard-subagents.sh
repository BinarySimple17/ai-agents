#!/usr/bin/env bash
# scripts/tests/test-guard-subagents.sh — self-test гейта субагентов (фикстурные прогонки).
#
# Гвард scripts/guard-subagents.sh — гейт в цепочке make check (start-фаза из init.sh),
# поэтому он сам верифицируется на каждом прогоне: набор кейсов покрывает обе фазы,
# автодобавление чекбоксов, идемпотентность, строгий режим фичи и обратную совместимость.
#
# Кейсы:
#   1  start, активной сессии нет            → exit 0 + WARN (рестартуемость свежего клона)
#   2  start, файл без раздела               → автодобавление ([FIX]) + exit 1 (ARCH не заполнен)
#   2b плейсхолдер из автодобавления          → дословно равен строке TEMPLATE_session.md
#   3  идемпотентность: повторный start      → ровно один заголовок и по одной строке, exit 1
#   4  start: N/A-ARCH + плейсхолдер-REVIEW  → exit 0 (REVIEW — мягкий WARN)
#   5  start: ✅-ARCH + ✅+SHA-REVIEW        → exit 0
#   6  start: ARCH без ✅/N/A                → exit 1
#   7  end: ✅-ARCH + ✅+SHA-REVIEW          → exit 0 (дефолтная фаза = end, совместимость)
#   8  end: плейсхолдер-REVIEW               → exit 1; файл НЕ мутирован
#   8b end: ✅-REVIEW без SHA                → exit 1 (SHA диапазона обязателен)
#   9  strict (feature): N/A «не начата»     → exit 1
#   10 start: закрытая сессия в корне         → WARN «make archive», exit 0 (ARCH заполнен)
#   11 start: раздел есть, обеих строк нет    → вставка после заголовка (ARCH до REVIEW), exit 1
#   11b после заполнения ARCH                 → exit 0, дублей нет (повторный прогон)
#   12 start: только ARCH                     → REVIEW дописан после ARCH; заполнение → exit 0
#   13 start: только REVIEW                   → ARCH вставлен ПЕРЕД REVIEW, exit 1
#   14 start: нет раздела и нет «Отчёта»      → append-ветка: раздел в конец файла, exit 1
#   15 --phase bogus                          → exit 2 (ошибка вызова)
#   16 start + feature без фичевой сессии     → WARN, exit 0 (без провала в auto-detect, ревью S22 I1)
#   17 «<» в легально заполненной строке      → гейт не красят (плейсхолдер — только «[»-форма)
#   18 start: две запланированные («[ ] не начата») → exit 0, [SKIP] с именами (D-027: гейт — по стартованным)
#   19 start: ◐ настоящая + запланированная   → exit 0, «проверено: 1»
#   20 start: ◐ с ARCH «не начата»            → exit 1 (стартовала — обязана резолвить; дыра владельца)
#   21 start: только запланированные          → exit 0 («стартованных (◐) сессий нет»)
#   22 end без фичи: ◐ с «не начата»          → exit 0 (совместимость; strict — только при feature)
#   23 end: запланированная + ◐ заполненная   → exit 0, проверена только ◐
#   24 end: только запланированные            → WARN exit 0 («гейту закрытия нечего проверять»)
#
# Запуск: bash scripts/tests/test-guard-subagents.sh   (без внешних зависимостей)
# Exit: 0 — все кейсы зелёные, 1 — есть провалы.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GUARD="$ROOT/scripts/guard-subagents.sh"

TESTS=0
FAILED=0

assert_rc() { # <описание> <ожидаемый-exit> <фактический-exit>
  TESTS=$((TESTS + 1))
  if [ "$2" -eq "$3" ]; then
    echo "  [PASS] $1 (exit $3)"
  else
    echo "  [FAIL] $1 — ожидался exit $2, получен $3"; FAILED=$((FAILED + 1))
  fi
}

assert_out() { # <описание> <подстрока> <вывод>
  TESTS=$((TESTS + 1))
  if printf '%s' "$3" | grep -qF "$2"; then
    echo "  [PASS] $1"
  else
    echo "  [FAIL] $1 — в выводе нет: «$2»"; FAILED=$((FAILED + 1))
  fi
}

assert_count() { # <описание> <паттерн-egrep> <файл> <ожидаемое-число>
  TESTS=$((TESTS + 1))
  local n
  n="$(grep -cE "$2" "$3" || true)"
  if [ "$n" -eq "$4" ]; then
    echo "  [PASS] $1 ($n)"
  else
    echo "  [FAIL] $1 — ожидалось $4, найдено $n"; FAILED=$((FAILED + 1))
  fi
}

# fixtтура: минимальный файл сессии; $1 — путь, $2 — содержимое раздела гейтов (опционально)
make_session() {
  {
    echo "# S99 — Фикстурная сессия"
    echo ""
    echo "Этап: тест | Владелец: тест | Зависимости: — | Оценка: 0.1 | Статус: ◐ в работе"
    echo "Фичи: ${3:-—}"
    echo ""
    echo "## Задачи"
    echo ""
    echo "### Task 1: заглушка"
    echo ""
    echo "## Отчёт по сессии"
    echo ""
    echo "- Сделано: —"
    if [ -n "${2:-}" ]; then
      echo ""
      printf '%s\n' "$2"
    fi
  } > "$1"
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
SESS="$TMP/sessions"
mkdir -p "$SESS/archive"

run_start() { SESSIONS_DIR="$SESS" bash "$GUARD" --phase start 2>&1; }
run_end()   { SESSIONS_DIR="$SESS" bash "$GUARD" 2>&1; }

SECTION='## Гейты субагентов (заполняется по факту, до закрытия сессии)'
ARCH_OK='- ARCH-GATE: ✅ compliant — план канонен (architect, canon-check)'
REV_OK='- REVIEW-GATE: ✅ Принято (code-reviewer, abc1234..def5678)'
ARCH_NA='- ARCH-GATE: N/A — сессия не трогает архитектуру (док-правки)'
ARCH_BAD='- ARCH-GATE: (пусто — не заполнено)'
REV_PH='- REVIEW-GATE: [✅ <Ready to merge? вердикт> + <BASE7>..<HEAD7> | N/A — <причина>]'

echo "=== Self-test: guard-subagents.sh ==="

# --- кейс 1: start, активных сессий нет → WARN, exit 0 (рестартуемость) ---
out="$(run_start)"; rc=$?
assert_rc "1: start без активных сессий → exit 0" 0 "$rc"
assert_out "1: WARN о создании файла сессии" "[WARN] активные сессии не найдены" "$out"

# --- кейс 2: start, файл без раздела → автодобавление + exit 1 (ARCH не заполнен) ---
F="$SESS/S99_session.md"
make_session "$F"
out="$(run_start)"; rc=$?
assert_rc "2: start без раздела гейтов → exit 1" 1 "$rc"
assert_out "2: раздел дозаполнен ([FIX])" "[FIX] добавлен раздел" "$out"
assert_count "2: заголовков раздела — один" '^## Гейты субагентов' "$F" 1
assert_count "2: строк ARCH-GATE — одна" '^- ARCH-GATE:' "$F" 1
assert_count "2: строк REVIEW-GATE — одна" '^- REVIEW-GATE:' "$F" 1
# 2b: вставленный плейсхолдер дословно равен канону TEMPLATE (ревью S22 Minor-7; tr -d '\r' — CRLF)
tpl_arch="$(grep -E '^- ARCH-GATE:' "$ROOT/solution/sessions/TEMPLATE_session.md" | tr -d '\r')"
ins_arch="$(grep -E '^- ARCH-GATE:' "$F" | tr -d '\r')"
assert_rc "2b: плейсхолдер ARCH дословно равен TEMPLATE" 0 "$([ "$ins_arch" = "$tpl_arch" ] && echo 0 || echo 1)"
tpl_rev="$(grep -E '^- REVIEW-GATE:' "$ROOT/solution/sessions/TEMPLATE_session.md" | tr -d '\r')"
ins_rev="$(grep -E '^- REVIEW-GATE:' "$F" | tr -d '\r')"
assert_rc "2b: плейсхолдер REVIEW дословно равен TEMPLATE" 0 "$([ "$ins_rev" = "$tpl_rev" ] && echo 0 || echo 1)"

# --- кейс 3: идемпотентность — повторный start не плодит строки и [FIX] ---
out="$(run_start)"; rc=$?
assert_rc "3: повторный start → exit 1" 1 "$rc"
if printf '%s' "$out" | grep -qF "[FIX]"; then
  TESTS=$((TESTS + 1)); echo "  [FAIL] 3: повторные [FIX]-вставки"; FAILED=$((FAILED + 1))
else
  TESTS=$((TESTS + 1)); echo "  [PASS] 3: без повторных [FIX]"
fi
assert_count "3: заголовков по-прежнему один" '^## Гейты субагентов' "$F" 1
assert_count "3: строк ARCH-GATE по-прежнему одна" '^- ARCH-GATE:' "$F" 1
assert_count "3: строк REVIEW-GATE по-прежнему одна" '^- REVIEW-GATE:' "$F" 1

# --- кейс 4: start, N/A-ARCH + плейсхолдер-REVIEW → exit 0 (REVIEW мягкий) ---
make_session "$F" "$SECTION

$ARCH_NA
$REV_PH"
out="$(run_start)"; rc=$?
assert_rc "4: start N/A-ARCH + плейсхолдер-REVIEW → exit 0" 0 "$rc"
assert_out "4: REVIEW — мягкий WARN" "[WARN] REVIEW-GATE" "$out"
assert_out "4: итоговая строка старт-гейта" "Session start gate: OK" "$out"

# --- кейс 5: start, ✅-ARCH + ✅+SHA-REVIEW → exit 0 ---
make_session "$F" "$SECTION

$ARCH_OK
$REV_OK"
out="$(run_start)"; rc=$?
assert_rc "5: start ✅/✅+SHA → exit 0" 0 "$rc"
if printf '%s' "$out" | grep -qF "[WARN]"; then
  TESTS=$((TESTS + 1)); echo "  [FAIL] 5: лишний WARN при заполненных гейтах"; FAILED=$((FAILED + 1))
else
  TESTS=$((TESTS + 1)); echo "  [PASS] 5: без WARN при заполненных гейтах"
fi

# --- кейс 6: start, ARCH без ✅/N/A → exit 1 ---
make_session "$F" "$SECTION

$ARCH_BAD
$REV_PH"
out="$(run_start)"; rc=$?
assert_rc "6: start незаполненный ARCH → exit 1" 1 "$rc"
assert_out "6: подсказка про ритуал canon-check" "canon-check" "$out"

# --- кейс 7: end (дефолт, без --phase), ✅/✅+SHA → exit 0 ---
make_session "$F" "$SECTION

$ARCH_OK
$REV_OK"
out="$(run_end)"; rc=$?
assert_rc "7: end ✅/✅+SHA (дефолтная фаза) → exit 0" 0 "$rc"
assert_out "7: summary end-фазы" "Subagent gates: OK" "$out"

# --- кейс 8: end, плейсхолдер-REVIEW → exit 1, файл не мутирован ---
make_session "$F" "$SECTION

$ARCH_OK
$REV_PH"
before="$(md5sum "$F" | cut -d' ' -f1)"
out="$(run_end)"; rc=$?
assert_rc "8: end плейсхолдер-REVIEW → exit 1" 1 "$rc"
after="$(md5sum "$F" | cut -d' ' -f1)"
assert_rc "8: end-фаза не мутирует файл" 0 "$([ "$before" = "$after" ] && echo 0 || echo 1)"
assert_out "8: FAIL на шаблонном плейсхолдере" "шаблонный плейсхолдер" "$out"

# --- кейс 8b: end, ✅-REVIEW без SHA → exit 1 (SHA диапазона обязателен) ---
make_session "$F" "$SECTION

$ARCH_OK
- REVIEW-GATE: ✅ Принято (code-reviewer, без диапазона)"
out="$(run_end)"; rc=$?
assert_rc "8b: end ✅-REVIEW без SHA → exit 1" 1 "$rc"
assert_out "8b: FAIL с требованием SHA" "SHA диапазона" "$out"

# --- кейс 9: strict (feature), N/A «не начата» → exit 1 ---
make_session "$SESS/S99_session.md" "$SECTION

- ARCH-GATE: N/A — сессия не начата
- REVIEW-GATE: N/A — сессия не начата" "feat-099 — Test; закрывает feat-099"
out="$(SESSIONS_DIR="$SESS" bash "$GUARD" "" feat-099 2>&1)"; rc=$?
assert_rc "9: strict N/A «не начата» → exit 1" 1 "$rc"
assert_out "9: объяснение строгого режима" "гейты заполняются ПО ФАКТУ" "$out"

# --- кейс 10: start, закрытая сессия в корне → WARN «make archive», exit 0 ---
make_session "$F" "$SECTION

$ARCH_OK
$REV_OK"
sed -i 's/Статус: ◐ в работе/Статус: [x] ✅ 2026-11-19/' "$F"
out="$(run_start)"; rc=$?
assert_rc "10: start закрытой-в-корне сессии → exit 0" 0 "$rc"
assert_out "10: WARN про make archive" "make archive" "$out"

# --- кейс 11: start, раздел есть, обеих строк нет → вставка после заголовка, ARCH до REVIEW ---
make_session "$F" "$SECTION"
out="$(run_start)"; rc=$?
assert_rc "11: раздел без строк → exit 1" 1 "$rc"
assert_count "11: строк ARCH-GATE — одна" '^- ARCH-GATE:' "$F" 1
assert_count "11: строк REVIEW-GATE — одна" '^- REVIEW-GATE:' "$F" 1
arch_ln="$(grep -nE '^- ARCH-GATE:' "$F" | cut -d: -f1)"
rev_ln="$(grep -nE '^- REVIEW-GATE:' "$F" | cut -d: -f1)"
assert_rc "11: ARCH вставлен до REVIEW" 0 "$([ "${rev_ln:-0}" -gt "${arch_ln:-0}" ] && echo 0 || echo 1)"
sed -i 's|^- ARCH-GATE:.*|- ARCH-GATE: N/A — тест: архитектура не трогается|' "$F"
out="$(run_start)"; rc=$?
assert_rc "11b: после заполнения ARCH → exit 0, без дублей" 0 "$rc"
assert_count "11b: дублей не появилось" '^- (ARCH|REVIEW)-GATE:' "$F" 2

# --- кейс 12: start, только ARCH → REVIEW дописан после ARCH; заполнение → exit 0 ---
make_session "$F" "$SECTION

$ARCH_OK"
out="$(run_start)"; rc=$?
assert_rc "12: только ARCH + плейсхолдер-REVIEW → exit 0 (REVIEW мягкий)" 0 "$rc"
assert_count "12: строк REVIEW-GATE — одна" '^- REVIEW-GATE:' "$F" 1
arch_ln="$(grep -nE '^- ARCH-GATE:' "$F" | cut -d: -f1)"
rev_ln="$(grep -nE '^- REVIEW-GATE:' "$F" | cut -d: -f1)"
assert_rc "12: REVIEW дописан после ARCH" 0 "$([ "${rev_ln:-0}" -gt "${arch_ln:-0}" ] && echo 0 || echo 1)"

# --- кейс 13: start, только REVIEW → ARCH вставлен ПЕРЕД REVIEW, exit 1 ---
make_session "$F" "$SECTION

$REV_OK"
out="$(run_start)"; rc=$?
assert_rc "13: только REVIEW (ARCH-плейсхолдер) → exit 1" 1 "$rc"
assert_count "13: строк ARCH-GATE — одна" '^- ARCH-GATE:' "$F" 1
arch_ln="$(grep -nE '^- ARCH-GATE:' "$F" | cut -d: -f1)"
rev_ln="$(grep -nE '^- REVIEW-GATE:' "$F" | cut -d: -f1)"
assert_rc "13: ARCH вставлен перед REVIEW" 0 "$([ "${arch_ln:-0}" -lt "${rev_ln:-0}" ] && echo 0 || echo 1)"

# --- кейс 14: start, нет раздела и нет «Отчёта» → append-ветка: раздел в конец файла ---
printf '# S99 — без отчёта\n\nЭтап: тест | Статус: ◐ в работе\n' > "$F"
out="$(run_start)"; rc=$?
assert_rc "14: файл без раздела и без «Отчёта» → exit 1" 1 "$rc"
assert_count "14: заголовков раздела — один" '^## Гейты субагентов' "$F" 1
assert_count "14: строк чекбоксов — две" '^- (ARCH|REVIEW)-GATE:' "$F" 2

# --- кейс 15: --phase bogus → exit 2 (ошибка вызова) ---
out="$(SESSIONS_DIR="$SESS" bash "$GUARD" --phase bogus 2>&1)"; rc=$?
assert_rc "15: --phase bogus → exit 2" 2 "$rc"
assert_out "15: сообщение об ошибке вызова" "ERROR: --phase" "$out"

# --- кейс 16: start + feature без фичевой сессии → WARN, exit 0 (без провала в auto-detect) ---
out="$(SESSIONS_DIR="$SESS" bash "$GUARD" --phase start "" feat-077 2>&1)"; rc=$?
assert_rc "16: start + feature без сессии → exit 0" 0 "$rc"
assert_out "16: WARN про фичевую сессию" "нечего проверять" "$out"
if printf '%s' "$out" | grep -q 'session:'; then
  TESTS=$((TESTS + 1)); echo "  [FAIL] 16: провалился в автоопределение чужой сессии"; FAILED=$((FAILED + 1))
else
  TESTS=$((TESTS + 1)); echo "  [PASS] 16: автоопределение не запускалось"
fi

# --- кейс 17: «<» в легально заполненной строке не красит гейт (только «[»-форма с <) ---
make_session "$F" "$SECTION

- ARCH-GATE: N/A — см. обоснование в <solution/backlog/BL-006-…md>
$REV_OK"
out="$(run_start)"; rc=$?
assert_rc "17: N/A с <в угловых скобках> → exit 0 (ложный плейсхолдер исключён)" 0 "$rc"

# --- кейс 18: start, ДВЕ запланированные («[ ] не начата») → exit 0, гейт их скипает (D-027) ---
F2="$SESS/S98_session.md"
make_session "$F2" "$SECTION

- ARCH-GATE: N/A — сессия не начата
- REVIEW-GATE: N/A — сессия не начата"
make_session "$F" "$SECTION

- ARCH-GATE: N/A — сессия не начата
- REVIEW-GATE: N/A — сессия не начата"
sed -i 's/Статус: ◐ в работе/Статус: [ ] не начата/' "$F2" "$F"
out="$(run_start)"; rc=$?
assert_rc "18: две запланированные сессии → exit 0 (гейт — только для стартованных)" 0 "$rc"
assert_out "18: запланированные перечислены в [SKIP]" "[SKIP] запланированы" "$out"
assert_out "18: обе в списке SKIP" "S98_session.md" "$out"
assert_out "18: и вторая" "S99_session.md" "$out"
assert_out "18: стартованных нет — объяснение ритуала" "стартованных (◐) сессий нет" "$out"

# --- кейс 19: start, одна ◐ настоящая N/A + одна запланированная → exit 0, проверена одна ---
make_session "$F2" "$SECTION

$ARCH_NA
$REV_OK"
make_session "$F" "$SECTION

- ARCH-GATE: N/A — сессия не начата
- REVIEW-GATE: N/A — сессия не начата"
sed -i 's/Статус: ◐ в работе/Статус: [ ] не начата/' "$F"
out="$(run_start)"; rc=$?
assert_rc "19: ◐ настоящая + запланированная → exit 0" 0 "$rc"
assert_out "19: настоящая проверена" "[PASS] ARCH-GATE: N/A — сессия не трогает архитектуру" "$out"
assert_out "19: итог с числом проверенных" "сессий проверено: 1" "$out"
assert_out "19: запланированная скипнута" "[SKIP] запланированы" "$out"

# --- кейс 20: start, ◐ с ARCH «N/A — сессия не начата» → exit 1 (стартовала — обязана резолвить) ---
make_session "$F" "$SECTION

- ARCH-GATE: N/A — сессия не начата
- REVIEW-GATE: N/A — сессия не начата"
out="$(run_start)"; rc=$?
assert_rc "20: ◐ с «не начата» в ARCH → exit 1 (дыра владельца закрыта)" 1 "$rc"
assert_out "20: объяснение противоречия статусу" "уже стартовала" "$out"
assert_out "20: сессия в списке провальных" "S99_session.md" "$out"

# --- кейс 21: start, только запланированные (◐ нет) → exit 0 с объяснением ---
rm -f "$F"
sed -i 's/Статус: ◐ в работе/Статус: [ ] не начата/' "$F2"
out="$(run_start)"; rc=$?
assert_rc "21: только запланированные → exit 0" 0 "$rc"
assert_out "21: стартованных нет" "стартованных (◐) сессий нет" "$out"

# --- кейс 22: end без фичи, ◐ с «не начата» → exit 0 (совместимость: strict только при feature) ---
make_session "$F" "$SECTION

- ARCH-GATE: N/A — сессия не начата
- REVIEW-GATE: N/A — сессия не начата"
out="$(run_end)"; rc=$?
assert_rc "22: end без фичи «не начата» → exit 0" 0 "$rc"

# --- кейс 23: end без фичи, одна запланированная + одна ◐ заполненная → exit 0 (проверена ◐) ---
make_session "$F2" "$SECTION

$ARCH_NA
$REV_OK"
make_session "$F" "$SECTION

- ARCH-GATE: N/A — сессия не начата
- REVIEW-GATE: N/A — сессия не начата"
sed -i 's/Статус: ◐ в работе/Статус: [ ] не начата/' "$F"
out="$(run_end)"; rc=$?
assert_rc "23: end проверяет ◐, запланированная не блокирует → exit 0" 0 "$rc"
assert_out "23: проверена стартованная" "S98_session.md" "$out"

# --- кейс 24: end без фичи, только запланированные → WARN, exit 0 ---
rm -f "$F2"
out="$(run_end)"; rc=$?
assert_rc "24: end только запланированные → WARN exit 0" 0 "$rc"
assert_out "24: объяснение" "гейту закрытия нечего проверять" "$out"

echo ""
if [ "$FAILED" -eq 0 ]; then
  echo "Self-test: OK ($TESTS assertions passed) — guard-subagents.sh соответствует контракту."
  exit 0
fi
echo "Self-test: FAIL ($FAILED из $TESTS) — см. [FAIL] выше."
exit 1

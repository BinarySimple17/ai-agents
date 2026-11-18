#!/usr/bin/env bash
# scripts/guard-subagents.sh — механический гейт субагентов (architect / code-reviewer).
#
# Проверяет, что в файле текущей сессии заполнен раздел «Гейты субагентов»:
#   - ARCH-GATE:   canon-check сабагента architect до старта работ
#                  (AGENTS.md Startup Workflow, шаг 8)
#   - REVIEW-GATE: ревью code-reviewer после зелёного make check, до коммита
#                  (AGENTS.md End of Session, шаг 8)
# Каждая строка — ✅ + evidence, либо N/A — причина. Пустые/шаблонные строки = FAIL:
# гейт нельзя пропустить незаметно.
#
# Использование:
#   bash scripts/guard-subagents.sh                        # автоопределение последней сессии
#   bash scripts/guard-subagents.sh path/to/X_session.md   # явный файл сессии
# Вызывается из: make clean-check, make session-end, scripts/verify-feature.sh.
#
# Автоопределение: последняя *_session.md в корне solution/sessions/ (sort -V); если там
# пусто — последняя в solution/sessions/archive/. S00_session.md (канон) и
# TEMPLATE_session.md исключаются. Сессия в archive/ БЕЗ раздела гейтов — закрыта до
# введения механизма (legacy): WARN, exit 0. Сессия в корне без раздела — FAIL.
#
# Exit: 0 — оба гейта учтены (или legacy), 1 — гейт пропущен/не заполнен.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SESSION="${1:-}"
cd "$ROOT"

PASS=0
FAIL=0

report() {
  if [ "$2" -eq 0 ]; then
    echo "  [PASS] $1"; PASS=$((PASS + 1))
  else
    echo "  [FAIL] $1"; FAIL=$((FAIL + 1))
  fi
}

# --- файл сессии: сначала активные, затем archive/ (legacy-проверка) ---
if [ -z "$SESSION" ]; then
  SESSION="$(find solution/sessions -maxdepth 1 -name '*_session.md' \
    ! -name 'S00_session.md' ! -name 'TEMPLATE_session.md' 2>/dev/null | sort -V | tail -1)"
  if [ -z "$SESSION" ]; then
    SESSION="$(find solution/sessions/archive -name '*_session.md' 2>/dev/null | sort -V | tail -1)"
  fi
fi

echo "=== Subagent Gates Guard ($ROOT) ==="
if [ -z "$SESSION" ] || [ ! -f "$SESSION" ]; then
  echo "  [FAIL] файл сессии не найден (solution/sessions/*_session.md) — создайте его из TEMPLATE_session.md до старта работ"
  exit 1
fi
echo "  session: $SESSION"

# --- legacy: закрыта до введения гейтов ---
if [[ "$SESSION" == *archive/* ]] && ! grep -q '^## Гейты субагентов' "$SESSION"; then
  echo "  [WARN] сессия в archive/ без раздела гейтов — закрыта до введения механизма (legacy), проверка пропущена"
  exit 0
fi

# --- раздел на месте ---
grep -q '^## Гейты субагентов' "$SESSION"
report "раздел «Гейты субагентов» присутствует" $?

ARCH_LINE="$(grep -E '^- ARCH-GATE:' "$SESSION" | tail -1)"
REV_LINE="$(grep -E '^- REVIEW-GATE:' "$SESSION" | tail -1)"

# check_gate <имя> <строка> <требовать-SHA: 0|1>
check_gate() {
  local name="$1" line="$2" hexreq="$3"
  if [ -z "$line" ]; then
    echo "  [FAIL] $name: строка отсутствует в разделе"
    FAIL=$((FAIL + 1))
    return
  fi
  if printf '%s' "$line" | grep -q '<'; then
    echo "  [FAIL] $name: остался шаблонный плейсхолдер (<…>) — замените строку фактическими данными"
    FAIL=$((FAIL + 1))
    return
  fi
  local ok na
  ok="$(printf '%s' "$line" | grep -c '✅' || true)"
  na="$(printf '%s' "$line" | grep -c 'N/A' || true)"
  if [ "$ok" -ge 1 ] && [ "$na" -ge 1 ]; then
    echo "  [FAIL] $name: отмечены ОБА варианта — оставьте ЛИБО «✅ + evidence», ЛИБО «N/A — причина»"
    FAIL=$((FAIL + 1))
    return
  fi
  if [ "$ok" -ge 1 ]; then
    local ev="${line#*✅}"
    ev="$(printf '%s' "$ev" | sed -e 's/^[[:space:]]*//')"
    if [ "${#ev}" -lt 5 ]; then
      echo "  [FAIL] $name: после ✅ нет evidence"
      FAIL=$((FAIL + 1))
      return
    fi
    if [ "$hexreq" -eq 1 ] && ! printf '%s' "$ev" | grep -Eq '[0-9a-f]{7,}'; then
      echo "  [FAIL] $name: evidence должен содержать SHA диапазона ревью (BASE7..HEAD7 или хотя бы BASE7)"
      FAIL=$((FAIL + 1))
      return
    fi
    echo "  [PASS] $name: ✅ $(printf '%s' "$ev" | cut -c1-80)"
    PASS=$((PASS + 1))
    return
  fi
  if [ "$na" -ge 1 ]; then
    local reason="${line#*N/A}"
    reason="$(printf '%s' "$reason" | sed -e 's/^[[:space:]—–-]*//')"
    if [ "${#reason}" -lt 5 ]; then
      echo "  [FAIL] $name: после N/A нет причины"
      FAIL=$((FAIL + 1))
      return
    fi
    echo "  [PASS] $name: N/A — $(printf '%s' "$reason" | cut -c1-80)"
    PASS=$((PASS + 1))
    return
  fi
  echo "  [FAIL] $name: нет ни ✅, ни N/A — строка не заполнена"
  FAIL=$((FAIL + 1))
}

check_gate "ARCH-GATE" "$ARCH_LINE" 0
check_gate "REVIEW-GATE" "$REV_LINE" 1

echo ""
if [ "$FAIL" -eq 0 ]; then
  echo "Subagent gates: OK ($PASS passed) — оба гейта учтены."
  exit 0
fi
echo "Subagent gates: FAIL ($FAIL) — пропускать гейты нельзя:"
echo "  - ARCH-GATE:   architect (canon-check) по плану сессии ДО старта работ — AGENTS.md Startup Workflow, шаг 8"
echo "  - REVIEW-GATE: code-reviewer по диапазону BASE..HEAD после зелёного make check, до коммита — AGENTS.md End of Session, шаг 8"
echo "  Формат: «- ARCH-GATE: ✅ <verdict> <evidence>» или «- ARCH-GATE: N/A — <причина>»;"
echo "  шаблон раздела — solution/sessions/TEMPLATE_session.md; диспетчеризация — skill requesting-code-review."
exit 1

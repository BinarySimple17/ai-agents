#!/usr/bin/env bash
# scripts/guard-subagents.sh — механический гейт субагентов (architect / code-reviewer),
# двухфазный: ARCH-GATE неизбежен НА СТАРТЕ сессии, REVIEW-GATE — на закрытии.
#
# Проверяет (а в start-фазе дозаполняет) раздел «Гейты субагентов» в файле текущей сессии:
#   - ARCH-GATE:   canon-check сабагента architect ДО старта работ
#                  (AGENTS.md Startup Workflow, шаг 8)
#   - REVIEW-GATE: ревью code-reviewer после зелёного make check, до коммита
#                  (AGENTS.md End of Session, шаг 8)
# Каждая строка — ✅ + evidence, либо N/A — причина. Пустые/шаблонные строки = FAIL:
# гейт нельзя пропустить незаметно.
#
# Фазы:
#   --phase start  гейт СТАРТА сессии (init.sh, make session-start). Самовосстановление:
#                  нет раздела/строк чекбоксов — дописывает канонические плейсхолдеры
#                  ([FIX], идемпотентно). ARCH-GATE обязателен: плейсхолдер/незаполненная
#                  строка = FAIL — make check остаётся красным, пока canon-check не даст
#                  ✅ verdict или не записан обоснованный N/A. REVIEW-GATE — мягкий
#                  ([WARN]): ревью по определению делается в конце, жёстко проверяется
#                  end-фазой. Активной сессии нет (свежий клон / всё заархивировано) →
#                  WARN, exit 0 — рестартуемость репозитория (feat-001 verification = ./init.sh).
#   --phase end    гейт ЗАКРЫТИЯ (make clean-check, make session-end, verify-feature.sh
#                  в строгом режиме). Обе строки обязательны (✅/N/A); файл НЕ мутируется.
#                  Дефолт при отсутствии --phase — end (обратная совместимость вызовов).
#
# Использование:
#   bash scripts/guard-subagents.sh --phase start            # гейт старта (последняя сессия в корне)
#   bash scripts/guard-subagents.sh                          # гейт закрытия (автоопределение)
#   bash scripts/guard-subagents.sh path/to/X_session.md     # явный файл сессии (end)
#   bash scripts/guard-subagents.sh "" feat-002              # сессия, ведущая фичу feat-002 (end, strict)
# Вызывается из: init.sh и make session-start (--phase start); make clean-check,
# make session-end, scripts/verify-feature.sh (end). Self-test — scripts/tests/test-guard-subagents.sh.
#
# Автоопределение: последняя *_session.md в корне $SESSIONS_DIR (sort -V); в end-фазе,
# если корень пуст, — последняя в $SESSIONS_DIR/archive/ (start-фаза archive/ не ищет:
# архивная сессия не может быть «текущей»). S00_session.md (канон) и TEMPLATE_session.md
# исключаются. При заданном feature-id ищется файл сессии, ведущий эту фичу (grep по
# «закрывает feat-XXX»); нет такого файла — end-фаза: FAIL (гейт без сессии невозможен),
# start-фаза: WARN, exit 0. Сессия в archive/ БЕЗ раздела гейтов — закрыта до введения
# механизма (legacy): WARN, exit 0. Режим фичи (arg2 задан): причина N/A «сессия не начата»
# невалидна — гейты заполняются ПО ФАКТУ работ, незакрытая сессия не может verify-feature
# (ревью S02 Important-5).
#
# Exit: 0 — гейты учтены (или legacy / нет активной сессии в start-фазе),
#       1 — гейт пропущен/не заполнен, 2 — некорректный вызов.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Фаза: start (гейт старта сессии, самовосстановление) | end (гейт закрытия, дефолт)
PHASE="end"
if [ "${1:-}" = "--phase" ]; then
  if [ "$#" -lt 2 ] || [ -z "${2:-}" ]; then
    echo "ERROR: --phase требует значение start|end" >&2
    exit 2
  fi
  PHASE="$2"
  shift 2
fi
case "$PHASE" in
  start|end) ;;
  *) echo "ERROR: --phase должен быть start|end (получено: «$PHASE»)" >&2; exit 2 ;;
esac

SESSION="${1:-}"
FEATURE="${2:-}"
# Каталог сессий: переопределяется в self-test (фикстурные прогонки, scripts/tests/).
SESSIONS_DIR="${SESSIONS_DIR:-solution/sessions}"
# Строгий режим: гейт привязан к фиче, которая закрывается
GATE_STRICT=0
[ -n "$FEATURE" ] && GATE_STRICT=1
cd "$ROOT"

echo "=== Subagent Gates Guard [$PHASE] ($ROOT) ==="

PASS=0
FAIL=0
WARN=0

# Пороги валидности строк гейтов (AGENTS.md: литералы логики — именованные константы)
MIN_EVIDENCE_LEN=5   # короче — считаем «после ✅/N/A нет evidence/причины»
MIN_SHA_LEN=7        # минимальная длина SHA диапазона ревью (короткий хэш git)
PREVIEW_LEN=80       # обрезка evidence/причины в summary-строках вывода
# Признак закрытой сессии ищем в шапке файла; синхронно с scripts/archive.sh (первые 20 строк)
STATUS_SCAN_LINES=20

report() {
  if [ "$2" -eq 0 ]; then
    echo "  [PASS] $1"; PASS=$((PASS + 1))
  else
    echo "  [FAIL] $1"; FAIL=$((FAIL + 1))
  fi
}

# Канонические плейсхолдеры (дословно как в TEMPLATE_session.md) — для автодобавления.
# Они же распознаются check_gate как «<»-плейсхолдеры = незаполненный гейт.
ARCH_PLACEHOLDER='- ARCH-GATE: [✅ <verdict: compliant | violations | needs-decision> + <план/файл> | N/A — <причина>]'
REV_PLACEHOLDER='- REVIEW-GATE: [✅ <Ready to merge? вердикт> + <BASE7>..<HEAD7> | N/A — <причина>]'

# --- самовосстановление (только start-фаза): дозаполнить недостающие чекбоксы ---

# ensure_gate_section — добавляет раздел «Гейты субагентов», если его нет:
# перед «## Отчёт по сессии», а при отсутствии такого раздела — в конец файла.
ensure_gate_section() {
  grep -q '^## Гейты субагентов' "$SESSION" && return 0
  local tmp
  tmp="$(mktemp)"
  if grep -q '^## Отчёт по сессии' "$SESSION"; then
    awk -v arch="$ARCH_PLACEHOLDER" -v rev="$REV_PLACEHOLDER" '
      /^## Отчёт по сессии/ && !done {
        print "## Гейты субагентов (заполняется по факту, до закрытия сессии)";
        print "";
        print "<!-- Автодобавлено guard-subagents.sh --phase start: ARCH-GATE — до старта работ";
        print "     (canon-check architect: ✅ verdict, либо N/A — причина); REVIEW-GATE — по факту";
        print "     ревью, до закрытия сессии. Строка = ОДИН вариант: ✅ + evidence, либо N/A — причина. -->";
        print "";
        print arch;
        print rev;
        print "";
        done=1
      }
      { print }
    ' "$SESSION" > "$tmp" && mv "$tmp" "$SESSION" || {
      rm -f "$tmp"
      echo "  [FAIL] не удалось обновить файл сессии ($SESSION) — проверьте права доступа/свободное место"
      exit 2
    }
  else
    {
      echo ""
      echo "## Гейты субагентов (заполняется по факту, до закрытия сессии)"
      echo ""
      echo "<!-- Автодобавлено guard-subagents.sh --phase start: ARCH-GATE — до старта работ"
      echo "     (canon-check architect: ✅ verdict, либо N/A — причина); REVIEW-GATE — по факту"
      echo "     ревью, до закрытия сессии. Строка = ОДИН вариант: ✅ + evidence, либо N/A — причина. -->"
      echo ""
      printf '%s\n' "$ARCH_PLACEHOLDER"
      printf '%s\n' "$REV_PLACEHOLDER"
    } >> "$SESSION" || {
      echo "  [FAIL] не удалось обновить файл сессии ($SESSION) — проверьте права доступа/свободное место"
      exit 2
    }
  fi
  echo "  [FIX] добавлен раздел «Гейты субагентов» с плейсхолдерами — заполните ARCH-GATE до старта работ"
}

# ensure_gate_lines — дописывает недостающие строки-чекбоксы (ARCH перед REVIEW):
# после существующей парной строки, а если парных нет — сразу после заголовка раздела.
ensure_gate_lines() {
  local need_arch=0 need_rev=0
  grep -qE '^- ARCH-GATE:' "$SESSION" || need_arch=1
  grep -qE '^- REVIEW-GATE:' "$SESSION" || need_rev=1
  [ "$need_arch" -eq 0 ] && [ "$need_rev" -eq 0 ] && return 0
  local tmp
  tmp="$(mktemp)"
  awk -v need_arch="$need_arch" -v need_rev="$need_rev" \
      -v arch="$ARCH_PLACEHOLDER" -v rev="$REV_PLACEHOLDER" '
    /^## Гейты субагентов/ && !hdr {
      print; hdr=1
      if (need_arch == 1 && need_rev == 1) { print ""; print arch; print rev; print "" }
      next
    }
    /^- ARCH-GATE:/ {
      print
      if (need_rev == 1 && !revdone) { print rev; revdone=1 }
      next
    }
    /^- REVIEW-GATE:/ {
      if (need_arch == 1 && !archdone) { print arch; archdone=1 }
      print
      next
    }
    { print }
  ' "$SESSION" > "$tmp" && mv "$tmp" "$SESSION" || {
    rm -f "$tmp"
    echo "  [FAIL] не удалось обновить файл сессии ($SESSION) — проверьте права доступа/свободное место"
    exit 2
  }
  if [ "$need_arch" -eq 1 ]; then
    echo "  [FIX] добавлена строка ARCH-GATE (плейсхолдер) — заполните ДО старта работ: make check красный до заполнения"
  fi
  if [ "$need_rev" -eq 1 ]; then
    echo "  [FIX] добавлена строка REVIEW-GATE (плейсхолдер) — заполните по факту ревью, до закрытия сессии"
  fi
  return 0
}

# --- файл сессии: явно заданный → по фиче (роль «закрывает») → последняя активная → archive/ (только end) ---
if [ -z "$SESSION" ] && [ -n "$FEATURE" ]; then
  # ведущая сессия фичи помечена «закрывает feat-XXX»; сессии-зависимости упоминают
  # фичу как dep — grep по роли, иначе guard возьмёт чужую сессию (урок S02)
  SESSION="$(grep -l "закрывает $FEATURE" "$SESSIONS_DIR"/*_session.md 2>/dev/null | grep -v 'S00_session\|TEMPLATE_session' | sort -V | tail -1)"
  if [ -z "$SESSION" ] && [ "$PHASE" = "end" ]; then
    SESSION="$(grep -l "$FEATURE" "$SESSIONS_DIR"/*_session.md 2>/dev/null | grep -v 'S00_session\|TEMPLATE_session' | sort -V | tail -1)"
  fi
  if [ -z "$SESSION" ] && [ "$PHASE" = "end" ]; then
    SESSION="$(grep -l "$FEATURE" "$SESSIONS_DIR"/archive/*_session.md 2>/dev/null | sort -V | tail -1)"
  fi
fi
if [ -z "$SESSION" ]; then
  if [ "$PHASE" = "start" ] && [ -n "$FEATURE" ]; then
    # Важно: не проваливаемся в автоопределение «последней сессии» — гейт должен
    # проверять сессию ведущей фичи, а не чужую (ревью S22 Important-1)
    echo "  [WARN] сессия, ведущая $FEATURE, не найдена в $SESSIONS_DIR — старт-гейту фичи нечего проверять;"
    echo "         создайте файл сессии (роль «закрывает $FEATURE») из TEMPLATE_session.md и повторите"
    exit 0
  fi
  SESSION="$(find "$SESSIONS_DIR" -maxdepth 1 -name '*_session.md' \
    ! -name 'S00_session.md' ! -name 'TEMPLATE_session.md' 2>/dev/null | sort -V | tail -1)"
  if [ -z "$SESSION" ] && [ "$PHASE" = "end" ]; then
    SESSION="$(find "$SESSIONS_DIR/archive" -name '*_session.md' 2>/dev/null | sort -V | tail -1)"
  fi
fi

if [ -z "$SESSION" ] || [ ! -f "$SESSION" ]; then
  if [ "$PHASE" = "start" ]; then
    # Не блокер: свежий клон/всё заархивировано — репозиторий остаётся рестартуемым
    # (./init.sh — verification feat-001). Но сессия без файла гейтами не учтена.
    echo "  [WARN] активная сессия не найдена ($SESSIONS_DIR/*_session.md) — работа без файла сессии гейтами не учтена;"
    echo "         создайте файл из TEMPLATE_session.md ДО старта работ: после создания make check будет красным,"
    echo "         пока ARCH-GATE (canon-check архитектора, Startup Workflow шаг 8) не получит ✅ или обоснованный N/A"
    exit 0
  fi
  if [ -n "$FEATURE" ]; then
    echo "  [FAIL] нет файла сессии, ведущего $FEATURE, — создайте его из TEMPLATE_session.md ДО старта работ (гейт без сессии невозможен)"
  else
    echo "  [FAIL] файл сессии не найден ($SESSIONS_DIR/*_session.md) — создайте его из TEMPLATE_session.md до старта работ"
  fi
  exit 1
fi
echo "  session: $SESSION"

# --- start-фаза: сессия помечена закрытой, но не заархивирована — диагностическое напоминание ---
if [ "$PHASE" = "start" ] && head -"$STATUS_SCAN_LINES" "$SESSION" | grep -q 'Статус:.*✅'; then
  echo "  [WARN] сессия помечена закрытой (✅), но файл ещё в корне $SESSIONS_DIR/ — выполните make archive"
fi

# --- legacy: закрыта до введения гейтов (в archive/; start-фаза archive/ не выбирает) ---
if [[ "$SESSION" == *archive/* ]] && ! grep -q '^## Гейты субагентов' "$SESSION"; then
  echo "  [WARN] сессия в archive/ без раздела гейтов — закрыта до введения механизма (legacy), проверка пропущена"
  exit 0
fi

# --- start-фаза: самовосстановление — раздел и строки чекбоксов дозаполняются ---
# (end-фаза файл не мутирует: закрытие — только проверка; мутации — только для
# легитимных файлов активных сессий, не archive/ и не произвольные .md — ревью S22 Minor-6)
if [ "$PHASE" = "start" ]; then
  case "$SESSION" in
    *archive/*)
      # файлы закрытых сессий не переписываются (solution/README, жизненный цикл п. 3)
      echo "  [WARN] start-фаза: архивная сессия самовосстановлению не подлежит (только проверка)"
      ;;
    "$SESSIONS_DIR"/*_session.md)
      ensure_gate_section
      ensure_gate_lines
      ;;
    *)
      echo "  [WARN] start-фаза: файл вне $SESSIONS_DIR/*_session.md — самовосстановление не применяется (только проверка)"
      ;;
  esac
fi

# --- раздел на месте ---
grep -q '^## Гейты субагентов' "$SESSION"
report "раздел «Гейты субагентов» присутствует" $?

ARCH_LINE="$(grep -E '^- ARCH-GATE:' "$SESSION" | tail -1)"
REV_LINE="$(grep -E '^- REVIEW-GATE:' "$SESSION" | tail -1)"

# check_gate <имя> <строка> <требовать-SHA: 0|1> <soft: 0|1>
# soft=1 (start-фаза, REVIEW-GATE): нарушения — [WARN], гейт не краснят (ревью делается
# в конце сессии; жёсткая проверка — end-фаза).
check_gate() {
  local name="$1" line="$2" hexreq="$3" soft="${4:-0}"
  local tag="[FAIL]"
  [ "$soft" -eq 1 ] && tag="[WARN]"
  gate_fail() {
    echo "  $tag $name: $1"
    if [ "$soft" -eq 1 ]; then WARN=$((WARN + 1)); else FAIL=$((FAIL + 1)); fi
  }
  if [ -z "$line" ]; then
    gate_fail "строка отсутствует в разделе"
    return
  fi
  # Шаблонный плейсхолдер — каноническая «[ … <…> … ]»-форма (пробел после «:» опционален);
  # «<» в легально заполненной строке вне такой формы гейт не красит (ревью S22 Minor-4, R6)
  if printf '%s' "$line" | grep -qE '^- (ARCH|REVIEW)-GATE: ?\[[^]]*<[^]]*\]'; then
    gate_fail "остался шаблонный плейсхолдер (<…>) — замените строку целиком фактическими данными"
    return
  fi
  local ok na
  ok="$(printf '%s' "$line" | grep -c '✅' || true)"
  na="$(printf '%s' "$line" | grep -c 'N/A' || true)"
  if [ "$ok" -ge 1 ] && [ "$na" -ge 1 ]; then
    gate_fail "отмечены ОБА варианта — оставьте ЛИБО «✅ + evidence», ЛИБО «N/A — причина»"
    return
  fi
  if [ "$ok" -ge 1 ]; then
    local ev="${line#*✅}"
    ev="$(printf '%s' "$ev" | sed -e 's/^[[:space:]]*//')"
    if [ "${#ev}" -lt "$MIN_EVIDENCE_LEN" ]; then
      gate_fail "после ✅ нет evidence"
      return
    fi
    if [ "$hexreq" -eq 1 ] && ! printf '%s' "$ev" | grep -Eq "[0-9a-f]{$MIN_SHA_LEN,}"; then
      gate_fail "evidence должен содержать SHA диапазона ревью (BASE7..HEAD7 или хотя бы BASE7)"
      return
    fi
    echo "  [PASS] $name: ✅ $(printf '%s' "$ev" | cut -c1-"$PREVIEW_LEN")"
    PASS=$((PASS + 1))
    return
  fi
  if [ "$na" -ge 1 ]; then
    local reason="${line#*N/A}"
    reason="$(printf '%s' "$reason" | sed -e 's/^[[:space:]—–-]*//')"
    if [ "$GATE_STRICT" -eq 1 ] && printf '%s' "$reason" | grep -q 'не начата'; then
      gate_fail "причина «сессия не начата» невалидна — гейты заполняются ПО ФАКТУ работ; незакрытая сессия не может пройти verify-feature (ревью S02 Important-5). Замените на evidence или фактическую причину пропуска."
      return
    fi
    if [ "${#reason}" -lt "$MIN_EVIDENCE_LEN" ]; then
      gate_fail "после N/A нет причины"
      return
    fi
    echo "  [PASS] $name: N/A — $(printf '%s' "$reason" | cut -c1-"$PREVIEW_LEN")"
    PASS=$((PASS + 1))
    return
  fi
  gate_fail "нет ни ✅, ни N/A — строка не заполнена"
}

check_gate "ARCH-GATE" "$ARCH_LINE" 0
if [ "$PHASE" = "start" ]; then
  check_gate "REVIEW-GATE" "$REV_LINE" 1 1
else
  check_gate "REVIEW-GATE" "$REV_LINE" 1
fi

echo ""
if [ "$FAIL" -eq 0 ]; then
  if [ "$PHASE" = "start" ]; then
    echo "Session start gate: OK (ARCH-GATE учтён) — сессия стартует с закрытым архитектурным контролем."
    if [ "$WARN" -gt 0 ]; then
      echo "  REVIEW-GATE ещё не заполнен ([WARN] выше) — по факту ревью, до закрытия сессии"
      echo "  (end-фаза: make clean-check / make session-end / make verify-feature)."
    fi
    exit 0
  fi
  echo "Subagent gates: OK ($PASS passed) — оба гейта учтены."
  exit 0
fi
if [ "$PHASE" = "start" ]; then
  echo "Session start gate: FAIL ($FAIL) — сессия не может стартовать с незаполненным ARCH-GATE:"
  echo "  1. Сессия трогает архитектуру → диспетчеризуйте architect (canon-check) по плану сессии"
  echo "     ДО кода (AGENTS.md Startup Workflow, шаг 8), вердикт — в строку ARCH-GATE."
  echo "  2. Иначе → впишите обоснованный N/A с причиной (например: границы модулей/зависимости/"
  echo "     схема/ER не затрагиваются). make check зеленеет сразу после заполнения строки."
else
  echo "Subagent gates: FAIL ($FAIL) — пропускать гейты нельзя:"
  echo "  - ARCH-GATE:   architect (canon-check) по плану сессии ДО старта работ — AGENTS.md Startup Workflow, шаг 8"
  echo "  - REVIEW-GATE: code-reviewer по диапазону BASE..HEAD после зелёного make check, до коммита — AGENTS.md End of Session, шаг 8"
fi
echo "  Формат: «- ARCH-GATE: ✅ <verdict> <evidence>» или «- ARCH-GATE: N/A — <причина>»;"
echo "  шаблон раздела — solution/sessions/TEMPLATE_session.md; диспетчеризация — skill requesting-code-review."
exit 1

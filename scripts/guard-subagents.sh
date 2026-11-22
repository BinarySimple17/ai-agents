#!/usr/bin/env bash
# scripts/guard-subagents.sh — механический гейт субагентов (architect / code-reviewer),
# двухфазный: ARCH-GATE неизбежен НА СТАРТЕ сессии, REVIEW-GATE — на закрытии.
#
# Проверяет (а в start-фазе дозаполняет) раздел «Гейты субагентов» в файлах сессий:
#   - ARCH-GATE:   canon-check сабагента architect ДО старта работ
#                  (AGENTS.md Startup Workflow, ARCH-GATE-ритуал)
#   - REVIEW-GATE: ревью code-reviewer после зелёного make check, до коммита
#                  (AGENTS.md End of Session)
# Каждая строка — ✅ + evidence, либо N/A — причина. Пустые/шаблонные строки = FAIL:
# гейт нельзя пропустить незаметно.
#
# Какая сессия «стартует» (D-027): статус в шапке файла (первые 20 строк, как в archive.sh):
#   «◐ в работе» — сессия стартовала/в работе → её гейт проверяет старт-фаза (ARCH-GATE
#   жёстко: плейсхолдер, незаполненная строка или «N/A — сессия не начата» = FAIL — make
#   check красный, пока canon-check не даст ✅ или не записан обоснованный N/A);
#   «[ ] не начата» — запланирована (батч-планирование допустимо) → скипается: гейт
#   применится при старте, когда шапка переведена в «◐ в работе»;
#   «✅» — закрыта, но не заархивирована → WARN «make archive» (архивные файлы не правятся).
# В Предусловиях сессии есть чекбокс старта (шаблон TEMPLATE_session.md).
#
# Фазы:
#   --phase start  гейт СТАРТА (init.sh, make session-start): перебирает ВСЕ сессии в
#                  корне $SESSIONS_DIR (sort -V, без S00/TEMPLATE) и проверяет
#                  стартованные (◐). Самовосстановление ([FIX], идемпотентно) — только
#                  для проверяемых (стартованных) сессий. Стартованных нет → WARN, exit 0 —
#                  рестартуемость репозитория (feat-001 verification = ./init.sh).
#                  REVIEW-GATE стартованной сессии — мягкий ([WARN]): ревью делается в конце.
#   --phase end    гейт ЗАКРЫТИЯ (make clean-check, make session-end, verify-feature.sh
#                  в строгом режиме). Одна сессия; обе строки обязательны (✅/N/A); файл
#                  НЕ мутируется. Дефолт при отсутствии --phase — end (совместимость).
#
# Использование:
#   bash scripts/guard-subagents.sh --phase start            # старт-гейт: стартованные (◐) сессии в корне
#   bash scripts/guard-subagents.sh                          # гейт закрытия (автоопределение)
#   bash scripts/guard-subagents.sh path/to/X_session.md     # явный файл сессии — проверяется жёстко (end)
#   bash scripts/guard-subagents.sh "" feat-002              # сессия, ведущая фичу feat-002 (end, strict)
# Вызывается из: init.sh и make session-start (--phase start); make clean-check,
# make session-end, scripts/verify-feature.sh (end). Self-test — scripts/tests/test-guard-subagents.sh.
#
# Автоопределение end-фазы (ревью S23 C1): последняя СТАРТОВАННАЯ (◐) сессия в корне;
# стартованных нет — последняя закрытая (✅) в корне (к моменту clean-check закрываемая
# уже «✅»/в archive — скипать её = байпас REVIEW-GATE); иначе — последняя в
# $SESSIONS_DIR/archive/ (legacy-совместимость); проверять нечего вовсе → WARN, exit 0.
# Явный файл и путь по фиче (arg2) минуют классификацию по статусу — проверяются жёстко
# (fail-closed). При заданном feature-id ищется сессия с ролью «закрывает feat-XXX»; нет
# такого файла — end-фаза: FAIL (гейт без сессии невозможен), start-фаза: WARN, exit 0.
# Причина N/A «сессия не начата»: start-фаза — невалидна для стартованной (◐) сессии
# (D-027), end-фаза при verify-feature (strict) — невалидна (ревью S02 Important-5),
# end-фаза без фичи — валидна (совместимость).
#
# Exit: 0 — гейты учтены (или legacy / нет стартованных сессий в start-фазе),
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
FAILED_SESSIONS=""

# Пороги валидности строк гейтов (AGENTS.md: литералы логики — именованные константы)
MIN_EVIDENCE_LEN=5   # короче — считаем «после ✅/N/A нет evidence/причины»
MIN_SHA_LEN=7        # минимальная длина SHA диапазона ревью (короткий хэш git)
PREVIEW_LEN=80       # обрезка evidence/причины в summary-строках вывода
# Статус сессии ищем в шапке файла; синхронно с scripts/archive.sh (первые 20 строк)
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

# classify_session <файл> → closed (✅) | started (◐) | planned ([ ] не начата).
# Статус якорится по значению после «Статус:» (ревью S23 I1): легенда шаблона и
# нестандартные статусы («на паузе», опечатки) не молча скипаются, а попадают в started
# (fail-closed: гейт проверяет). Сессия БЕЗ строки «Статус:» в шапке — тоже started.
classify_session() {
  local head_content
  head_content="$(head -"$STATUS_SCAN_LINES" "$1" 2>/dev/null)"
  if printf '%s' "$head_content" | grep -Eq 'Статус:[[:space:]]*(\[[xX]\])?[[:space:]]*✅'; then
    echo "closed"
  elif printf '%s' "$head_content" | grep -Eq 'Статус:[[:space:]]*◐'; then
    echo "started"
  elif printf '%s' "$head_content" | grep -Eq 'Статус:[[:space:]]*\[[[:space:]]*\][[:space:]]*не начата[[:space:]]*$'; then
    echo "planned"
  else
    echo "started"
  fi
}

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

# check_gate <имя> <строка> <требовать-SHA: 0|1> <soft: 0|1> <запретить-«не начата»: 0|1>
# soft=1 (start-фаза, REVIEW-GATE): нарушения — [WARN], гейт не краснят (ревью делается
# в конце сессии; жёсткая проверка — end-фаза).
# no_not_started=1 (start-фаза, ARCH-GATE, D-027): причина «сессия не начата» невалидна —
# сессия в статусе «◐ в работе» стартовала и обязана резолвить ARCH-GATE по факту.
check_gate() {
  local name="$1" line="$2" hexreq="$3" soft="${4:-0}" no_not_started="${5:-0}"
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
    if [ "$no_not_started" -eq 1 ] && printf '%s' "$reason" | grep -Eq '^[[:space:]]*сессия не начата'; then
      gate_fail "причина «сессия не начата» невалидна — сессия в статусе «◐ в работе» уже стартовала (D-027):"
      echo "         резолвьте ARCH-GATE ДО кода — canon-check архитектора (✅ verdict) либо обоснованный"
      echo "         N/A (например: границы модулей/зависимости/схема/ER не затрагиваются);"
      echo "         make check зеленеет сразу после заполнения строки."
      return
    fi
    if [ "$GATE_STRICT" -eq 1 ] && printf '%s' "$reason" | grep -Eq '^[[:space:]]*сессия не начата'; then
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

# check_one_session <файл> — полный прогон одной сессии (блок вывода + проверки).
# Счётчики PASS/FAIL/WARN — глобальные, накапливаются между сессиями.
check_one_session() {
  SESSION="$1"
  local fail_before="$FAIL"
  echo "  session: $SESSION"
  if [ ! -f "$SESSION" ]; then
    echo "  [FAIL] файл сессии не найден: $SESSION"
    FAIL=$((FAIL + 1))
    FAILED_SESSIONS="$FAILED_SESSIONS $SESSION"
    return
  fi

  # --- start-фаза: сессия помечена закрытой, но не заархивирована — напоминание ---
  # (для явно заданного файла: авто-перебор closed-сессии скипает раньше)
  if [ "$PHASE" = "start" ] && head -"$STATUS_SCAN_LINES" "$SESSION" | grep -q 'Статус:.*✅'; then
    echo "  [WARN] сессия помечена закрытой (✅), но файл ещё в корне $SESSIONS_DIR/ — выполните make archive"
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

  if [ "$PHASE" = "start" ]; then
    check_gate "ARCH-GATE" "$ARCH_LINE" 0 0 1
    check_gate "REVIEW-GATE" "$REV_LINE" 1 1 0
  else
    check_gate "ARCH-GATE" "$ARCH_LINE" 0
    check_gate "REVIEW-GATE" "$REV_LINE" 1
  fi

  if [ "$FAIL" -gt "$fail_before" ]; then
    FAILED_SESSIONS="$FAILED_SESSIONS $SESSION"
  fi
}

# --- список сессий к проверке: явный файл → по фиче → перебор корня с классификацией ---
if [ -n "$SESSION" ]; then
  SESSION_LIST="$SESSION"
elif [ -n "$FEATURE" ]; then
  # ведущая сессия фичи помечена «закрывает feat-XXX»; сессии-зависимости упоминают
  # фичу как dep — grep по роли, иначе guard возьмёт чужую сессию (урок S02)
  SESSION_LIST="$(grep -l "закрывает $FEATURE" "$SESSIONS_DIR"/*_session.md 2>/dev/null | grep -v 'S00_session\|TEMPLATE_session' | sort -V | tail -1)"
  if [ -z "$SESSION_LIST" ] && [ "$PHASE" = "end" ]; then
    SESSION_LIST="$(grep -l "$FEATURE" "$SESSIONS_DIR"/*_session.md 2>/dev/null | grep -v 'S00_session\|TEMPLATE_session' | sort -V | tail -1)"
  fi
  if [ -z "$SESSION_LIST" ] && [ "$PHASE" = "end" ]; then
    SESSION_LIST="$(grep -l "$FEATURE" "$SESSIONS_DIR"/archive/*_session.md 2>/dev/null | sort -V | tail -1)"
  fi
  if [ -z "$SESSION_LIST" ]; then
    if [ "$PHASE" = "start" ]; then
      # Не проваливаемся в автоопределение «последней сессии» — гейт должен проверять
      # сессию ведущей фичи, а не чужую (ревью S22 Important-1)
      echo "  [WARN] сессия, ведущая $FEATURE, не найдена в $SESSIONS_DIR — старт-гейту фичи нечего проверять;"
      echo "         создайте файл сессии (роль «закрывает $FEATURE») из TEMPLATE_session.md и повторите"
      exit 0
    fi
    echo "  [FAIL] нет файла сессии, ведущего $FEATURE, — создайте его из TEMPLATE_session.md ДО старта работ (гейт без сессии невозможен)"
    exit 1
  fi
else
  SESSION_LIST="$(find "$SESSIONS_DIR" -maxdepth 1 -name '*_session.md' \
    ! -name 'S00_session.md' ! -name 'TEMPLATE_session.md' 2>/dev/null | sort -V)"
  if [ -z "$SESSION_LIST" ]; then
    if [ "$PHASE" = "start" ]; then
      # Не блокер: свежий клон/всё заархивировано — репозиторий остаётся рестартуемым
      # (./init.sh — verification feat-001). Но сессия без файла гейтами не учтена.
      echo "  [WARN] активные сессии не найдены ($SESSIONS_DIR/*_session.md) — работа без файлов сессий гейтами не учтена;"
      echo "         создайте файл из TEMPLATE_session.md и при старте переведите шапку в «◐ в работе»:"
      echo "         старт-гейт проверит её ARCH-GATE (canon-check ✅ или обоснованный N/A — D-027)"
      exit 0
    fi
    SESSION_LIST="$(find "$SESSIONS_DIR/archive" -name '*_session.md' 2>/dev/null | sort -V | tail -1)"
    if [ -z "$SESSION_LIST" ]; then
      echo "  [FAIL] файл сессии не найден ($SESSIONS_DIR/*_session.md) — создайте его из TEMPLATE_session.md до старта работ"
      exit 1
    fi
  fi
fi

# --- start-фаза без явного файла/фичи: классификация по статусу шапки (D-027) ---
# Гейт кусает только сессию, которую СТАРТУЮТ (◐ в работе); запланированные («не начата»)
# — планы, скипаются; закрытые (✅) в корне — напоминание про make archive.
if [ "$PHASE" = "start" ] && [ -z "$SESSION" ] && [ -z "$FEATURE" ]; then
  STARTED_LIST=""
  PLANNED_LIST=""
  CLOSED_LIST=""
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    case "$(classify_session "$f")" in
      started)  STARTED_LIST="${STARTED_LIST}${f}\n" ;;
      planned)  PLANNED_LIST="${PLANNED_LIST}${f}\n" ;;
      closed)   CLOSED_LIST="${CLOSED_LIST}${f}\n" ;;
    esac
  done <<EOF
$SESSION_LIST
EOF
  if [ -n "$CLOSED_LIST" ]; then
    echo "  [WARN] закрыты (✅), но не заархивированы — выполните make archive:" \
      "$(printf '%b' "$CLOSED_LIST" | tr '\n' ' ')"
  fi
  if [ -n "$PLANNED_LIST" ]; then
    echo "  [SKIP] запланированы («не начата» — гейт применится при старте, шапка → «◐ в работе»):" \
      "$(printf '%b' "$PLANNED_LIST" | tr '\n' ' ')"
  fi
  if [ -z "$STARTED_LIST" ]; then
    echo ""
    echo "Session start gate: OK — стартованных (◐) сессий нет; гейт применится при старте сессии"
    echo "  (переведите статус шапки в «◐ в работе» и резолвьте ARCH-GATE ДО кода — D-027)."
    exit 0
  fi
  SESSION_LIST="$(printf '%b' "$STARTED_LIST")"
fi

# --- end-фаза без явного файла/фичи: кого проверять ---
# Порядок (ревью S23 C1): последняя стартованная (◐); если стартованных нет — последняя
# закрытая (✅) в корне (по AGENTS.md clock-out к clean-check сессия уже «✅»/в archive —
# молча скипать её = байпас REVIEW-GATE); иначе — последняя в archive/ (legacy). Только
# когда проверять нечего вовсе — WARN exit 0. Запланированные («не начата») не кусаются.
if [ "$PHASE" = "end" ] && [ -z "$SESSION" ] && [ -z "$FEATURE" ]; then
  STARTED_LIST=""
  CLOSED_LAST=""
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    case "$(classify_session "$f")" in
      started) STARTED_LIST="${STARTED_LIST}${f}\n" ;;
      closed)  CLOSED_LAST="$f" ;;
    esac
  done <<EOF
$SESSION_LIST
EOF
  if [ -n "$STARTED_LIST" ]; then
    SESSION_LIST="$(printf '%b' "$STARTED_LIST" | tail -1)"
    OTHER_STARTED="$(printf '%b' "$STARTED_LIST" | head -n -1 | tr '\n' ' ')"
    if [ -n "$(printf '%s' "$OTHER_STARTED" | tr -d '[:space:]')" ]; then
      echo "  [WARN] в работе также:$OTHER_STARTED — end-фаза проверяет последнюю стартованную ($SESSION_LIST);"
      echo "         их REVIEW-GATE проверится при их закрытии (или явно: bash scripts/guard-subagents.sh <файл>)"
    fi
  elif [ -n "$CLOSED_LAST" ]; then
    SESSION_LIST="$CLOSED_LAST"
  else
    SESSION_LIST="$(find "$SESSIONS_DIR/archive" -name '*_session.md' 2>/dev/null | sort -V | tail -1)"
    if [ -z "$SESSION_LIST" ]; then
      echo "  [WARN] гейту закрытия нечего проверять: стартованных (◐) и закрытых (✅) сессий нет —"
      echo "         запланированные («не начата») гейт не кусает"
      exit 0
    fi
  fi
fi

# --- legacy: сессия в archive/ без раздела гейтов — закрыта до введения гейтов ---
# (единственный файл в списке; авто-перебор start archive/ не выбирает)
if [ "$(printf '%s\n' "$SESSION_LIST" | grep -c .)" -eq 1 ]; then
  ONLY_SESSION="$(printf '%s\n' "$SESSION_LIST" | head -1)"
  if [[ "$ONLY_SESSION" == *archive/* ]] && ! grep -q '^## Гейты субагентов' "$ONLY_SESSION"; then
    echo "  session: $ONLY_SESSION"
    echo "  [WARN] сессия в archive/ без раздела гейтов — закрыта до введения механизма (legacy), проверка пропущена"
    exit 0
  fi
fi

# --- прогон по списку (heredoc — while в основном шелле: счётчики глобальные) ---
while IFS= read -r f; do
  [ -n "$f" ] || continue
  check_one_session "$f"
done <<EOF
$SESSION_LIST
EOF

echo ""
if [ "$FAIL" -eq 0 ]; then
  if [ "$PHASE" = "start" ]; then
    N_CHECKED="$(printf '%s\n' "$SESSION_LIST" | grep -c .)"
    echo "Session start gate: OK — стартованных (◐) сессий проверено: $N_CHECKED, ARCH-GATE учтён во всех."
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
  echo "Session start gate: FAIL ($FAIL) — ARCH-GATE не резолвлен в:$FAILED_SESSIONS"
  echo "  Стартованная (◐) сессия не может работать с незаполненным ARCH-GATE:"
  echo "  1. Сессия трогает архитектуру → диспетчеризуйте architect (canon-check) по плану сессии"
  echo "     ДО кода (AGENTS.md Startup Workflow, ARCH-GATE-ритуал), вердикт — в строку ARCH-GATE."
  echo "  2. Иначе → впишите обоснованный N/A с причиной (например: границы модулей/зависимости/"
  echo "     схема/ER не затрагиваются). «N/A — сессия не начата» не проходит (D-027): сессия в статусе"
  echo "     «◐ в работе» уже стартовала; make check зеленеет сразу после заполнения строки."
else
  echo "Subagent gates: FAIL ($FAIL) — пропускать гейты нельзя:"
  echo "  - ARCH-GATE:   architect (canon-check) по плану сессии ДО старта работ — AGENTS.md Startup Workflow, ARCH-GATE-ритуал"
  echo "  - REVIEW-GATE: code-reviewer по диапазону BASE..HEAD после зелёного make check, до коммита — AGENTS.md End of Session"
fi
echo "  Формат: «- ARCH-GATE: ✅ <verdict> <evidence>» или «- ARCH-GATE: N/A — <причина>»;"
echo "  шаблон раздела — solution/sessions/TEMPLATE_session.md; диспетчеризация — skill requesting-code-review."
exit 1

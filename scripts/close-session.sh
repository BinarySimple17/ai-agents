#!/usr/bin/env bash
# scripts/close-session.sh — механический хвост End of Session (AGENTS.md / docs/AGENT-OPERATIONS.md).
# Нарратив делает агент (progress.md, feature_list.json, отчёт сессии, quality-document,
# сообщение коммита в файл; весь КОД закоммичен до запуска); скрипт — проверки и
# детерминированные мутации.
#
# Порядок (pre-flight fail-closed): ВСЕ проверки — до первой мутации (гейты, отчёт,
# фичи, грязь вне allowlist, clean-state protocol). Мутации атомарны в памяти (обе
# записи — после всех преобразований). Сбой ПОСЛЕ мутаций (archive/git) возможен и
# лечится вручную — см. «Восстановление» в docs/AGENT-OPERATIONS.md.
#
# Использование:
#   make close-session S=S11 MSG=close-msg.txt [SUMMARY="feat-011 → passing"] [FLAGS=--push]
#   bash scripts/close-session.sh S=S11 MSG=close-msg.txt --dry-run
#
#   S       — ID сессии (обяз.; файл solution/sessions/<S>_session.md, статус «◐ в работе»)
#   MSG     — путь к НЕпустому файлу сообщения коммита (обяз.; без него только --dry-run)
#   SUMMARY — краткий итог для «Текущего состояния» (опц.; дефолт строится из роли сессии)
#   --push  — после коммита: git push origin HEAD (пуш рабочей ветки свободен; main/dev не пройдут)
#   --dry-run — pre-flight + план без мутаций (clean-state не гоняется)
#
# Self-test hook (только scripts/tests/test-ritual-scripts.sh; в ручных прогонах не задавать):
#   CLOSE_SESSION_CLEANSTATE=0 — пропустить clean-state-check (тесты проверяют мутации
#   без полного init.sh; fail-closed остальных проверок сохранён).
#
# Exit: 0 — сессия закрыта (или план dry-run), 1 — валидация/проверка не прошла, 2 — некорректный вызов.
# node обязателен (как в archive.sh) — текстовые мутации без sed-экранирования.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SESSIONS_DIR="solution/sessions"
SESSIONS_MD="solution/sessions.md"

S=""
MSG=""
SUMMARY=""
PUSH=0
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --push) PUSH=1 ;;
    --dry-run) DRY_RUN=1 ;;
    S=*) S="${arg#S=}" ;;
    MSG=*) MSG="${arg#MSG=}" ;;
    SUMMARY=*) SUMMARY="${arg#SUMMARY=}" ;;
    *) echo "ERROR: неизвестный аргумент «$arg» (ожидание: S= MSG= SUMMARY= --push --dry-run)" >&2; exit 2 ;;
  esac
done

[ -z "$S" ] && { echo "ERROR: S обязателен. Пример: make close-session S=S11 MSG=close-msg.txt" >&2; exit 2; }
# Валидация всей строки (case, не grep: ^…$ матчит только первую строку — S=$'S99\nevil' проходил)
case "$S" in *[!A-Za-z0-9_-]*) echo "ERROR: S — только латиница/цифры/дефис/подчёркивание, без путей и переводов строк (получено: «$S»)" >&2; exit 2 ;; esac
if [ "$DRY_RUN" -eq 0 ] && { [ -z "$MSG" ] || [ ! -s "$MSG" ]; }; then
  echo "ERROR: MSG обязателен для мутаций и должен быть НЕпустым файлом (фраза WHY); план без мутаций — --dry-run" >&2
  exit 2
fi

SESSION_FILE="$SESSIONS_DIR/${S}_session.md"
[ -f "$SESSION_FILE" ] || {
  [ -f "$SESSIONS_DIR/archive/${S}_session.md" ] \
    && { echo "ERROR: сессия $S уже закрыта и лежит в $SESSIONS_DIR/archive/ — close-session повторно не закрывает" >&2; exit 1; }
  echo "ERROR: файл сессии не найден: $SESSION_FILE — создайте его (make new-session S=$S ...)" >&2
  exit 1
}
command -v node >/dev/null 2>&1 || { echo "ERROR: node required (как в archive.sh)" >&2; exit 2; }

# --- статус сессии (первые 20 строк; якорные regex — те же, что у гварда, D-027 I1:
# сырая легенда шаблона не считается ни ✅, ни ◐) ---
HEAD20="$(head -20 "$SESSION_FILE")"
if printf '%s' "$HEAD20" | grep -Eq 'Статус:[[:space:]]*(\[[xX]\])?[[:space:]]*✅'; then
  echo "ERROR: сессия $S уже помечена ✅ в шапке, но ещё в корне $SESSIONS_DIR/ — выполните make archive" >&2
  exit 1
fi
printf '%s' "$HEAD20" | grep -Eq 'Статус:[[:space:]]*◐' \
  || { echo "ERROR: сессия $S не в статусе «◐ в работе» — закрывают только стартованные сессии" >&2; exit 1; }

# --- роль по фиче: строка обязательна и канонична (fail-open здесь = закрытие без
# сверки фич — ревью I4). Значение, начинающееся с «—», — легитимный no-feature режим
# (конвенция шаблона; прецеденты S21/S22/S27 с разными формулировками) ---
ROLE_LINE="$(grep -m1 '^Роль по фиче:' "$SESSION_FILE" || true)"
if [ -z "$ROLE_LINE" ]; then
  echo "ERROR: в сессии $S нет строки «Роль по фиче:» — заполни шапку: закрывает/продолжает feat-0XX либо «— …» (сессия без фич)" >&2
  exit 1
fi
ROLE_VALUE="${ROLE_LINE#Роль по фиче:}"
ROLE_VALUE="${ROLE_VALUE#"${ROLE_VALUE%%[![:space:]]*}"}"
if [[ "$ROLE_VALUE" == —* ]]; then
  ROLE="harness"
  FEATS_CSV=""
elif printf '%s' "$ROLE_VALUE" | grep -Eq 'закрывает|продолжает'; then
  ROLE="$(printf '%s' "$ROLE_LINE" | grep -oq 'продолжает' && echo "продолжает" || echo "закрывает")"
  FEATS_CSV="$(printf '%s' "$ROLE_LINE" | grep -oE 'feat-[0-9]{3,}' | sort -u | paste -sd, -)"
  [ -z "$FEATS_CSV" ] && { echo "ERROR: роль заявлена («$ROLE»), но в строке нет feat-0XX: $ROLE_LINE" >&2; exit 1; }
else
  echo "ERROR: строка «Роль по фиче:» не каноническая (ожидание: закрывает/продолжает feat-0XX либо «— …» для сессии без фич): $ROLE_LINE" >&2
  exit 1
fi
echo "=== close-session $S [${ROLE}${FEATS_CSV:+ $FEATS_CSV}] ==="
if [ -n "$FEATS_CSV" ]; then
  node -e '
    const fs = require("fs");
    const [role, idsCsv] = process.argv.slice(1);
    const fl = JSON.parse(fs.readFileSync("feature_list.json", "utf8"));
    const bad = [];
    for (const id of idsCsv.split(",").filter(Boolean)) {
      const f = fl.features.find(x => x.id === id);
      if (!f) { bad.push(id + ": не найдена в feature_list.json"); continue; }
      const want = role === "закрывает" ? "passing" : "active";
      if (f.state !== want) bad.push(id + ": state=" + f.state + ", ожидалось " + want);
    }
    if (bad.length) {
      console.error("  [FAIL] фичи не согласованы с ролью сессии: " + bad.join("; "));
      console.error(role === "закрывает"
        ? "         сначала: make verify-feature F=<id> для каждой фичи сессии"
        : "         продолжающая фича обязана быть active (не прыгать в passing мимо verify-feature)");
      process.exit(1);
    }
    console.log("  [PASS] фичи (" + role + "): " + idsCsv);
  ' "$ROLE" "$FEATS_CSV" || exit 1
else
  echo "  [PASS] сессия без фич (states не меняются)"
fi

# --- гейты субагентов: end-фаза, явный файл (жёстко: обе строки ✅/N/A с evidence) ---
bash scripts/guard-subagents.sh "$SESSION_FILE" || {
  echo "ERROR: гейты субагентов не учтены — см. вывод guard-subagents.sh выше" >&2
  exit 1
}

# --- отчёт по сессии заполнен (не заглушка шаблона) ---
node -e '
  const fs = require("fs");
  const lines = fs.readFileSync(process.argv[1], "utf8").split(/\r?\n/);
  const fail = (msg) => { console.error("  [FAIL] отчёт по сессии: " + msg); process.exit(1); };
  const repStart = lines.findIndex(l => /^## Отчёт по сессии/.test(l));
  if (repStart === -1) fail("раздел отсутствует");
  const doneIdx = lines.findIndex((l, i) => i > repStart && /^- Сделано:/.test(l));
  if (doneIdx === -1) fail("нет подраздела «- Сделано:»");
  let filled = 0;
  for (let i = doneIdx + 1; i < lines.length; i++) {
    if (/^## /.test(lines[i]) || /^- [А-ЯA-Z]/.test(lines[i])) break;
    const t = lines[i].trim();
    if (/^- /.test(t) && !/^\[Task:/.test(t.slice(2)) && t.length >= 4) filled++;
  }
  if (filled === 0) fail("«- Сделано:» содержит только заглушку шаблона — заполни по факту");
  const stubs = lines.filter(l => /^\s+- \[(что сделали|ID сессии)/.test(l)).length;
  if (stubs > 0) console.log("  [WARN] в «Отклонения/Перенесено» остались подсказки-заглушки (" + stubs + ") — проверь осознанность");
  console.log("  [PASS] отчёт: «Сделано» — " + filled + " п.");
' "$SESSION_FILE" || exit 1

# --- ветка: закрытие не на защищённых (гварды pre-commit не пробьёшь, но объясняем сразу) ---
BRANCH="$(git branch --show-current)"
case "$BRANCH" in
  main|dev) echo "ERROR: close-session на защищённой ветке «$BRANCH» — работа закрывается в рабочей ветке" >&2; exit 1 ;;
esac

# --- грязь вне allowlist = FAIL до мутаций (I3): коммит закрытия ведёт только
# ритуальные артефакты; код и прочее закоммичены ДО close-session (End of Session).
# porcelain -z: пути с пробелами/юникодом не ломаются; rename = пара полей.
# MSG исключается по относительному И нормализованному пути (./x, абсолютный) ---
DIRTY_OUT=0
DIRTY_LIST=""
MSG_EXCLUDE_2=""
if [ -n "$MSG" ]; then
  MSG_ABS="$(readlink -f "$MSG" 2>/dev/null || printf '%s' "$MSG")"
  MSG_EXCLUDE_2="${MSG_ABS#"$ROOT"/}"
fi
while IFS= read -r -d '' entry; do
  st="${entry:0:2}"
  path="${entry:3}"
  if [[ "$st" == R* || "$st" == C* ]]; then
    read -r -d '' _oldpath || true   # парное поле rename: старый путь
  fi
  if [ "$path" = "$MSG" ] || { [ -n "$MSG_EXCLUDE_2" ] && [ "$path" = "$MSG_EXCLUDE_2" ]; }; then
    continue
  fi
  case "$path" in
    solution|solution/*|progress.md|feature_list.json|DECISIONS.md|docs/quality-document.md) ;;
    *) DIRTY_OUT=$((DIRTY_OUT + 1)); DIRTY_LIST="$DIRTY_LIST $path" ;;
  esac
done < <(git status --porcelain -z)
if [ "$DIRTY_OUT" -gt 0 ]; then
  echo "ERROR: незакоммиченные изменения вне allowlist коммита закрытия:$DIRTY_LIST" >&2
  echo "       закоммить их ДО close-session (End of Session: код коммитится в волне фичи, не в ритуале закрытия)" >&2
  exit 1
fi
if [ -n "$MSG" ]; then
  echo "  [PASS] рабочее дерево: вне allowlist — только MSG-файл"
else
  echo "  [PASS] рабочее дерево: вне allowlist изменений нет"
fi

DATE="$(date +%F)"
SUMMARY="$(printf '%s' "$SUMMARY" | tr '\n' ' ' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
if [ -z "$SUMMARY" ]; then
  case "$ROLE" in
    закрывает)  SUMMARY="${FEATS_CSV} — закрыто (см. отчёт в sessions/archive/${S}_session.md)" ;;
    продолжает) SUMMARY="${FEATS_CSV}: фича active, продолжение зафиксировано в progress.md" ;;
    harness)    SUMMARY="harness-сессия закрыта (см. отчёт в sessions/archive/${S}_session.md)" ;;
  esac
fi

echo "  план: статус шапки ◐ → ✅ $DATE; реестр | $S | → ✅; «Текущее состояние» → 1 строка;"
echo "        make archive; git add (allowlist) + commit -F $MSG$([ "$PUSH" -eq 1 ] && echo " + push")"
if [ "$DRY_RUN" -eq 1 ]; then
  echo "dry-run: мутаций и clean-state не выполнено."
  exit 0
fi

# --- clean-state protocol — pre-flight ДО мутаций (I1): полный ./init.sh внутри ---
if [ "${CLOSE_SESSION_CLEANSTATE:-1}" = "1" ]; then
  bash scripts/clean-state-check.sh . "$FEATS_CSV" || {
    echo "ERROR: clean-state-check не прошёл — мутаций не было (pre-flight), исправь FAIL выше" >&2
    exit 1
  }
else
  echo "  [WARN] clean-state пропущен (self-test hook CLOSE_SESSION_CLEANSTATE=0) — в ручных прогонах не задавать"
fi

# --- мутации: шапка сессии, реестр, «Текущее состояние» (node; атомарно: все
# преобразования в памяти, обе записи — после; концы строк файлов сохраняются) ---
node -e '
const fs = require("fs");
const [sessPath, regPath, s, date, summary] = process.argv.slice(1);
const readLines = (p) => {
  const raw = fs.readFileSync(p, "utf8");
  return { eol: raw.includes("\r\n") ? "\r\n" : "\n", lines: raw.split(/\r?\n/) };
};

const sess = readLines(sessPath);
const reg = readLines(regPath);

const HEAD_SCAN = 20; // синхронно с guard-subagents.sh/archive.sh
// Предикат статуса — тот же гибкий, что в bash pre-flight (Статус: ◐, допускает
// пробелы; иначе строка проходит pre-flight и падает уже после clean-state)
const stRe = /Статус:[ \t]*◐[^\r\n|]*/;
const stIdx = sess.lines.findIndex((l, i) => i < HEAD_SCAN && stRe.test(l));
if (stIdx === -1) throw new Error("шапка сессии: строка «Статус: ◐ …» не найдена в первых " + HEAD_SCAN + " строках");
sess.lines[stIdx] = sess.lines[stIdx].replace(stRe, "Статус: ✅ " + date);

const rowIdx = reg.lines.findIndex(l => l.startsWith("| " + s + " |"));
if (rowIdx === -1) throw new Error("реестр: строка | " + s + " | не найдена");
const cellRe = /\|[ ]*(?:⬜|◐[^|]*)[ \t]*\|[ \t]*$/;
if (!cellRe.test(reg.lines[rowIdx])) throw new Error("реестр: статус-ячейка строки | " + s + " | не ⬜/◐: …" + reg.lines[rowIdx].slice(-40));
reg.lines[rowIdx] = reg.lines[rowIdx].replace(cellRe, "| ✅ " + date + " |");

const secIdx = reg.lines.findIndex(l => l.trim() === "## Текущее состояние");
if (secIdx === -1) throw new Error("реестр: раздел «Текущее состояние» не найден");
let end = reg.lines.findIndex((l, i) => i > secIdx && /^## /.test(l));
if (end === -1) end = reg.lines.length;
const newLine = "- Последняя ✅: " + s + " — " + date + ": " + summary;
let last = -1, insertAt = secIdx;
for (let i = secIdx; i < end; i++) {
  if (/^- Последняя ✅/.test(reg.lines[i])) last = i;
  if (/^>/.test(reg.lines[i])) insertAt = i + 1;
}
if (last !== -1) reg.lines[last] = newLine; else reg.lines.splice(insertAt, 0, newLine);

fs.writeFileSync(sessPath, sess.lines.join(sess.eol));
fs.writeFileSync(regPath, reg.lines.join(reg.eol));
console.log("  статус шапки: ◐ → ✅ " + date);
console.log("  реестр: | " + s + " | → ✅ " + date);
console.log("  «Текущее состояние»: " + s + " (одна строка — старые подрежет make archive)");
' "$SESSION_FILE" "$SESSIONS_MD" "$S" "$DATE" "$SUMMARY" || exit 1

# --- archive + коммит (сбой здесь — после мутаций: восстановление по docs/AGENT-OPERATIONS.md) ---
bash scripts/archive.sh || exit 1

git add -- solution progress.md feature_list.json DECISIONS.md docs/quality-document.md
if git diff --cached --quiet; then
  echo "ERROR: после add нет staged-изменений — коммитить нечего (артефакты уже закоммичены?)" >&2
  exit 1
fi
if ! git commit -F "$MSG"; then
  echo "ERROR: git commit не прошёл (вывод хуков выше)" >&2
  exit 1
fi
COMMIT_SHA="$(git rev-parse --short HEAD)"
echo "  commit: $(git log --oneline -1 | head -c 100)"
if [ "$PUSH" -eq 1 ]; then
  if git push origin HEAD >/dev/null; then
    echo "  push: origin/${BRANCH:-detached HEAD}"
  else
    echo "ERROR: git push не прошёл — коммит $COMMIT_SHA локален, отправь вручную (git push origin ${BRANCH:-HEAD})" >&2
    exit 1
  fi
fi

echo ""
echo "End of Session: сессия $S закрыта (${BRANCH:-detached HEAD} @ $COMMIT_SHA). Репо рестартуем — следующая сессия с ./init.sh."

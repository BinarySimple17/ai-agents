#!/usr/bin/env bash
# scripts/archive.sh — перенос закрытых сессий и беклогов в archive/ (см. solution/README.md).
#
# Запускается при закрытии сессии (End of Session в AGENTS.md): make archive.
#   1. Сессия закрыта, если в шапке файла (первые 20 строк) «Статус:» содержит ✅ —
#      файл переносится solution/sessions/ → solution/sessions/archive/.
#      S00_session.md (канон) и TEMPLATE_session.md не архивируются никогда.
#   2. Беклог закрыт, если в шапке «Статус:» содержит «закрыт» —
#      solution/backlog/BL-*.md → solution/backlog/archive/.
#   3. Анти-раздувание: раздел «Текущее состояние» в solution/sessions.md приводится
#      к ОДНОЙ строке статуса последней закрытой сессии (подробности — в файле сессии).
#
# Скрипт идемпотентен: повторный запуск ничего не ломает. Требуется node.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SESSIONS="$ROOT/solution/sessions"
BACKLOG="$ROOT/solution/backlog"
SESSIONS_MD="$ROOT/solution/sessions.md"

moved=0

# ── 1. Закрытые сессии → sessions/archive/ ─────────────────────────────────────
mkdir -p "$SESSIONS/archive"
for f in "$SESSIONS"/*.md; do
  [ -e "$f" ] || break
  name="$(basename "$f")"
  case "$name" in
    S00_session.md|TEMPLATE_session.md) continue ;;
  esac
  if head -20 "$f" | grep -q 'Статус:.*✅'; then
    mv "$f" "$SESSIONS/archive/"
    echo "archived: solution/sessions/archive/$name"
    moved=$((moved + 1))
  fi
done

# ── 2. Закрытые беклоги → backlog/archive/ ─────────────────────────────────────
mkdir -p "$BACKLOG/archive"
for f in "$BACKLOG"/BL-*.md; do
  [ -e "$f" ] || break
  name="$(basename "$f")"
  if head -10 "$f" | grep -qi 'Статус:.*закрыт'; then
    mv "$f" "$BACKLOG/archive/"
    echo "archived: solution/backlog/archive/$name"
    moved=$((moved + 1))
  fi
done

# ── 3. «Текущее состояние» — только последняя закрытая сессия ─────────────────
command -v node >/dev/null 2>&1 || { echo "ERROR: node required (see verify-feature.sh)" >&2; exit 2; }
node -e '
const fs = require("fs");
const p = process.argv[1];
let md = fs.readFileSync(p, "utf8");
const start = md.indexOf("## Текущее состояние");
if (start === -1) { console.log("sessions.md: section not found — skip"); process.exit(0); }
const next = md.indexOf("\n## ", start + 1);
const end = next === -1 ? md.length : next + 1;
const section = md.slice(start, end);
const keep = (section.match(/^[- ]*Последняя ✅.*$/gm) || []).slice(-1);
const rebuilt =
  "## Текущее состояние\n\n" +
  "> Обновляется при закрытии сессии (clock-out — `AGENTS.md`); проверка — `make check`.\n" +
  "> В разделе — ТОЛЬКО статус последней закрытой сессии; подробности исполнения —\n" +
  "> в файле самой сессии (закрытые переносятся `make archive` в sessions/archive/).\n" +
  (keep.length ? "\n" + keep[0] + "\n" : "\n");
md = md.slice(0, start) + rebuilt + md.slice(end).replace(/^\n/, "");
fs.writeFileSync(p, md);
console.log("sessions.md: «Текущее состояние» → " + (keep.length ? "1 строка (последняя закрытая)" : "пусто (нет закрытых сессий)") + (keep.length > 1 ? " (удалено старых строк: " + (keep.length - 1) + ")" : ""));
' "$SESSIONS_MD"

echo ""
if [ "$moved" -eq 0 ]; then
  echo "Nothing to archive (нет файлов со статусом ✅/закрыт вне archive/)."
else
  echo "Archived: $moved file(s)."
fi

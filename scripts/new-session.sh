#!/usr/bin/env bash
# scripts/new-session.sh — механическая часть ПЛАНИРОВАНИЯ сессии
# (AGENTS.md Startup Workflow шаг 4; solution/README.md «Жизненный цикл», п. 1):
# копия TEMPLATE_session.md под ID + строка в реестре solution/sessions.md.
# Нарратив (предусловия, задачи с самопроверками, ловушки, правила) заполняет
# агент — скрипт делает только заготовку и шапку; подсказки-комментарии шаблона
# сохраняются до заполнения (их удаление — часть заполнения).
#
# Использование:
#   make new-session S=S12 TITLE="check_cost: EXPLAIN-порог" F=feat-012 [ROLE=продолжает] [FLAGS=--dry-run]
#   bash scripts/new-session.sh S=S12 TITLE="..." F=feat-012 ROLE=закрывает --dry-run
#
#   S      — ID сессии (обяз.; файл solution/sessions/<S>_session.md)
#   TITLE  — краткое название (обяз.; шапка файла + колонка «Этап» реестра)
#   F      — фича/фичи сессии через запятую (опц.; имена — из feature_list.json)
#   ROLE   — закрывает (дефолт) | продолжает — роль сессии по фиче
#   --dry-run — показать план без записи файлов
#
# Exit: 0 — создано (или план dry-run), 1 — валидация не прошла, 2 — некорректный вызов.
# node обязателен (как в archive.sh) — текстовые мутации без sed-экранирования.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SESSIONS_DIR="solution/sessions"
TEMPLATE="$SESSIONS_DIR/TEMPLATE_session.md"
SESSIONS_MD="solution/sessions.md"

S=""
TITLE=""
F=""
ROLE="закрывает"
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    S=*) S="${arg#S=}" ;;
    TITLE=*) TITLE="${arg#TITLE=}" ;;
    F=*) F="${arg#F=}" ;;
    ROLE=*) ROLE="${arg#ROLE=}" ;;
    *) echo "ERROR: неизвестный аргумент «$arg» (ожидание: S= TITLE= F= ROLE= --dry-run)" >&2; exit 2 ;;
  esac
done

# Пороги/литералы логики — именованные константы (AGENTS.md)
TITLE_MAX_LEN=80   # защита от простыни в шапке реестра: название — не отчёт

# make-слой передаёт ROLE="$(ROLE)" всегда — пустая строка затирает дефолт (ревью C1)
[ -z "$ROLE" ] && ROLE="закрывает"
[ -z "$S" ] && { echo "ERROR: S обязателен. Пример: make new-session S=S12 TITLE=\"check_cost\" F=feat-012" >&2; exit 2; }
printf '%s' "$S" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9_-]*$' || {
  echo "ERROR: S — латиница/цифры/дефис/подчёркивание, без путей (получено: «$S»)" >&2; exit 2;
}
[ -z "$TITLE" ] && { echo "ERROR: TITLE обязателен" >&2; exit 2; }
[ "${#TITLE}" -gt "$TITLE_MAX_LEN" ] && { echo "ERROR: TITLE длиннее $TITLE_MAX_LEN символов — название, не отчёт" >&2; exit 2; }
case "$TITLE" in
  *'|'*|*[$'\r\n']*) echo "ERROR: TITLE не должен содержать «|» или переводов строк (ломает шапку/реестр)" >&2; exit 2 ;;
esac
case "$ROLE" in
  закрывает|продолжает) ;;
  *) echo "ERROR: ROLE = закрывает|продолжает (получено: «$ROLE»)" >&2; exit 2 ;;
esac

SESSION_FILE="$SESSIONS_DIR/${S}_session.md"
[ -f "$TEMPLATE" ] || { echo "ERROR: шаблон не найден: $TEMPLATE" >&2; exit 1; }
if [ -f "$SESSION_FILE" ] || [ -f "$SESSIONS_DIR/archive/${S}_session.md" ]; then
  echo "ERROR: сессия $S уже существует ($SESSION_FILE или в $SESSIONS_DIR/archive/) — new-session не перезаписывает" >&2
  exit 1
fi
command -v node >/dev/null 2>&1 || { echo "ERROR: node required (как в archive.sh)" >&2; exit 2; }

# Имена фич — из feature_list.json (источник истины); несуществующая фича = отказ
FEATS_CELL=""
if [ -n "$F" ]; then
  FEATS_CELL="$(node -e '
    const fs = require("fs");
    const fl = JSON.parse(fs.readFileSync("feature_list.json", "utf8"));
    const ids = (process.argv[1] || "").split(",").map(s => s.trim()).filter(Boolean);
    if (ids.length === 0) { console.error("ERROR: пустой список фич"); process.exit(1); }
    const parts = ids.map(id => {
      const f = fl.features.find(x => x.id === id);
      if (!f) { console.error("ERROR: фича " + id + " не найдена в feature_list.json"); process.exit(1); }
      return id + " — " + f.name;
    });
    console.log(parts.join("; "));
  ' "$F")" || exit 1
fi

ROLE_TEXT="финальная сессия, через make verify-feature"
[ "$ROLE" = "продолжает" ] && ROLE_TEXT="промежуточная, фича остаётся active"

if [ "$DRY_RUN" -eq 1 ]; then
  echo "=== new-session [dry-run] ==="
  echo "  создать:  $SESSION_FILE (из TEMPLATE_session.md)"
  echo "  шапка:    # $S — $TITLE"
  [ -n "$F" ] && { echo "  Фичи:     $FEATS_CELL"; echo "  Роль:     $ROLE $F — $ROLE_TEXT"; }
  grep -q "^| $S |" "$SESSIONS_MD" \
    && echo "  реестр:   строка | $S | уже есть — не дублируется" \
    || echo "  реестр:   добавить строку | $S | (после последней сессии таблицы)"
  echo "  статусы:  файл «[ ] не начата», реестр ⬜ — старт-гейт кусает только при «◐ в работе» (D-027)"
  echo "  далее:    агент заполняет предусловия/задачи/ловушки, удаляет подсказки-комментарии"
  exit 0
fi

node -e '
const fs = require("fs");
const [tpl, out, sessionsMd, s, title, featsCell, role, featIds, roleText] = process.argv.slice(1);
const plain = (text, from, to) => { if (!text.includes(from)) throw new Error("шаблон изменился: не найдено «" + from + "»"); return text.split(from).join(to); };

// Шаблон: строковые замены по месту — концы строк файла сохраняются как есть
let md = fs.readFileSync(tpl, "utf8");
md = plain(md, "# 00N — [Краткое название сессии]", "# " + s + " — " + title);
// Легенда трёх состояний → чистый статус «запланирована»: классификатор гварда
// распознаёт planned только по строке, кончающейся на «не начата» (иначе = started)
md = plain(md, "Статус: [ ] не начата · ◐ в работе · [x] ✅ ГГГГ-ММ-ДД", "Статус: [ ] не начата");
if (featsCell) {
  md = plain(md, "Фичи: [feat-0XX — Name; их dependencies уже в passing]", "Фичи: " + featsCell + "; их dependencies уже в passing");
  md = plain(md,
    "Роль по фиче: [закрывает feat-0XX — финальная сессия, через make verify-feature | продолжает feat-0XX — промежуточная, фича остаётся active]",
    "Роль по фиче: " + role + " " + featIds + " — " + roleText);
} else {
  // Конвенция harness-сессий без фич (S21/S22/S27): без плейсхолдера — иначе close-session
  // на незакрываемой строке упадёт (ревью I4)
  const NO_FEATS = "— (сессия без фичи, states не меняются)";
  md = plain(md, "Фичи: [feat-0XX — Name; их dependencies уже в passing]", "Фичи: " + NO_FEATS);
  md = plain(md,
    "Роль по фиче: [закрывает feat-0XX — финальная сессия, через make verify-feature | продолжает feat-0XX — промежуточная, фича остаётся active]",
    "Роль по фиче: " + NO_FEATS);
}
fs.writeFileSync(out, md);
console.log("created: " + out);

// Реестр: вставка строки после последней сессии таблицы; концы строк файла сохраняются
const raw = fs.readFileSync(sessionsMd, "utf8");
if (!raw.includes("| " + s + " |")) {
  const row = "| " + s + " | [sessions/" + s + "_session.md](sessions/" + s + "_session.md) | " + title
    + " | агент | [S-ID закрытых сессий] | " + (featIds ? featIds + " — " + role : "— (сессия без фичи, states не меняются)") + " | [оценка] | [P0-P2] | ⬜ |";
  const eol = raw.includes("\r\n") ? "\r\n" : "\n";
  const lines = raw.split(/\r?\n/);
  let at = -1;
  for (let i = 0; i < lines.length; i++) if (/^\| S\d/.test(lines[i])) at = i;
  if (at === -1) throw new Error("в " + sessionsMd + " нет ни одной строки сессии (| S… |) — точка вставки не найдена");
  lines.splice(at + 1, 0, row);
  fs.writeFileSync(sessionsMd, lines.join(eol));
  console.log("registry: строка | " + s + " | добавлена после | " + (lines[at].split("|")[1] || "").trim() + " | (плейсхолдеры [deps]/[оценка]/[P0-P2] заполнить)");
} else {
  console.log("registry: строка | " + s + " | уже есть — не дублируется");
}
' "$TEMPLATE" "$SESSION_FILE" "$SESSIONS_MD" "$S" "$TITLE" "$FEATS_CELL" "$ROLE" "$F" "$ROLE_TEXT" || exit 1

echo ""
echo "Дальше — агент: заполнить предусловия/задачи/ловушки, удалить подсказки-комментарии;"
echo "старт сессии — шапка → «◐ в работе» + ARCH-GATE до кода (make check красный до заполнения, D-027)."

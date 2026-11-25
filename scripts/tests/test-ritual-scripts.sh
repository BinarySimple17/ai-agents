#!/usr/bin/env bash
# scripts/tests/test-ritual-scripts.sh — self-test ритуальных скриптов
# new-session.sh / close-session.sh (ревью S28: I6 — скрипты мутируют git-состояние,
# ручной прогон регрессом не заменяется).
#
# Механика: одноразовый git worktree --detach от HEAD в mktemp-каталоге — все мутации
# в песочнице, рабочий checkout не трогается. Тестируемые скрипты и Makefile
# копируются из ИСХОДНОГО дерева (гейт проверяет текущий код, не последний коммит).
# Кейсы с clean-state: I1 — с честным clean-state (красным в песочнице по посторонней
# причине: нет .env), но с сентинелом RITUAL_SELFTEST — вложенный init.sh НЕ рекурсивно
# зовёт этот же self-test (ревью I-2); причина отказа I1 доказывается grep'ом вывода
# («progress.md has been updated»), не только кодом выхода. I3 — с
# CLOSE_SESSION_CLEANSTATE=0: единственный источник exit 1 — dirty-check (ревью I-1:
# без этого кейс зелёный даже без фикса, clean-state краснеет сам по себе).
#
# Кейсы:
#   C1-регресс: make/direct new-session без ROLE (make передаёт пустую строку) → exit 0
#   I4: new-session без F → в шапке и реестре «— (сессия без фичи…)», не плейсхолдер
#   fail-closed: close-session на planned-сессии → exit 1, файл не мутирует
#   I3-регресс: грязь вне allowlist → exit 1 ДО мутаций, шапка остаётся ◐
#   I1-регресс: красный clean-state (placeholder progress.md) → exit 1 ДО мутаций
#   пустой MSG-файл → exit 2 до мутаций
#   EOL: вставка в CRLF-реестр сохраняет CRLF (Windows-чекахаут)
#   C1 (ревью S30): коммит в песочнице при стерильном git-конфиге (env-identity —
#               GIT_CONFIG_GLOBAL/SYSTEM → пустые файлы = чистый CI-раннер)
#   happy path: полный цикл close-session (статус/реестр/«Текущее состояние»/archive/
#               commit из MSG-файла) в песочнице
#
# Exit: 0 — все кейсы зелёные, 1 — есть провалы. Вызывается из ./init.sh.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

PASS=0
FAIL=0
ok()  { echo "  [PASS] $1"; PASS=$((PASS + 1)); }
bad() { echo "  [FAIL] $1"; FAIL=$((FAIL + 1)); }
assert_rc()  { local want="$1" desc="$2"; shift 2; "$@" >/dev/null 2>&1; local rc=$?; [ "$rc" -eq "$want" ] && ok "$desc" || bad "$desc (exit $rc, ожидался $want)"; }
assert_grep() { local desc="$1"; shift; grep -q "$@" 2>/dev/null && ok "$desc" || bad "$desc"; }

WORK="$(mktemp -d)"
cleanup() { git worktree remove --force "$WORK" >/dev/null 2>&1; rm -rf "$WORK" 2>/dev/null; }
trap cleanup EXIT

# Ревью S30 C1: CI-раннер и свежий клон без глобального git-identity — setup-коммит
# и happy-path close-session (git commit внутри песочницы) падали бы rc=128
# «Author identity unknown». env-identity герметична: не пишет в config (worktree
# делит .git/config с основным репо — repo-local `git config` загрязнил бы его),
# приоритет env выше config — все коммиты self-test'а работают в чистом окружении.
export GIT_AUTHOR_NAME="ritual-selftest"
export GIT_AUTHOR_EMAIL="ritual-selftest@example.invalid"
export GIT_COMMITTER_NAME="ritual-selftest"
export GIT_COMMITTER_EMAIL="ritual-selftest@example.invalid"

git worktree add --detach "$WORK" HEAD >/dev/null 2>&1 || {
  echo "ERROR: не удалось создать песочницу-worktree от HEAD" >&2
  exit 1
}
echo "=== Ritual Scripts Self-Test (worktree: $WORK) ==="
cp "$ROOT/scripts/new-session.sh" "$ROOT/scripts/close-session.sh" "$WORK/scripts/"
cp "$ROOT/Makefile" "$WORK/Makefile"
# Гейт должен тестировать ТЕКУЩИЙ код, а dirty-check close-session честно отказывает
# на untracked-скриптах вне allowlist — фиксируем копии setup-коммитом в песочнице
git -C "$WORK" add scripts/ Makefile
git -C "$WORK" commit -m "self-test setup: тестируемые скрипты" >/dev/null
cd "$WORK"

# --- Регрессия C1 (ревью S30): герметичность git-identity песочницы ---
# CI-раннер и свежий клон не имеют глобального git-конфига: коммиты self-test'а
# (setup выше и happy-path close-session ниже) обязаны проходить в стерильном
# окружении. Красный на коде до фикса: rc=128 «Author identity unknown»
# (воспроизведено ревьюером в чистом HOME; локально зелёный только из-за
# глобальной конфигурации разработчика).
_identity_probe="$(mktemp)"; : > "$_identity_probe"
(
  cd "$WORK"
  export GIT_CONFIG_GLOBAL="$_identity_probe" GIT_CONFIG_SYSTEM="$_identity_probe"
  printf '\n# self-test C1: probe (ревью S30 — git-identity)\n' >> Makefile
  git add Makefile
  git commit -m "self-test C1: коммит в стерильном окружении identity" >/dev/null
  _rc=$?
  # провал регрессии не должен каскадить: песочница чиста и при красном кейсе
  git reset --hard -q HEAD >/dev/null 2>&1
  exit "$_rc"
) && ok "C1: коммит в песочнице при стерильном git-конфиге (env-identity)" \
  || bad "C1: коммит в песочнице при стерильном git-конфиге (env-identity не задана)"
rm -f "$_identity_probe"
# Ревью-2 S30 Minor-4: repo-local identity (общий .git/config с worktree) замаскировал бы
# провал регрессии — кейс остался бы зелёным даже после удаления env-identity. Fail-closed:
# обнаружено маскирование — Suite красная с внятной причиной (не «чинить» пробу config'ом)
if [ -n "$(git config --local --get user.name 2>/dev/null || true)" ]; then
  bad "C1: repo-local identity в .git/config — bite регрессии замаскирован (не задавать user.name в config проекта; env-identity self-test'а — единственный источник)"
else
  ok "C1: repo-local identity отсутствует — bite регрессии жив"
fi

# Сентинел против рекурсии self-test → clean-state → init.sh → self-test (ревью I-2)
export RITUAL_SELFTEST=1

FIX_S="S99C"
NEW_SESS=(bash scripts/new-session.sh)
CLOSE_SESS=(bash scripts/close-session.sh)

# --- C1: make-слой передаёт ROLE="" — дефолт «закрывает» должен выживать ---
assert_rc 0 "direct new-session без ROLE — dry-run exit 0 (C1-регресс)" \
  "${NEW_SESS[@]}" S=S99A TITLE=probe F=feat-011 --dry-run
if command -v make >/dev/null 2>&1; then
  assert_rc 0 "make new-session без ROLE — dry-run exit 0 (C1-регресс)" \
    make new-session S=S99A TITLE=probe F=feat-011 FLAGS=--dry-run
fi

# --- I4: сессия без F → harness-конвенция в шапке и реестре, не плейсхолдер ---
assert_rc 0 "new-session без F — exit 0" "${NEW_SESS[@]}" S=S99B TITLE="Без фич"
assert_grep "шапка S99B: роль = harness-конвенция (I4)" \
  'Роль по фиче: — (сессия без фичи, states не меняются)' solution/sessions/S99B_session.md
assert_grep "реестр: строка S99B с harness-конвенцией (I4)" \
  '— (сессия без фичи, states не меняются)' <(grep '| S99B |' solution/sessions.md)
# EOL: конвенция файла сохраняется — концы строк вставленной строки = концы строк файла
# (bash-чек $'\r', не od/grep-пайп — MSYS искажает аргументы с бэкслешами)
ROW=$(grep '| S99B |' solution/sessions.md)
FIRST_LINE=$(head -1 solution/sessions.md)
if [[ "$ROW" == *$'\r'* && "$FIRST_LINE" == *$'\r'* ]] || [[ "$ROW" != *$'\r'* && "$FIRST_LINE" != *$'\r'* ]]; then
  ok "вставленная строка реестра сохранила концы строк файла (EOL-preserving)"
else
  bad "EOL вставленной строки ($ROW) не совпал с конвенцией файла"
fi

# --- fail-closed: planned-сессия не закрывается ---
assert_rc 1 "close-session на planned-сессии — отказ (exit 1)" "${CLOSE_SESS[@]}" S=S99B --dry-run
assert_grep "planned-сессия не мутировала" 'Статус: \[ \] не начата' solution/sessions/S99B_session.md

# --- фикстура закрывающей сессии: create + ◐ + гейты + отчёт (feat-001 уже passing в HEAD) ---
assert_rc 0 "new-session фикстуры S99C (закрывает feat-001)" \
  "${NEW_SESS[@]}" S=$FIX_S TITLE="Фикстура закрытия" F=feat-001
node -e '
const fs = require("fs");
const plain = (t, from, to, what) => { if (!t.includes(from)) throw new Error(what); return t.split(from).join(to); };
let md = fs.readFileSync("solution/sessions/S99C_session.md", "utf8");
md = plain(md, "| Статус: [ ] не начата", "| Статус: ◐ в работе", "status");
md = plain(md, "- ARCH-GATE: [✅ <verdict: compliant | violations | needs-decision> + <план/файл> | N/A — <причина>]", "- ARCH-GATE: ✅ compliant — self-test ritual-scripts", "arch");
md = plain(md, "- REVIEW-GATE: [✅ <Ready to merge? вердикт> + <BASE7>..<HEAD7> | N/A — <причина>]", "- REVIEW-GATE: ✅ Ready to merge: Yes + abcdef12345 (self-test)", "review");
md = plain(md, "  - [Task: что реально сделано, коммиты, число тестов/ассертов, live-прогон]", "  - Self-test: механика close-session в песочнице", "report");
fs.writeFileSync("solution/sessions/S99C_session.md", md);
' || bad "фикстура S99C не собралась (node)"
printf '%s' "self-test: механика закрытия сессий в песочнице" > close-msg.txt

# --- I3: грязь вне allowlist → отказ до мутаций (dirty-check идёт раньше clean-state;
# clean-state глушим — иначе песочница краснеет сама по себе и кейс ложноположителен,
# ревью I-1) ---
printf 'rogue\n' > rogue.txt
assert_rc 1 "грязь вне allowlist → отказ (exit 1, I3-регресс)" \
  env CLOSE_SESSION_CLEANSTATE=0 "${CLOSE_SESS[@]}" S=$FIX_S MSG=close-msg.txt
assert_grep "после отказа мутаций нет — шапка всё ещё ◐ (I3)" 'Статус: ◐ в работе' solution/sessions/${FIX_S}_session.md
rm -f rogue.txt

# --- I1: красный clean-state (placeholder progress.md) → отказ до мутаций; причина —
# именно clean-state — доказывается текстом вывода, не только кодом выхода (ревью I-1) ---
printf '**Last Updated:** YYYY-MM-DD\n' > progress.md
I1_OUT="$("${CLOSE_SESS[@]}" S=$FIX_S MSG=close-msg.txt 2>&1)"; I1_RC=$?
[ "$I1_RC" -eq 1 ] && ok "красный clean-state → отказ до мутаций (I1-регресс, exit 1)" \
  || bad "I1-регресс (exit $I1_RC, ожидался 1)"
if grep -q "progress.md has been updated" <<<"$I1_OUT"; then
  ok "причина отказа — clean-state по progress.md (I1, bite-доказуемо)"
else
  bad "в выводе нет отказа по progress.md — причина отказа не доказана (I1)"
fi
assert_grep "после clean-state-отказа мутаций нет — шапка ◐ (I1)" 'Статус: ◐ в работе' solution/sessions/${FIX_S}_session.md
git checkout -- progress.md

# --- I-3: S27-стиль роли «— …» (не буквальная «без фич») распознаётся как no-feature ---
assert_rc 0 "new-session S99D (без F)" "${NEW_SESS[@]}" S=S99D TITLE="S27-стиль роли"
sed -i 's|Статус: \[ \] не начата|Статус: ◐ в работе|' solution/sessions/S99D_session.md
sed -i 's|^Роль по фиче: .*|Роль по фиче: — (финальные сессии фич — S04/S05)|' solution/sessions/S99D_session.md
sed -i 's|^- ARCH-GATE: .*|- ARCH-GATE: ✅ compliant — self-test (S27-стиль)|' solution/sessions/S99D_session.md
sed -i 's|^- REVIEW-GATE: .*|- REVIEW-GATE: ✅ Ready to merge: Yes + abcdef12345 (self-test)|' solution/sessions/S99D_session.md
sed -i 's|^  - \[Task:.*|  - Self-test: распознавание S27-стиля роли|' solution/sessions/S99D_session.md
# MSG передаётся и в dry-run: close-msg.txt от предыдущих кейсов должен исключаться
assert_rc 0 "close-session распознаёт S27-стиль роли «— …» (I-3-регресс)" \
  "${CLOSE_SESS[@]}" S=S99D MSG=close-msg.txt --dry-run

# --- пустой MSG-файл → отказ до мутаций ---
: > empty-msg.txt
assert_rc 2 "пустой MSG-файл → отказ (exit 2)" \
  env CLOSE_SESSION_CLEANSTATE=0 "${CLOSE_SESS[@]}" S=$FIX_S MSG=empty-msg.txt
rm -f empty-msg.txt

# --- happy path: полный цикл в песочнице (clean-state глушен self-test hook'ом) ---
assert_rc 0 "полный цикл close-session — exit 0" \
  env CLOSE_SESSION_CLEANSTATE=0 "${CLOSE_SESS[@]}" S=$FIX_S MSG=close-msg.txt SUMMARY="self-test полного цикла"
[ -f "solution/sessions/archive/${FIX_S}_session.md" ] \
  && ok "файл сессии перенесён в archive/" || bad "файл сессии не уехал в archive/"
assert_grep "статус шапки → ✅" 'Статус: ✅' solution/sessions/archive/${FIX_S}_session.md
assert_grep "реестр: строка → ✅" '| S99C |.*✅' solution/sessions.md
assert_grep "«Текущее состояние» → строка последней закрытой" \
  '^[- ]*Последняя ✅: S99C — ' solution/sessions.md
assert_grep "коммит из MSG-файла создан" 'механика закрытия сессий' <(git log -1 --format=%s)

echo ""
echo "Ritual scripts self-test: $PASS passed, $FAIL failed."
[ "$FAIL" -eq 0 ] && exit 0
exit 1

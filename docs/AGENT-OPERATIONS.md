# AGENT-OPERATIONS — полный операционный объём ритуалов

Свёртка правил работы агента — в [AGENTS.md](../AGENTS.md); здесь — полный объём,
вынесенный из него для ужатия системного промпта. При конфликте приоритет у AGENTS.md.

## Branch Hygiene — полный граф веток

- `main` — только версии продукта (release-срезы, теги `vX.Y.Z`). Прямые пуши запрещены — только через PR.
- `dev` — интеграционная ветка разработки. Прямые пуши запрещены — только через PR.
- `milestone/v2` — ветка вехи V2 (ДЗ-03): отпочкована от `dev`, живёт параллельно, пока V1 (ДЗ-02) не сдана и не принята. Рабочие ветки для V2 почкуются от `milestone/v2` и мержатся в неё напрямую; сама `milestone/v2` никуда не сливается (`dev`/`main`) без явной команды владельца — после приёмки V1 реинтеграция через PR (D-024). Хотфиксы V1 — по-прежнему рабочая ветка от `dev` → PR → `dev`.
- `milestone/v3` — ветка вехи V3 (ДЗ-04 «Гибридная память»): отпочкована от `milestone/v2` (14da923), живёт параллельно, пока V2 (ДЗ-03) не сдана и не принята. Рабочие ветки для V3 почкуются от `milestone/v3` и мержатся в неё напрямую; сама `milestone/v3` никуда не сливается (`dev`/`main`) без явной команды владельца — реинтеграция через PR по модели D-024 (D-029). Хотфиксы V2 — рабочая ветка от `milestone/v2` → merge в `milestone/v2`; хотфиксы V1 — по-прежнему от `dev` → PR → `dev`.
- `milestone/v4` — ветка вехи V4 (ДЗ-05 «State machine», `hw-05-agent-logic`): отпочкована от `milestone/v3`, живёт параллельно и ждёт приёмки вехи V4. Рабочие ветки для V4 почкуются от `milestone/v4` и мержатся в неё напрямую; сама `milestone/v4` никуда не сливается (`dev`/`main`) без явной команды владельца — реинтеграция через PR по модели D-024 (D-031). Хотфиксы V3 — рабочая ветка от `milestone/v3` → merge в `milestone/v3`; хотфиксы V2/V1 — по прежним правилам.
- Вся работа — в рабочих ветках: `feat-0XX-<slug>` (фичи), `harness/<тема>` (обвязка). Пуш рабочей ветки в origin свободен; попадание в `dev`/`main` — только PR (`gh pr create --base dev`, release — `--base main`).
- Release: PR `dev` → `main` + тег версии. Хотфикс: рабочая ветка от `main` → PR → `main`, затем перенос в `dev`.
- Локальные гварды: `.githooks/pre-commit` (коммит на `main`/`dev` невозможен) и `.githooks/pre-push` (пуш в `main`/`dev` невозможен); подключаются идемпотентно в `init.sh` (`core.hooksPath`). Серверный уровень — GitHub branch protection на `main` и `dev`.

## End of Session — полный чеклист закрытия

Нарратив делает агент (шаги 1–4); шаги 5–7 — прогона `make close-session`
(clean-state — его pre-flight, рестартуемость — его итог). Скрипт не пишет
содержание: пустой нарратив = отказ закрытия (fail-closed).

1. **Нарратив-артефакты:** `progress.md` (Current State, Next Steps), `feature_list.json`
   (state + evidence: фича-«закрывает» — `make verify-feature F=<id>` до закрытия;
   промежуточная сессия оставляет фичу `active` с allowlist), `docs/quality-document.md`
   (A/B/C/D per dimension), отчёт в файле сессии («Отчёт по сессии»: Сделано /
   Отклонения от плана / Перенесено).
2. **Ревью (REVIEW-GATE):** после зелёного `make check` диспетчеризация `code-reviewer`
   по диапазону (BASE = merge-base с `main` или последний отревьюенный коммит, HEAD =
   `HEAD`); исправить Critical/Important — **каждый Critical закрывается регрессионным
   тестом** (падает на до-фиксовом коде и остаётся в наборе), re-run tests; Minor →
   `solution/backlog/`; вердикт + диапазон — в строку REVIEW-GATE файла сессии
   (guard-enforced). Отчёт ревью — по формату из определения агента
   (`~/.zcode/agents/code-reviewer.md`, вне репо): `file:line` + цитата ≤ 1 строки.
3. **Риски/блокеры:** unresolved risks or blockers → `progress.md` и `DECISIONS.md`;
   out-of-scope findings → `solution/backlog/` (BL-NN files).
4. **Сообщение коммита:** записать в файл (коммиты объясняют WHY, не только WHAT) —
   путь понадобится скрипту.
5. **`make close-session S=<ID> MSG=<файл-сообщение> [--push] [--dry-run]`** —
   механический хвост (см. «Скрипты ритуалов» ниже): проверки гейтов/отчёта/фич,
   статус шапки `◐ → ✅ дата`, строка реестра, «Текущее состояние» (одна строка),
   `make archive`, clean-state protocol, коммит из MSG-файла.
6. **Clean-state protocol:** скрипт прогоняет `scripts/guard-subagents.sh` (обе строки
   гейтов — ✅+evidence или N/A+reason) и `scripts/clean-state-check.sh` (no debug
   artifacts, рестартуемость — `./init.sh`); чеклист —
   [templates/clean-state-checklist.md](../templates/clean-state-checklist.md).
7. **Рестартуемость:** следующая сессия запускается с `./init.sh` сразу; repo must
   remain restartable from the standard startup path.

If running low on context, do not rush to finish — stop, update `progress.md`,
and commit a clean checkpoint. Cleanup is dual-mode: immediate cleanup after every
session + periodic (weekly) full sweep for structural drift.

## Скрипты ритуалов

### `make new-session` — механическая часть планирования

```bash
make new-session S=S12 TITLE="check_cost: EXPLAIN-порог" F=feat-012          # закрывает (дефолт)
make new-session S=S14 TITLE="diagnose_error + ретраи" F=feat-014 ROLE=продолжает
make new-session S=S30 TITLE="Хотфикс-сборка" FLAGS=--dry-run               # план без записи
```

Делает: копию `TEMPLATE_session.md` → `solution/sessions/<S>_session.md` (шапка:
заголовок, строка «Фичи» из `feature_list.json` по имени, «Роль по фиче»), строку в
таблицу `solution/sessions.md` (если её ещё нет). НЕ делает (за агентом): предусловия,
задачи с самопроверками, ловушки, правила сессии, подсказки-комментарии удалить.
Сессия создаётся в статусе `[ ] не начата` — старт-гейт (D-027) кусает только при
переводе шапки в «◐ в работе».

### `make close-session` — механический хвост End of Session

```bash
make close-session S=S11 MSG=close-msg.txt SUMMARY="feat-011 → passing" FLAGS=--push
make close-session S=S11 MSG=close-msg.txt FLAGS=--dry-run                   # pre-flight (без clean-state) + план без мутаций
```

Pre-flight — ВСЕ проверки до первой мутации (fail-closed): сессия в статусе
«◐ в работе»; строка «Роль по фиче:» канонична (закрывает/продолжает `feat-0XX`,
либо «— (сессия без фичи, states не меняются)» для harness-сессий); консистентность
`feature_list.json` с ролью («закрывает» → все фичи сессии `passing`; «продолжает» →
`active`); обе строки гейтов (`scripts/guard-subagents.sh`, end-фаза, явный файл);
отчёт «Отчёт по сессии» заполнен (не заглушка); MSG-файл существует и непуст;
рабочее дерево чистое вне allowlist коммита (`solution/`, `progress.md`,
`feature_list.json`, `DECISIONS.md`, `docs/quality-document.md` + сам MSG) — код и
прочее закоммичены ДО close-session; clean-state protocol (полный `./init.sh`
внутри; self-test hook `CLOSE_SESSION_CLEANSTATE=0` — только для
`scripts/tests/test-ritual-scripts.sh`).

Мутации (атомарны в памяти; концы строк правимых файлов сохраняются): статус шапки →
`✅ <дата>`; строка реестра `solution/sessions.md` → `✅ <дата>`; «Текущее состояние» →
одна строка `Последняя ✅: <S> — <дата>: <SUMMARY>`; `make archive` (файл сессии →
`sessions/archive/`, закрытые беклоги → `backlog/archive/`); git add по allowlist +
`git commit -F <MSG>`; с `--push` — `git push origin HEAD` (пуш рабочей ветки свободен;
в `main`/`dev` гварды не пустят; неудавшийся push — явный ERROR, exit 1).

**Восстановление при сбое после мутаций** (archive/commit упал): файл из
`sessions/archive/` вернуть в `sessions/`, статус шапки → «◐ в работе», ячейку реестра →
`⬜`/`◐`, «Текущее состояние» → прежняя строка; устранить причину; перезапустить
close-session. `--dry-run` гоняет весь pre-flight, кроме clean-state, без мутаций.

Self-test обоих скриптов — `bash scripts/tests/test-ritual-scripts.sh` (внутри
`./init.sh`): 20 assert-ов в одноразовом worktree-песочнице, включая регрессии ревью
(C1 — дефолт ROLE, I1 — pre-flight clean-state, I3 — грязь вне allowlist, I4 —
harness-конвенция «без фич», EOL-preserving).

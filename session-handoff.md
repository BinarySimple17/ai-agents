# Session Handoff

<!-- ЗАПОЛНЯЕТСЯ В КОНЦЕ КРУПНОЙ СЕССИИ: снимок состояния для передачи
     следующей сессии. Для мелких задач достаточно обновить progress.md. -->

## Current Objective

- Goal: механический контур защит до код-сессий долга (S31–S35) — import-linter, CI, документация контура; Task 5 (серверная защита веток) снят владельцем (D-034)
- Current status: волна (8a99ceb, 4a4b572, b0a1d99) + fix-волна (10447e6) + финализация; REVIEW-GATE: ревью-1 «With fixes» 1C/2I/10M → re-review `b0a1d99..10447e6` **«Ready to merge: Yes» 0C/0I/5M** (миноры финализации закрыты); `make close-session S=S30` → merge `harness/mechanical-guards` → `milestone/v4`
- Branch / commit: `harness/mechanical-guards` от `milestone/v4` (`3f68c9c` + волна сессии)

## Completed This Session

- [x] **ARCH-GATE**: canon-check архитектора — violations → rulings F1–F10 ДО кода (план дополнен: контракты F1–F3, CI F6/F7, доки F4/F8/F9, беклог F10)
- [x] **Task 1**: import-linter 2.17 (dev), 10 контрактов в `[tool.importlinter]` (два layers, independence, 7 forbidden — производные F1/F2 помечены в именах), `lint-imports` в init.sh + `make lint-imports`; негативный контроль ×2 RED; Contracts: 10 kept, 0 broken
- [x] **Task 2**: build_documents → `composition/knowledge_docs.py` (нарушение apps→infrastructure из аудита устранено; parents[3] — F5); guard-тест перепривязан; живой прогон — 324 документа (= S27)
- [x] **Task 3**: `.github/workflows/ci.yml` compose-based (`up -d --wait`; cp .env.example .env — F6; setup-python 3.13 — F7); neo4j-хелсчек HTTP→bolt в compose; yamllint OK; `--wait` Healthy bite-проверено локально
- [x] **Task 4**: **D-034**, FIX-018 (c4-l2 выровнен с independence — F4), синх AGENTS×2/AGENT-OPERATIONS/ARCHITECTURE/scripts README/quality-document, BL-035/036/037 + реестр
- [x] **Task 5 СНЯТ** владельцем до старта (D-034): branch protection платная на private-репо, включаться не будет — «Прямые пуши запрещены» = локальные хуки `.githooks/` (проверены: pre-commit/pre-push блокируют main/dev и force-push через запрет любых пушей) + CI как контур наблюдаемости
- [x] **Fix-волна + re-review**: C1 git-identity — регресс в ritual self-test (стерильный GIT_CONFIG_GLOBAL/SYSTEM; bite 23+1F → 24P/0F; env-identity — worktree делит config, repo-local нельзя) + git-config шаг в ci.yml; I1 apps→{orchestration,config} в контракт (контроль RED); I2 живой CI отложено с ruling (критерий 3); M1–M10 закрыты/BL-038; re-review «Ready to merge: Yes» 0C/0I/5M — миноры финализации (85/25, quality ×4 RED, c4-l2 три запрета, assert repo-local identity)

## Verification Evidence

| Check | Command | Result | Notes |
|---|---|---|---|
| Старт-гейт | `bash init.sh` (start-фаза) | PASS | ARCH-GATE canon-check ✅ учтён |
| Контракты слоёв | `uv run lint-imports` | 10 kept, 0 broken | негативный контроль ×2 RED (application→orchestration; apps→infrastructure) |
| Живая переиндексация | `make infra-migrate && make index-knowledge` | 324 дока (= S27) | поведение фабрики не изменилось |
| Compose | `docker compose config -q` + `up -d --wait` | OK; neo4j Healthy | bolt-хелсчек bite-проверен (контейнер пересоздан) |
| ci.yml | `uvx yamllint` | exit 0 | живой CI-прогон отложен — сеть |
| Полная верификация | `make check` | EXIT=0 | 899 passed + self-tests 85/25 (ritual 25 после Minor-4 assert) + lint-imports |

## Files Changed

- `pyproject.toml` (+import-linter 2.17, +[tool.importlinter] 10 контрактов), `uv.lock`, `init.sh` (+lint-imports), `Makefile` (+lint-imports)
- `src/text_to_sql_agent/composition/knowledge_docs.py` (новая фабрика), `src/apps/index_knowledge.py` (переезд на фабрику), `src/tests/unit/test_knowledge_sources.py` (патч-цель — F5)
- `.github/workflows/ci.yml` (новый), `docker-compose.yml` (neo4j healthcheck → bolt)
- `DECISIONS.md` (D-034), `solution/FIXES.md` (FIX-018), `AGENTS.md` (×3), `docs/AGENT-OPERATIONS.md`, `docs/ARCHITECTURE.md`, `docs/diagrams/c4-l2-container.md`, `scripts/README.md`, `docs/quality-document.md`, `solution/backlog/BL-035…037` + реестр

## Decisions Made

- **D-034** — серверная защита веток на GitHub не включается (платный тариф private-репо, решение владельца 2026-11-25 в диалоге старта): контракт «Прямые пуши запрещены» = локальные хуки + CI процедурно; prd CI-гейт ACL-leak остаётся требованием, исполнение — при слиянии. Task 5 снят до старта работ

## Blockers / Risks

- ~~Сеть/живой CI~~ — ЗАКРЫТО 2026-11-25 (S30-хвост, по команде владельца): сеть восстановлена, gh-токен дозапрошен с `workflow`-скоупом (device-code); CI полностью зелёный на Linux-раннере (45635720765 success 3м27с: Contracts 10/0, 897 passed, 2 skipped честных [corpus-guards], guard 85, ritual 29/0). Все ветки запушены; milestone/v4 синхронна с origin. Хуки в индексе 100755 — close-session работает на Linux/macOS
- **S31 deps**: S30 ✅ + подтверждение куратора о статусе артефактов hw-02/03 (не заморожены ли сдачей; правило 5 S31); окно до 04.12
- **FIX-017 (C6)** ждёт решения куратора; остатки C3–C5 — S38, не закрывать досрочно
- Контрактные комментарии: новый подпакет `application/` добавить в forbidden-контракт infrastructure вручную (комментарий в pyproject.toml, F3)
- Строка S30 в реестре станет битой после `make archive` (ловушка 4) — чинит S38

## Next Session Startup

1. Read `AGENTS.md`, `progress.md` (Next Steps), реестр `solution/sessions.md`
2. Следующая по графу — **S31** (единый рубеж исполнения, вариант A по A1/D-033 п.5): файл существует, `make new-session` не нужен; старт-ритуал: статус ◐, ARCH-GATE до кода, `./init.sh`
3. Сеть и CI в порядке (живой прогон зелёный 45635720765); BL-035/038 (gitleaks, SHA-пины/permissions) — по желанию, после первого зелёного прогона они уже не блокеры

## Recommended Next Step

- Закрыть S30: вердикт ревью в REVIEW-GATE → `make close-session S=S30 MSG=<файл>` → merge `harness/mechanical-guards` → `milestone/v4` (по D-031 — без слияния самой milestone-ветки); сетевые хвосты — S31

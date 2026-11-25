# AGENTS.md — Operating Manual for AI Coding Agents

This is a Text-to-SQL analytics agent (HW-01, OTUS «AI Agents»): an agent built
on a harness that makes agent sessions start fast, stay in scope, verify their
work, and resume cleanly.

**Проект:** агент-аналитик данных. Принимает бизнес-вопрос на естественном языке,
строит многотабличный SQL по графу связей, выполняет его под read-only ролью и
объясняет результат на языке бизнеса. **Конвенция ДЗ:** папки `hw-NN/` — решения
домашних заданий NN (артефакты сдачи, ужатые до требуемого объёма); `.docs/hw-NN/` —
полные исторические спецификации (вне гита, **READ ONLY** — D-011). Полная историческая
спецификация ДЗ-1 — `.docs/hw-01/README.md` (≈89 КБ, только чтение); живые прикладные
требования — [docs/prd.md](docs/prd.md); `hw-01/README.md` — решение ДЗ-1 (≈30 КБ).
Архитектурный пакет v1.8 (11 ADR, C4 L1–L3, FSM, ER, prd, security) — [docs/arch-package.md](docs/arch-package.md);
краткие свёртки — [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/PRODUCT.md](docs/PRODUCT.md).

**Стек:** Python 3.13 · LangGraph · PostgreSQL (pgvector) · Neo4j (граф связей) · Valkey ·
OpenAI-совместимый шлюз (облако routerai.ru или локальный LM Studio — D-020). Зависимости — uv. **Весь код — в `src/`:**
пакет `src/text_to_sql_agent/` (domain/application/infrastructure/orchestration/config/composition),
презентационный слой `src/apps/`, миграции Alembic `src/migrations/` (инициализация БД —
только миграциями), тесты `src/tests/`.

## Startup Workflow

Before writing code (session start / clock-in):

1. Confirm working directory with `pwd`
2. Read this file completely
3. Read project docs if present: [архитектурный пакет](docs/arch-package.md) (порядок чтения и матрица «вопрос → документ»), [Architecture](docs/ARCHITECTURE.md), [Product](docs/PRODUCT.md), канон — [solution/sessions/S00_session.md](solution/sessions/S00_session.md)
4. Create/verify the session file first — механика: `make new-session S=<ID> TITLE="..." F=<feat-XXX> [ROLE=продолжает]` (копия `TEMPLATE_session.md` под ID + строка в реестре `solution/sessions.md`, per `solution/README.md` lifecycle; нарратив — предусловия/задачи/ловушки — заполняет агент), then run `./init.sh` to verify the environment is healthy — it includes the session-start gate (`scripts/guard-subagents.sh --phase start`, D-023/D-027): the gate bites only the session you START (header status flipped to `◐ в работе`; planned `[ ] не начата` sessions are skipped), auto-adds missing checkboxes (`[FIX]`), and `make check` stays RED until that session's ARCH-GATE line is `✅ + evidence` or a genuine `N/A — reason` (`N/A — сессия не начата` does not pass for a started session)
5. Read `feature_list.json` to see current feature state
6. Review recent commits with `git log --oneline -5`
7. Read `progress.md` and `session-handoff.md` if present
8. Architectural gate (ARCH-GATE) — ритуал старта сессии, до любого кода:
   - **Отметь старт в файле сессии**: статус шапки → «◐ в работе» и чекбокс старта в «Предусловиях» — именно это включает гейт (запланированные «[ ] не начата» он скипает);
   - **canon-check**: сессия трогает архитектуру (границы модулей, новая зависимость, схема/ER, нерешённый дизайн-вопрос) → диспетчеризуй `architect` в режиме `canon-check` по плану сессии; компактная диспетчеризация (путь плана + вопрос + канон-указатели, без простыней — он читает по требованию);
   - **запиши вердикт** в строку ARCH-GATE файла сессии: `✅ + verdict/evidence` либо внятный `N/A — причина` (например: границы модулей/зависимости/схема/ER не затрагиваются); «N/A — сессия не начата» у стартованной сессии не проходит;
   - **findings fixed or explicitly ruled before implementation starts** — находки canon-check закрыты или явно решены (rulings) до первого код-коммита;
   guard-enforced: `scripts/guard-subagents.sh --phase start` (внутри `./init.sh` / `make check`) держит `make check` красным, пока ARCH-GATE стартованной (◐) сессии не заполнен (D-023/D-027).

If baseline verification is failing, repair that first — do not add new scope on a broken baseline.

## Working Rules

- **One feature at a time**: pick exactly one unfinished feature from `feature_list.json` (WIP=1 — only one feature may have `state: active` at a time)
- **Verification required**: run verification commands before claiming any work done
- **Update artifacts**: before ending session, update `progress.md` and `feature_list.json`
- **Stay in scope**: do not modify files unrelated to the current feature
- **Принципы кода**: SOLID/DRY/KISS — разумно, не в ущерб простоте и здравому смыслу (ясность важнее модности паттернов; преждевременная абстракция — не лучше дубля). Магические цифры и литералы в логике — именованные константы или Settings с комментарием «почему такое значение»; пороги, таймауты, лимиты, повторяющиеся литералы (ключи, URL, имена событий) — обязательно. why: код правится после того, как контекст сессии забыт, — имя и комментарий несут смысл, голое число нет; литерал в двух местах разъедется при правке.
- **Leave clean state**: the repo must remain restartable — next session runs `./init.sh` immediately

## Branch Hygiene

- `main` — только версии продукта (release-срезы, теги `vX.Y.Z`); `dev` — интеграция разработки. Прямые пуши запрещены — только через PR (локально `.githooks/pre-commit`/`pre-push`, подключаются идемпотентно в `init.sh`; серверная защита НЕ включается — branch protection платная на private-репо, решение владельца D-034: контракт держат локальные хуки, CI — контур наблюдаемости поверх).
- Вся работа — в рабочих ветках: `feat-0XX-<slug>` (фичи), `harness/<тема>` (обвязка). Пуш рабочей ветки в origin свободен; попадание в `dev`/`main` — только PR (`gh pr create --base dev`, release — `--base main`).
- Вехи: `milestone/v2→v3→v4` почкуются цепочкой (v2←dev, v3←v2, v4←v3) и живут параллельно до приёмки вехи. Рабочие ветки вехи — от её milestone-ветки, мержатся в неё напрямую; merge из одних журнально-доковых путей (`solution/**`, `progress.md`, `DECISIONS.md`) в origin отдельно не пушится — пуш milestone-ветки придерживается до содержательной волны (код/тесты/конфиги/CI/скрипты): прогон CI ~3.5 мин ради журнальной строки не жжётся (D-035/D-036); сама milestone-ветка не сливается в `dev`/`main` без явной команды владельца — реинтеграция через PR (D-024/D-029/D-031). Хотфиксы — от ветки своей вехи; хотфиксы V1 — от `dev` → PR → `dev`.
- Release: PR `dev` → `main` + тег версии. Хотфикс release: рабочая ветка от `main` → PR → `main`, затем перенос в `dev`.

Полный граф веток, правила хотфиксов по вехам и детали: [docs/AGENT-OPERATIONS.md](docs/AGENT-OPERATIONS.md).

## Constraints

- MUST NOT commit secrets, tokens, or credentials: ключи LLM-шлюза — только в `.env` (в git не попадает), в коде — только чтение через `Settings`. why: security.
- MUST NOT force-push to protected branches. why: git history is the system of record.
- MUST NOT пушить напрямую в `main` или `dev`: изменения попадают туда только через PR из рабочей ветки (локально — `.githooks/pre-push`; серверной защиты нет — D-034, «не мержить при красном CI» исполняется процедурно на слиянии). why: `main` — только версии продукта, `dev` — интеграция разработки; прямой пуш обходит ревью-контур и гейты.
- MUST NOT set `state: passing` directly in `feature_list.json` — run `make verify-feature F=<id>`. why: passing requires verification evidence.
- MUST NOT выполнять пользовательский SQL иначе как под read-only ролью `agent_ro` с `LIMIT 1000` и timeout 30s; никакого DDL/DML в исполняемом SQL. why: граница прав и безопасность (hw-01 §3.2).
- MUST NOT пропускать валидацию SQL: каждый запрос от LLM проходит AST-валидатор (sqlglot, deny-by-default) до выполнения; `EXPLAIN ANALYZE` запрещён — только `EXPLAIN`. why: текст от LLM не доверенный; EXPLAIN ANALYZE выполняет запрос.
- MUST NOT расширять права агента за пределы `agent_permissions`: недоступная таблица не существует в мире агента (подграф, retrieval, DDL). why: RBAC, deny-by-default (hw-01 §3.2).
- MUST NOT позволять LLM управлять переходами между узлами: маршрут задаёт state machine на коде, модель наполняет содержимое узлов. why: инвариант архитектуры (hw-01 §2.1).
- MUST NOT писать код стартованной сессии (статус шапки «◐ в работе») с незаполненным ARCH-GATE: canon-check архитектора (✅ verdict) или обоснованный N/A — до первого код-коммита; находки — закрыты или решены (rulings). Гвард red-lines `make check` до заполнения (D-027). why: архитектурные ошибки на порядок дешевле на этапе плана; «задним числом» canon-check вырождается в формальность.
- MUST NOT редактировать содержимое `.docs/` — исторические документы READ ONLY (D-011): изменения требований — в трекаемом пакете `docs/` (prd.md + затронутые ADR). why: две живые копии разъезжаются; история не переписывается.
- Update docs in the same commit as the code change — no stale documentation. why: docs rot fast.
- Commits are atomic: one logical operation per commit; the repo is in a consistent state after every commit. why: clean rollback.

## Required Artifacts

- `feature_list.json` — feature state tracker (source of truth)
- `progress.md` — session continuity log
- `DECISIONS.md` — decision log (decision + context + alternatives)
- `init.sh` — standard startup and verification path
- `session-handoff.md` — cross-session handoff (fill for large sessions)
- `docs/ARCHITECTURE.md`, `docs/PRODUCT.md`, `docs/quality-document.md` — topic docs
- `docs/AGENT-OPERATIONS.md` — полный операционный объём ритуалов (граф веток вех, чеклист End of Session)
- `docs/prd.md`, `docs/security.md`, `docs/observability.md`, `docs/model-card.md`, `docs/RISKS.md` — обвязка архитектурного пакета
- `docs/adr/` — architecture decision records (реестр: ADR-001…011 + [шаблон](docs/adr/ADR-TEMPLATE.md))
- `docs/diagrams/` — C4 L1/L2/L3, state FSM, sequence, ER-модель (каноническая)
- `solution/` — план-сессий многосессионных проектов: индекс (`solution/sessions.md`), канон ([solution/sessions/S00_session.md](solution/sessions/S00_session.md)), журнал противоречий (`solution/FIXES.md`)

## Feature State Rules

`feature_list.json` is the single source of truth. State machine:

`not_started` → `active` → `passing`

- Skipping states is not allowed.
- Never set `state: passing` directly — run `make verify-feature F=<id>` (or `bash scripts/verify-feature.sh <id>`); the script runs the feature's verification command and records evidence.
- A feature should be completable in one session; a large feature may span several sessions: exactly one final session closes it via `make verify-feature F=<id>`, intermediate sessions leave it `active` with a documented continuation (clean-check allowlist) and record progress in `progress.md`
- Record evidence (command and output) in `feature_list.json` and `progress.md`.

## Definition of Done

A feature is done only when runtime evidence passes — not when code is written. Three layers:

1. **Layer 1 — syntax/static**: build, compile, lint, type-check
2. **Layer 2 — runtime behavior**: tests and the feature's `verification` command from `feature_list.json`
3. **Layer 3 — system confirmation**: end-to-end run / manual check

Do not proceed to the next layer if the previous one fails. Done checklist:

- [ ] Target behavior implemented
- [ ] Required verification actually ran (tests / lint / type-check)
- [ ] Code review by the `code-reviewer` subagent passed — Critical/Important findings fixed or parked with a ruling
- [ ] Evidence recorded in `feature_list.json` and `progress.md`
- [ ] Repository remains restartable from the standard startup path (`./init.sh`)
- [ ] App reaches ready state, side effects are correct, no debug artifacts remain

Layer 3 (e2e) is required when changes cross component or domain boundaries.

## Verification Commands

```bash
# Full verification (recommended) — exits 0 when the repo is consistent
make check          # runs ./init.sh (Python: uv sync + ruff + lint-imports + pytest)
./init.sh           # direct path
```

Required checks:

- Tests: `./init.sh` → `uv sync --frozen` + `ruff check` + `ruff format --check` + `lint-imports` + `pytest`
- Контракты импортов слоёв (S30): `uv run lint-imports` (или `make lint-imports`) — входит в `./init.sh`, нарушение границы слоёв роняет `make check` так же, как lint
- Инфраструктура (PostgreSQL + Valkey) для тестов с БД: `make infra-up`; тесты без поднятой инфры скипаются с понятной причиной — `./init.sh` остаётся зелёным без Docker
- Одна фича: `make verify-feature F=<feat-XXX>`
- CI (S30): `.github/workflows/ci.yml` — тот же `bash init.sh` поверх сервисов `docker-compose.yml` (`up -d --wait`) и миграций; прогон — только на `milestone/**` (пуш после слияния + PR с целью milestone); рабочие ветки (`feat-**`, `harness/**`) и `dev`/`main` CI не запускают — минуты Actions бесплатного тарифа ограничены (D-035), локальный рубеж перед пушем — полный `make check`

The repo is in a consistent state when `make check` exits 0.

## Architecture Boundaries

Код-архитектура — по [code-architecture-draft §2](`.drafts/code-architecture-draft.md`) с поправкой D-009 (весь код в `src/`); см. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md):

- `src/text_to_sql_agent/domain/` — сущности (AgentState, JoinPlan), value objects, ошибки: без внешних зависимостей, без IO, без LLM;
- `src/text_to_sql_agent/application/ports/` — Protocol-интерфейсы стыков (LLMProvider, VectorStore, SchemaGraphStore, SQLExecutor, SemanticCache, Tracer, BudgetGuard);
- `src/text_to_sql_agent/application/services/` — по одному классу на узел графа;
- `src/text_to_sql_agent/infrastructure/` — адаптеры портов (openai_provider, pgvector_store, neo4j_graph_store, postgres_executor, valkey_cache, guardrails, jsonl_tracer);
- `src/text_to_sql_agent/orchestration/` — LangGraph: граф и условные переходы — только рёбра;
- `src/text_to_sql_agent/config/` — pydantic-settings, единственная точка чтения env;
- `src/text_to_sql_agent/composition/` — единственное место сборки зависимостей;
- `src/apps/` — презентационный слой: cli.py, index_knowledge.py, streamlit_app.py;
- `src/migrations/` — Alembic: единственный способ создать/изменить схему БД (`make infra-migrate`); правка применённой миграции запрещена;
- `src/tests/` — unit / integration / e2e (БД-тесты скипаются без инфры).

Allowed dependencies: `apps → composition → orchestration → application/services → domain`; `infrastructure` реализует порты (зависимость внутрь). Forbidden: узлы вызывают друг друга напрямую (только через state machine), бизнес-код читает `os.environ` мимо `Settings`, LLM-вызовы из `domain`, сборка зависимостей вне `composition`, CREATE TABLE вне миграций Alembic.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) и ADR-реестр ([docs/adr/README.md](docs/adr/README.md)). Constraint violations are reported with what/why/fix. Every new error category caught in code review is promoted into a rule in `.harness/arch-rules.json` (optional; create when the project grows).

## Tools & Permissions

- File edits: allowed inside project scope only (`src/`, `tests/`, `docs/`, `solution/`, инфра-файлы корня)
- Shell: read/debug commands freely; destructive commands require explicit user approval
- Docker: `make infra-up` / `make infra-down` разрешены без подтверждения (локальные контейнеры проекта); команды с удалением томов — по согласованию
- Сеть: LLM-вызовы — только к OpenAI-совместимому шлюзу: `LLM_BASE_URL` из `.env` или проектный дефолт (локальный LM Studio, D-020); произвольные адреса запрещены; из тестов — только моки, реальный шлюз в тестах не вызывается
- MCP/integrations: document here when added

## Observability

- Before each feature, agree on scope via a sprint contract: [templates/sprint-contract.md](templates/sprint-contract.md)
- Record verification runtime signals (command and output) in `progress.md` → Evidence of Completion
- Score completed work against [templates/evaluator-rubric.md](templates/evaluator-rubric.md) — every dimension must reach B or above

## End of Session

Before ending a session (session end / clock-out) — нарратив делает агент, механику делает скрипт:

1. Нарратив: обнови `progress.md` (Current State, Next Steps), `feature_list.json` (state + evidence: фича-«закрывает» — `make verify-feature F=<id>`; промежуточная — фича остаётся `active`), `docs/quality-document.md`, отчёт в файле сессии («Отчёт по сессии» + строка REVIEW-GATE: вердикт ревью + диапазон `BASE7..HEAD7`). Диспетчеризация `code-reviewer` — после зелёного `make check` по диапазону (BASE = merge-base с `main` или последний отревьюенный коммит); каждый Critical закрывается регрессионным тестом (падает на до-фиксовом коде и остаётся в наборе), Minor → `solution/backlog/`; отчёт ревью — по формату из определения агента (`~/.zcode/agents/code-reviewer.md`, вне репо).
2. Риски/блокеры → `progress.md` и `DECISIONS.md`; вне скоупа → `solution/backlog/` (BL-NN files)
3. Механика: запусти `make close-session S=<ID> MSG=<файл-сообщение-коммита>` — скрипт проверит гейты, отчёт, фичи и чистоту дерева вне allowlist (весь КОД закоммичен до запуска; грязь = отказ), перевернёт статус шапки `◐ → ✅ дата`, обновит строку реестра и «Текущее состояние» (только последняя закрытая сессия, одна строка), прогонит clean-state protocol + `make archive` и закоммитит из MSG-файла
4. Leave the repo clean and restartable: next session runs `./init.sh` immediately

If running low on context, do not rush to finish — stop, update `progress.md`, and commit a clean checkpoint. Полный чеклист закрытия и детали: [docs/AGENT-OPERATIONS.md](docs/AGENT-OPERATIONS.md).

## Escalation

- **Architecture decisions**: consult [arch-package.md](docs/arch-package.md) and the ADR index ([docs/adr/README.md](docs/adr/README.md), ADR-001…011 приняты); for complex trade-off decisions dispatch the `architect` subagent (`consult` mode: options + tradeoffs + recommendation, token-compact dispatch); significant decisions get a new ADR, otherwise ask the user
- **Data changes**: sync `docs/diagrams/er-data-model.md` in the same commit as the migration/seed
- **Unclear requirements**: check [docs/prd.md](docs/prd.md) (живые FR/NFR + трассировка) and [docs/PRODUCT.md](docs/PRODUCT.md); исторические детали — `.docs/hw-01/README.md` (READ ONLY, D-011; `hw-01/README.md` — только решение ДЗ); изменения требований фиксировать в prd.md + ADR, иначе ask the user
- **Риски перед фичей**: свериться с [docs/RISKS.md](docs/RISKS.md) — какие риски затрагивает фича и что уже заложено в митигации
- **Repeated test failures**: update `progress.md`, flag for human review
- **Scope ambiguity**: re-read the feature's definition of done in `feature_list.json`

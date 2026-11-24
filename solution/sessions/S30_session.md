# S30 — harness: механические защиты — import-linter, CI, защита веток

Этап: Веха V4 (ДЗ-05) · harness, окно 25–30.11.2026 | Владелец: агент | Зависимости: S29 | Оценка: 1 | Статус: [ ] не начата
Фичи: — (сессия без фичи, states не меняются)
Роль по фиче: — (сессия без фичи, states не меняются)

## ⚠️ Прочитай перед началом

Исполнитель: ИИ-агент; куратор: пользователь. Предмет — аудит 2026-11-23, D3 и E9a:

- Серверного контура нет: каталога `.github/` нет, GitHub на `main`/`dev` отвечал «Branch not
  protected» (404), rulesets пустые. «Прямые пуши запрещены» держится только на локальных хуках
  `.githooks/`; «CI-гейт: не мержить при ACL-leak > 0» (prd §6) исполнять нечему.
- Правила импортов слоёв заявлены в c4-l2 («проверяются `import-linter`»), самого инструмента
  нет ни в зависимостях, ни в `Makefile`, ни в тестах.
- D1 (30 незапушенных коммитов) снят 2026-11-24: `milestone/v4` запушен, рабочие ветки слиты в неё.

Сессия ставит механический контур раньше код-сессий долга (S31–S35), чтобы их правки шли под
контролем слоёв и CI. Решение куратора 2026-11-24: харнесс-долг делается в текущей вехе (D-033).

Границы скоупа: БЕЗ eval-сета и гейта ACL-leak (BL-025, выпускная), БЕЗ CI-гейта рёбер реестра
(BL-026), БЕЗ защиты `milestone/*` (Branch Hygiene говорит только о `main`/`dev`), `uv.lock`
меняется только добавлением самой dev-зависимости.

### Правила для исполнителя

1. Ветка `harness/mechanical-guards` от `milestone/v4`; merge — в `milestone/v4` (D-031).
2. Контракты слоёв — дословно из AGENTS.md «Architecture Boundaries» и S00 «Архитектура кода».
   Фактическая матрица импортов на `1b6af88` (AST-скан, 2026-11-24), на неё и опираться:
   - чисто: `domain` никуда внутрь не импортирует; `application` → только `ports` и `domain`;
     `orchestration` → `application`, `ports`, `domain` (без `infrastructure`); `infrastructure`
     → `ports`, `domain`, `config`; `composition` → все; `orchestration` ↔ `infrastructure` независимы;
   - **нарушение, которого в аудите нет:** `src/apps/index_knowledge.py:23,30` импортирует
     `infrastructure.memory.knowledge_sources` и `noise_corpus` напрямую — цепочка
     `apps → composition → …` это запрещает. Это не исключение контракта, а находка: перенести
     сборку офлайн-индексатора в `composition` (Task 2), контракт под код не подгонять.
3. Образы в CI — те же пины, что в `docker-compose.yml` (правило 10 S00): `pgvector/pgvector:0.8.0-pg17`,
   `neo4j:5.26.0-community`, `valkey/valkey:8.1.11-alpine3.24`. Секретов в workflow нет: пароли
   ролей и dev-токены — тестовые значения в `env:` CI.
4. Защита веток на GitHub — действие на сервере и требует прав администратора: включает сам
   куратор либо агент по его явному «да» в этой сессии. До сдачи ДЗ-02 не включать (условие
   куратора из аудита).
5. Пуш рабочей ветки в origin свободен (Branch Hygiene); пуш `milestone/v4` — по команде куратора.

## Предусловия

- [ ] S29 закрыта: `grep -n "| S29 |" solution/sessions.md` → статус ✅
- [ ] `make check` зелёный (baseline репозитория)
- [ ] `gh auth status` → авторизован; `gh api repos/{owner}/{repo}` → доступ с правами admin (для Task 5)
- [ ] Сессия СТАРТУЕТ: статус в шапке переведён в «◐ в работе» и ARCH-GATE резолвлен ДО кода (canon-check architect ✅ либо обоснованный N/A) — старт-гейт (`guard-subagents.sh --phase start`, D-027) проверяет только стартованные (◐) сессии; «[ ] не начата» — план, гейт её скипает

## Задачи

### Task 1: import-linter — контракты слоёв в Layer 1

Dev-зависимость `import-linter` (группа `dev` в `pyproject.toml`), `uv lock` (только добавление).
Конфиг `[tool.importlinter]` в `pyproject.toml`:

- `layers`: `apps | composition | orchestration | application | domain`;
- `layers`: `apps | composition | infrastructure | application | domain`;
- `independence`: `orchestration` ↔ `infrastructure`;
- `forbidden`: `application` → `infrastructure`, `orchestration`, `composition`, `apps`, `config`;
  `infrastructure` → `orchestration`, `composition`, `apps`, `application` кроме `application.ports`;
  `domain` → любой внутренний пакет; `config` → любой внутренний пакет; `langgraph` только в
  `orchestration` и `infrastructure`.

`init.sh`: шаг `uv run lint-imports` сразу после ruff; `Makefile`: цель `lint-imports`.

**✅ Самопроверка:**

```bash
uv run lint-imports                     # Contracts: N kept, 0 broken
uv run pytest src/tests/unit/test_smoke.py -q   # без регрессий
```

Негативный контроль (в отчёт): во временном файле `application/services/_tmp_probe.py` добавить
`import text_to_sql_agent.orchestration` → `lint-imports` падает (broken 1) → файл удалить.

### Task 2: Убрать нарушение `apps → infrastructure` (index_knowledge)

Сборку источников знаний и шумового корпуса для офлайн-индексатора вынести в `composition`
(например, фабрика writer-контейнера), `apps/index_knowledge.py` импортирует только `composition`,
порты и `domain`. Поведение `make index-knowledge` не меняется.

**✅ Самопроверка:**

```bash
make infra-up && make infra-migrate && make index-knowledge   # идемпотентная переиндексация проходит
git grep -n "text_to_sql_agent.infrastructure" -- src/apps     # пусто
```

### Task 3: CI — GitHub Actions с сервисами

`.github/workflows/ci.yml`: триггеры — push (`milestone/**`, `feat-**`, `harness/**`) и
`pull_request` (целевые `main`, `dev`, `milestone/**`); `ubuntu-latest`; `services` — postgres,
neo4j, valkey с пинами правила 3; шаги: checkout → uv (пин версии) → `make infra-migrate` →
`bash init.sh` (единый критерий консистентности: sync, ruff, `lint-imports`, pytest, self-test'ы).

**✅ Самопроверка:**

```bash
git push origin HEAD
gh run list --branch harness/mechanical-guards --limit 1        # conclusion: success
gh run view <id> --log | grep -E "passed|skipped|broken"        # N passed; skipped мало (инфра в CI поднята)
```

### Task 4: Документация контура

`AGENTS.md` (Verification Commands — CI), `docs/AGENT-OPERATIONS.md` (полный контур: локальные
гварды + CI + серверная защита), `docs/ARCHITECTURE.md` и c4-l2 (утверждение про `import-linter`
становится истинным), `scripts/README.md`, `docs/quality-document.md` (harness/scripts).

**✅ Самопроверка:** `git grep -n "import-linter" -- docs AGENTS.md` — утверждение подкреплено конфигом.

### Task 5: Защита веток `main` и `dev` — после сдачи ДЗ-02, по подтверждению куратора

Через `gh api`: обязательный PR, обязательные status checks (job CI), запрет force-push и
удаления ветки — одинаково для `main` и `dev`. Сначала показать куратору готовые команды.

**✅ Самопроверка:**

```bash
gh api repos/{owner}/{repo}/branches/main/protection   # 200 JSON (было 404)
gh api repos/{owner}/{repo}/branches/dev/protection    # 200 JSON
```

Подтверждения нет — задача переносится в отчёт («ждёт сдачи ДЗ-02»), молча не выполняется.

## Критерии приёмки

- [ ] `uv run lint-imports` входит в `./init.sh`: нарушение импорта слоёв роняет `make check` (негативный контроль зафиксирован в отчёте)
- [ ] `apps` не импортирует `infrastructure`; `make index-knowledge` работает без изменения поведения
- [ ] `.github/workflows/ci.yml` даёт зелёный прогон на push рабочей ветки; integration-тесты в CI исполняются, а не скипаются (число skipped в логе — в отчёте)
- [ ] `main` и `dev` защищены на сервере (API 200) — после сдачи ДЗ-02 и подтверждения куратора; иначе осознанно перенесено в отчёт
- [ ] `make check` EXIT=0; документы изменены в тех же коммитах, что и код

## Ловушки

1. **Контракт «по документу» красный на реальном коде** — сначала понять, кто нарушает: если код
   (как `index_knowledge`), чинится код; исключение в контракте — только с комментарием «почему»
   и решением на ARCH-GATE.
2. **Integration-тесты в CI без `make infra-migrate`** не скипируются, а падают: PostgreSQL
   доступен → фикстура не скипает, схемы нет. Миграция — до `init.sh`.
3. **Neo4j поднимается 20–30 с** — без ожидания healthcheck `test_join_graph` скипируется, и CI
   «зелёный» на неполном наборе. Число skipped проверять по логу, а не по conclusion.
4. **`gh api …/protection` отвечает 403** — у токена нет admin:scope; это вопрос куратору, а не
   «защита не нужна».
5. **`./init.sh` остаётся единым критерием** (feat-001): CI — второй контур поверх него, а не
   отдельный список шагов, который разъедется с локальным.
6. **CI и pypi.org**: `uv sync --frozen` в CI тянет `hatchling`; локально 2026-11-24 `pypi.org`
   был недоступен и синк шёл с `UV_OFFLINE=1` из кэша — в отчёте фиксировать, каким путём прошёл синк.

## Гейты субагентов (заполняется по факту, до закрытия сессии)

- ARCH-GATE: [✅ <verdict: compliant | violations | needs-decision> + <план/файл> | N/A — <причина>]
- REVIEW-GATE: [✅ <Ready to merge? вердикт> + <BASE7>..<HEAD7> | N/A — <причина>]

## Отчёт по сессии (заполняется по факту)

- Сделано:
  - [Task: что реально сделано, коммиты, число тестов/ассертов, live-прогон]
- Отклонения от плана:
  - [что сделали не так, как в задаче, и почему; каждое отклонение — осознанное]
- Перенесено в другие сессии:
  - [ID сессии — что перенесено и почему; если в беклог — BL-NN-файл в solution/backlog/ (см. backlog/README.md)]

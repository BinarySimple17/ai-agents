# S30 — harness: механические защиты — import-linter, CI (защита веток снята — D-034)

Этап: Веха V4 (ДЗ-05) · harness, окно 25–30.11.2026 | Владелец: агент | Зависимости: S29 | Оценка: 1 | Статус: ◐ в работе (2026-11-25)
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
(BL-026), БЕЗ защиты `milestone/*` (Branch Hygiene говорит только о `main`/`dev`). **Решение
владельца 2026-11-25 (диалог старта S30, D-034): серверная защита веток НЕ включается** —
GitHub branch protection платная на private-репо, куратор включать её не будет; контракт
«Прямые пуши запрещены» остаётся на локальных хуках `.githooks/` (Task 5 → снят, см. ниже).
`uv.lock` меняется только добавлением самой dev-зависимости.

### Правила для исполнителя

1. Ветка `harness/mechanical-guards` от `milestone/v4`; merge — в `milestone/v4` (D-031).
2. Контракты слоёв — из AGENTS.md «Architecture Boundaries» и S00 «Архитектура кода», ПЛЮС три
   дополнения canon-check 2026-11-25 (ARCH-GATE, F1–F3; в конфиге помечены «производное»):
   базовые слои AST-скана на `1b6af88` подтверждены на HEAD `3f68c9c`:
   - чисто: `domain` никуда внутрь не импортирует; `application` → только `ports` и `domain`;
     `orchestration` → `application`, `ports`, `domain` (без `infrastructure`); `infrastructure`
     → `ports`, `domain`, `config`; `composition` → все; `orchestration` ↔ `infrastructure` независимы;
   - **нарушение, которого в аудите нет:** `src/apps/index_knowledge.py:23,30` импортирует
     `infrastructure.memory.knowledge_sources` и `noise_corpus` напрямую — цепочка
     `apps → composition → …` это запрещает. Это не исключение контракта, а находка: перенести
     сборку офлайн-индексатора в `composition` (Task 2), контракт под код не подгонять;
   - **F1:** двух `layers` недостаточно — верхний слой в `layers` может импортировать ЛЮБОЙ
     нижний, поэтому `apps → infrastructure` останется зелёным. Нужен явный `forbidden`
     `apps → text_to_sql_agent.infrastructure` + второй негативный контроль в Task 1;
   - **F2:** запрет `application → config` — не дословный из AGENTS (канон требует лишь «env
     только через Settings»); при этом `orchestration → config` не покрыт никем. Оба запрета
     `→ config` вносятся одним контрактом с пометкой «производное» (мнение архитектора: сервисы
     и узлы получают настройки через composition);
   - **F3:** у `forbidden` нет исключений — «кроме `application.ports`» выражается перечислением
     `application.services` + `application.use_cases` (новый подпакет автоматически не попадёт —
     комментарий в конфиге); обязателен `root_packages = ["text_to_sql_agent", "apps"]`.
3. CI поднимает инфру СОСТАВОМ (`docker compose up -d --wait` из того же `docker-compose.yml`) —
   отклонение от буквы плана (`services:`) по находке F6/F2-архитектора: пины и healthcheck
   в одном месте (правило 10 S00 — «те же пины, что в compose» гарантируется структурно),
   ловушка 3 (Neo4j 20–30 с) закрывается `--wait`. Нужны healthcheck-блоки в compose (pg_isready
   уже в Makefile; добавить в compose для `--wait`). Секретов в workflow нет: `.env` создаётся
   шагом `cp .env.example .env` (S00: шаблон коммитится) — тестовые значения, Makefile
   `infra-migrate` с `--env-file .env` без файла падает (проверено архитектором на uv 0.11.24);
   `python` 3.13 в PATH — шаг setup-python (init.sh проверяет `command -v python`).
4. Защита веток на GitHub — СНЯТА решением владельца 2026-11-25 (D-034): branch protection —
   платная функция private-репо, куратор включать её не будет — ни в этой сессии, ни позже.
   «Прямые пуши запрещены» держится на локальных хуках `.githooks/`; CI-гейт prd (ACL-leak)
   остаётся требованием, исполнение — процедурное на слиянии (CI зелёный до merge).
5. Пуш рабочей ветки в origin свободен (Branch Hygiene); пуш `milestone/v4` — по команде куратора.

## Предусловия

- [x] S29 закрыта: `grep -n "| S29 |" solution/sessions.md` → статус ✅ 2026-11-25 (проверено на старте)
- [x] `make check` зелёный (baseline репозитория) — прогон в рамках старта, см. Evidence
- [N/A] `gh auth status` → admin-доступ — **не требуется: Task 5 закрыт решением владельца 2026-11-25** (диалог старта S30): защита веток на GitHub — платная функция (private repo) и включаться не будет; серверный контур не разворачивается, «Прямые пуши запрещены» остаётся на локальных хуках `.githooks/`. Зафиксировано в D-034
- [x] Сессия СТАРТУЕТ: статус в шапке переведён в «◐ в работе» (2026-11-25) и ARCH-GATE резолвлен ДО кода — canon-check архитектора ✅ (violations → rulings выше, все находки закрыты правками плана или беклогом) — старт-гейт (`guard-subagents.sh --phase start`, D-027) проверяет только стартованные (◐) сессии; «[ ] не начата» — план, гейт её скипает

## Задачи

### Task 1: import-linter — контракты слоёв в Layer 1

Dev-зависимость `import-linter` (группа `dev` в `pyproject.toml`), `uv lock` (только добавление).
Конфиг `[tool.importlinter]` в `pyproject.toml` (по canon-check F1–F3):

- `root_packages = ["text_to_sql_agent", "apps"]` (F3 — обязателен: контракты across двух пакетов);
- `layers`: `apps | composition | orchestration | application | domain`;
- `layers`: `apps | composition | infrastructure | application | domain`;
- `independence`: `orchestration` ↔ `infrastructure`;
- `forbidden`: `apps` → `text_to_sql_agent.infrastructure` (**F1** — `layers` не ловит: верхний
  слой может импортировать любой нижний);
- `forbidden`: `application` → `infrastructure`, `orchestration`, `composition`, `apps`,
  `text_to_sql_agent.config`; **и `orchestration` → `text_to_sql_agent.config` тем же контрактом**
  (**F2** — иначе не покрыт никем; пометка «производное» в комментарии);
- `forbidden`: `infrastructure` → `orchestration`, `composition`, `apps`, `application.services`,
  `application.use_cases` (**F3** — «кроме `application.ports`» = перечисление подпакетов;
  комментарий в конфиге: новый подпакет `application` в контракт добавить вручную);
- `forbidden`: `domain` → любой внутренний пакет; `config` → любой внутренний пакет;
- `forbidden`: `langgraph` (внешний, `include_external_packages = true`) — из `apps`, `domain`,
  `application`, `composition`, `config` (только `orchestration` и `infrastructure`).

`init.sh`: шаг `uv run lint-imports` сразу после ruff; `Makefile`: цель `lint-imports`.

**✅ Самопроверка:**

```bash
uv run lint-imports                     # Contracts: N kept, 0 broken
uv run pytest src/tests/unit/test_smoke.py -q   # без регрессий
```

Негативный контроль ×2 (в отчёт): (1) `application/services/_tmp_probe.py` с
`import text_to_sql_agent.orchestration` → broken 1; (2) `apps/_tmp_probe.py` с
`import text_to_sql_agent.infrastructure` → broken 1 (**F1** — проверяет именно новый контракт);
оба файла удалить после.

### Task 2: Убрать нарушение `apps → infrastructure` (index_knowledge)

Сборку источников знаний и шумового корпуса для офлайн-индексатора вынести в `composition`
(например, фабрика writer-контейнера), `apps/index_knowledge.py` импортирует только `composition`,
порты и `domain`. Поведение `make index-knowledge` не меняется. **В том же коммите (F5):**
регрессионный `test_build_documents_runs_the_guard` (`src/tests/unit/test_knowledge_sources.py:102`)
патчит имена в apps-модуле — перепривязать к новой фабрике composition; пути `_REPO_ROOT`
перебазировать `parents[2]` → `parents[3]` (глубина `composition/`).

**✅ Самопроверка:**

```bash
make infra-up && make infra-migrate && make index-knowledge   # идемпотентная переиндексация проходит
git grep -n "text_to_sql_agent.infrastructure" -- src/apps     # пусто
uv run pytest src/tests/unit/test_knowledge_sources.py -q      # guard-тест зелёный на новой фабрике
```

### Task 3: CI — GitHub Actions с инфрой из compose (правка по F6/F7 canon-check)

`.github/workflows/ci.yml`: триггеры — push (`milestone/**`, `feat-**`, `harness/**`) и
`pull_request` (целевые `main`, `dev`, `milestone/**`); `ubuntu-latest`; инфра —
`docker compose up -d --wait` (пины и healthcheck — из того же `docker-compose.yml`, отклонение
от `services:` обосновано в правиле 3); healthcheck-блоки в compose (postgres `pg_isready`,
neo4j `cypher-shell RETURN 1`, valkey `redis-cli ping`) — без них `--wait` не ждёт готовности и
тесты скипаются (ловушка 3). Шаги: checkout → setup-python 3.13 (init.sh требует `python` в PATH,
F7) → uv (пин версии) → `cp .env.example .env` (F6: `--env-file .env` без файла падает; compose
сам читает `.env` проекта) → `make infra-migrate` → `bash init.sh` (единый критерий:
sync, ruff, `lint-imports`, pytest, self-test'ы). Секретов в workflow нет.

**✅ Самопроверка (живой прогон — при восстановлении сети, в отчёт честно):**

```bash
git push origin HEAD
gh run list --branch harness/mechanical-guards --limit 1        # conclusion: success
gh run view <id> --log | grep -E "passed|skipped|broken"        # N passed; skipped мало (инфра в CI поднята)
```

### Task 4: Документация контура + D-034 (расширен по F4/F8/F9 canon-check)

`DECISIONS.md`: **D-034** (решение владельца: серверная защита веток не включается — платный
тариф private-репо; контур = локальные хуки + CI; prd CI-гейт ACL-leak остаётся требованием,
исполнение процедурное на слиянии). `AGENTS.md`: Branch Hygiene `:54` и MUST NOT `:65`
(«серверно — branch protection» больше не правда), Verification Commands — CI/lint-imports.
`docs/AGENT-OPERATIONS.md:15` — серверный уровень переписать по D-034. `docs/diagrams/c4-l2-container.md:94-100`
— выровнять с контрактами (**F4**: диаграмма рисует infrastructure звеном под orchestration —
читается как разрешённый `orchestration → infrastructure`, противоречит independence) +
запись **FIX-018** в `solution/FIXES.md` (противоречие канона). `docs/ARCHITECTURE.md`
(утверждение про `import-linter` становится истинным), `scripts/README.md`,
`docs/quality-document.md` (harness/scripts). Находки вне скоупа → беклог: **BL-035** gitleaks
(объявлен security.md:172, сканера нет), **BL-036** `plan_joins_demo` ходит в БД psycopg
напрямую (apps → драйвер, контракт не видит), **BL-037** формулировка domain «без внешних
зависимостей» (AGENTS.md:140) vs фактический pydantic в domain.

**✅ Самопроверка:** `git grep -n "import-linter" -- docs AGENTS.md` — утверждение подкреплено конфигом;
`git grep -n "branch protection" AGENTS.md docs/AGENT-OPERATIONS.md` — серверный уровень описан
по D-034 (платный тариф, не включается).

### Task 5 (СНЯТ решением владельца 2026-11-25): Защита веток `main` и `dev` на GitHub

**Решение владельца (диалог старта S30, 2026-11-25):** GitHub branch protection — платная
функция на private-репо; куратор включать её не будет. Серверный контур не разворачивается
ни в этой сессии, ни позже: контракт «Прямые пуши запрещены» держится на локальных хуках
`.githooks/` (pre-push блокирует `main`/`dev`/force-push — единственный исполняющий контур).
Задача не «переносится» — снята навсегда; зафиксировано в **D-034** и в документации контура
(Task 4). gh-команды защиты не выполняются, prереквизит admin-доступа — N/A.

## Критерии приёмки

- [ ] `uv run lint-imports` входит в `./init.sh`: нарушение импорта слоёв роняет `make check` (негативный контроль зафиксирован в отчёте)
- [ ] `apps` не импортирует `infrastructure`; `make index-knowledge` работает без изменения поведения
- [ ] `.github/workflows/ci.yml` даёт зелёный прогон на push рабочей ветки; integration-тесты в CI исполняются, а не скипаются (число skipped в логе — в отчёте)
- [x] ~~`main` и `dev` защищены на сервере (API 200)~~ — **снято решением владельца 2026-11-25 до старта работ** (D-034): branch protection платная, включаться не будет; осознанное решение зафиксировано в D-034 + документах контура (Task 4), не «перенос в отчёт»
- [ ] `make check` EXIT=0; документы изменены в тех же коммитах, что и код

## Ловушки

1. **Контракт «по документу» красный на реальном коде** — сначала понять, кто нарушает: если код
   (как `index_knowledge`), чинится код; исключение в контракте — только с комментарием «почему»
   и решением на ARCH-GATE.
2. **Integration-тесты в CI без `make infra-migrate`** не скипируются, а падают: PostgreSQL
   доступен → фикстура не скипает, схемы нет. Миграция — до `init.sh`.
3. **Neo4j поднимается 20–30 с** — без ожидания healthcheck `test_join_graph` скипируется, и CI
   «зелёный» на неполном наборе. Число skipped проверять по логу, а не по conclusion.
4. **Серверная защита веток** — снята решением владельца (D-034, платный тариф): gh-команды
   `…/protection` не выполняются вовсе, вопрос admin:scope закрыт вместе с задачей. Не
   «помочь владельцу включить», а принять решение как данность и починить документацию контура.
5. **`./init.sh` остаётся единым критерием** (feat-001): CI — второй контур поверх него, а не
   отдельный список шагов, который разъедется с локальным.
6. **CI и pypi.org**: `uv sync --frozen` в CI тянет `hatchling`; локально 2026-11-24 `pypi.org`
   был недоступен и синк шёл с `UV_OFFLINE=1` из кэша — в отчёте фиксировать, каким путём прошёл синк.

## Гейты субагентов (заполняется по факту, до закрытия сессии)

- ARCH-GATE: ✅ verdict: **violations → rulings до кода** (canon-check архитектора, 2026-11-25): нарушения не блокирующие, план дополнен ДО реализации — F1 двух `layers` мало (верхний слой может импортировать любой нижний: `apps → infrastructure` осталось бы зелёным) → явный `forbidden` + 2-й негативный контроль; F2 запрет `application → config` не дословный из AGENTS, `orchestration → config` не покрыт никем → один контракт на оба, пометка «производное»; F3 «кроме ports» = перечисление `application.services`/`use_cases` + комментарий + `root_packages`; F4 c4-l2:94–100 рисует infrastructure под orchestration (= разрешён `orchestration → infrastructure`) против independence → выравнивание в Task 4 + FIX-018; F5 регресс-тест патчит apps-модуль → перепривязка к фабрике composition в том же коммите (+`parents[3]`); F6 `make infra-migrate` без `.env` в CI падает (проверено на uv 0.11.24) → шаг `cp .env.example .env`; F7 `python` 3.13 в PATH → setup-python; F8 три места канона утверждают серверную защиту (AGENTS:54/65, AGENT-OPERATIONS:15) → переписаны по D-034; F9 prd CI-гейт ACL-leak без сервера неисполним машинно → D-034: требование остаётся, исполнение процедурное на слиянии; F10 gitleaks объявлен security.md:172, сканера нет → BL-035. Отложено в беклог: BL-036 (`plan_joins_demo` — psycopg из apps напрямую), BL-037 (domain «без внешних зависимостей» vs pydantic — дрейф формулировки). Санкция скоупа: Task 5 снят решением владельца (D-034); CI — compose вместо `services:` (правило 3 выше)
- REVIEW-GATE: [✅ <Ready to merge? вердикт> + <BASE7>..<HEAD7> | N/A — <причина>]

## Отчёт по сессии (заполняется по факту)

- Сделано:
  - [Task: что реально сделано, коммиты, число тестов/ассертов, live-прогон]
- Отклонения от плана:
  - [что сделали не так, как в задаче, и почему; каждое отклонение — осознанное]
- Перенесено в другие сессии:
  - [ID сессии — что перенесено и почему; если в беклог — BL-NN-файл в solution/backlog/ (см. backlog/README.md)]

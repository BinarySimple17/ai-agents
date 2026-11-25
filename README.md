# ai-agents — Text-to-SQL аналитик данных

Агент-аналитик данных: принимает бизнес-вопрос на естественном языке, строит
многотабличный SQL по графу связей, безопасно выполняет его под read-only
ролью и объясняет результат на языке бизнеса.

- **Живые прикладные требования**: [docs/prd.md](docs/prd.md) (трекаемый архитектурный пакет — вся работа здесь)
- **Историческая полная спецификация** (READ ONLY, вне гита): `.docs/hw-01/README.md`
- **Архитектурный пакет v1.8** (11 ADR, C4 L1–L3, FSM, ER, prd, security): [docs/arch-package.md](docs/arch-package.md)
- **Краткие свёртки для агента**: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/PRODUCT.md](docs/PRODUCT.md), канон — [solution/sessions/S00_session.md](solution/sessions/S00_session.md)
- **Правила работы ИИ-агента**: [AGENTS.md](AGENTS.md) — старт сессии, правило «одна фича за раз», Definition of Done, End of Session

**Стек:** Python 3.13 · LangGraph (граф на коде) · PostgreSQL (pgvector) · Neo4j (граф связей) · Valkey · OpenAI-совместимый шлюз (облако routerai.ru или локальный LM Studio — D-020). Зависимости — [uv](https://docs.astral.sh/uv/). Весь код — в `src/`: пакет `text_to_sql_agent`, презентационный слой `src/apps/` (CLI и Streamlit-демо), миграции Alembic `src/migrations/`, тесты `src/tests/`.

## Быстрый старт

```bash
cp .env.example .env          # локально ничего; для облака — LLM_BASE_URL + ключ
./init.sh                     # установка зависимостей + верификация (make check)
make infra-up                 # PostgreSQL + Valkey (по требованию)
make dev Q="Сколько заказов у клиентов из РФ?"   # identity — DEV_TOKEN из .env (S31);
# разовое переопределение: make dev Q="..." TOKEN=dev-logist-token (AGENT_DEV_TOKENS)
```

Верификация фич — `make verify-feature F=feat-XXX`; состояние фич — `feature_list.json`.

## Структура

```
├── AGENTS.md            ← главный файл: инструкция для агента
├── feature_list.json    ← трекер фич: единый источник правды по состоянию
├── progress.md          ← журнал сессий
├── DECISIONS.md         ← журнал решений
├── init.sh / Makefile   ← установка + верификация, все операции одной командой
├── docker-compose.yml   ← инфраструктура (PostgreSQL pgvector, Neo4j, Valkey)
├── src/                 ← весь код: text_to_sql_agent (слои), apps/ (CLI, Streamlit-демо),
│                          migrations/ (Alembic), tests/ (unit/integration/e2e)
├── hw-01/               ← решение ДЗ-1 (подготовленный к сдаче документ; hw-NN/ — решения ДЗ NN)
├── docs/                ← архпакет v1.7: prd, 11 ADR, C4/FSM/sequence/ER, security, observability, model-card, RISKS
├── solution/            ← план-сессий, канон (S00), беклог (backlog/), журнал противоречий (FIXES)
├── templates/           ← sprint-contract, evaluator-rubric, clean-state-checklist
└── scripts/             ← audit/validate/verify-feature/clean-state-check
```

## Harness

Шаблон harness собран по материалам курса
[walkinglabs/learn-harness-engineering](https://github.com/walkinglabs/learn-harness-engineering)
(MIT). Ключевая идея: **модель решает, какой код писать; harness решает, когда,
где и как она его пишет.** Проверка harness: `bash scripts/audit-harness.sh .`
и `node scripts/validate-harness.mjs .`.

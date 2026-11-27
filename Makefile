# Makefile — все операции репозитория одной командой (см. AGENTS.md).
# Стек: Python 3.13 + uv; инфраструктура — docker compose (PostgreSQL pgvector+AGE, Valkey).

SHELL := /bin/bash

.PHONY: setup dev check test lint lint-imports e2e infra-up infra-down infra-migrate index-knowledge sync-graph bench-retrieval verify-feature vcr archive new-session close-session session-start session-end clean-check

# Полная установка + верификация с чистого состояния
# (в Makefile — через `bash init.sh`: на Windows make гоняет рецепты через
# cmd.exe, который не исполняет ./init.sh напрямую)
setup:
	bash init.sh

# Полная верификация (единый критерий консистентности репозитория)
check:
	bash init.sh

# Только тесты (тесты с БД скипаются, если инфра не поднята)
test:
	uv run pytest

# Статика: lint + формат (весь код — в src/, включая apps/, миграции и тесты)
lint:
	uv run ruff check src
	uv run ruff format --check src

# Контракты импортов слоёв (S30, import-linter): тот же чек, что в init.sh между
# ruff и pytest — отдельно для быстрой обратной связи при правке импортов
lint-imports:
	uv run lint-imports

# E2E: сквозные проверки (запускаются при изменениях через границы компонентов;
# пустая папка — «no tests collected», не ошибка)
e2e:
	uv run pytest src/tests/e2e -q || [ $$? -eq 5 ]

# Локальный запуск агента (FSM — единственный контур после удаления тул-контура,
# BL-048/D-041): make dev Q="Сколько заказов у клиентов из РФ?"
# Identity (S31): проверенный токен обязателен для любого SQL-контура — берётся
# DEV_TOKEN из .env; TOKEN= переопределяет разово (значения — AGENT_DEV_TOKENS)
dev:
	@test -n "$(Q)" || { echo 'Usage: make dev Q="бизнес-вопрос" [TOKEN=dev-токен]'; exit 2; }
	uv run python -m apps.cli --token "$(TOKEN)" "$(Q)"

# Индексация кураторских знаний в pgvector (S06, feat-006): DDL/связи/глоссарий/few-shot;
# идемпотентно по source_ref — повторный запуск обновляет, не дублирует
index-knowledge:
	uv run --env-file .env python -m apps.index_knowledge

# Синк графовой памяти (S07, feat-007): активные строки реестра → Neo4j (только
# топология, ADR-003 v2.1); запускать после правки реестра и перед тестами путей
sync-graph:
	uv run --env-file .env python -m apps.sync_graph

# Бенч retrieval (S26, feat-021): hit@5 dense-only vs гибрид на фиксированных
# русских вопросах; инвариант D-026 «гибрид ≥ dense» — код 1 при обратном
# (ручная Layer-3 операция: реальный эмбеддер шлюза, тестами не вызывается)
bench-retrieval:
	uv run --env-file .env python -m apps.bench_retrieval

# --- Инфраструктура (PostgreSQL pgvector+AGE, Valkey) ---

infra-up:
	docker compose up -d --build
	docker compose exec -T postgres pg_isready -U postgres -d agent

infra-down:
	docker compose down

# Инициализация схемы БД — только миграциями Alembic (D-009, S00 правило 5).
# .env подхватывает uv (--env-file): alembic читает POSTGRES_DSN из окружения,
# секреты — только из .env (S00 правило 4), не из кода.
infra-migrate:
	uv run --env-file .env alembic upgrade head

# --- Harness ---

# Верификация одной фичи с записью evidence: make verify-feature F=feat-001
verify-feature:
	bash scripts/verify-feature.sh $(F)

# VCR — доля passing среди активированных фич (WIP=1 → VCR должен быть 1.0)
vcr:
	node -e "const f=require('./feature_list.json').features||[];const a=f.filter(function(x){return x.state==='active'}).length;const p=f.filter(function(x){return x.state==='passing'}).length;const t=a+p;console.log(t===0?'VCR: no activated features yet - OK to activate the first':'VCR = '+p+'/'+t+' = '+(p/t).toFixed(2));"

# Перенос закрытых сессий и беклогов в solution/*/archive/ + анти-раздувание
# «Текущего состояния» в sessions.md (см. scripts/archive.sh)
archive:
	bash scripts/archive.sh

# Механическая часть планирования сессии: копия шаблона + строка реестра;
# нарратив (предусловия/задачи/ловушки) — за агентом (AGENTS.md Startup шаг 4)
new-session:
	bash scripts/new-session.sh S="$(S)" TITLE="$(TITLE)" F="$(F)" ROLE="$(ROLE)" $(FLAGS)

# Механический хвост End of Session: гейты/отчёт/фичи → статус ✅ → реестр →
# «Текущее состояние» → archive → clean-state → commit -F MSG (docs/AGENT-OPERATIONS.md)
close-session:
	bash scripts/close-session.sh S="$(S)" MSG="$(MSG)" SUMMARY="$(SUMMARY)" $(FLAGS)

# Ритуалы начала/конца сессии (см. AGENTS.md: Startup Workflow / End of Session)
session-start:
	@echo "Clock-in: 1) pwd  2) прочитать AGENTS.md  3) создать файл сессии: make new-session S=<ID> TITLE=\"...\" F=<feat-XXX> (механика; нарратив — за агентом) и перевести шапку стартуемой сессии в «◐ в работе» — старт-гейт кусает только её (D-027)  4) прочитать progress.md  5) выбрать ОДНУ фичу из feature_list.json  6) запустить make check"
	bash scripts/guard-subagents.sh --phase start

session-end:
	bash scripts/guard-subagents.sh
	bash scripts/clean-state-check.sh .

# Протокол чистого состояния перед коммитом; многосессионная фича:
#   bash scripts/clean-state-check.sh . feat-0XX  (фича сознательно остаётся active до финальной сессии)
clean-check:
	bash scripts/guard-subagents.sh
	bash scripts/clean-state-check.sh .

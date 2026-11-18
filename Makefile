# Makefile — все операции репозитория одной командой (см. AGENTS.md).
# Стек: Python 3.13 + uv; инфраструктура — docker compose (PostgreSQL pgvector+AGE, Valkey).

SHELL := /bin/bash

.PHONY: setup dev check test lint e2e infra-up infra-down infra-migrate verify-feature vcr archive session-start session-end clean-check

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

# E2E: сквозные проверки (запускаются при изменениях через границы компонентов;
# пустая папка — «no tests collected», не ошибка)
e2e:
	uv run pytest src/tests/e2e -q || [ $$? -eq 5 ]

# Локальный запуск агента: make dev Q="Сколько заказов у клиентов из Москвы?"
dev:
	@test -n "$(Q)" || { echo 'Usage: make dev Q="бизнес-вопрос"'; exit 2; }
	uv run python -m apps.cli "$(Q)"

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

# Ритуалы начала/конца сессии (см. AGENTS.md: Startup Workflow / End of Session)
session-start:
	@echo "Clock-in: 1) pwd  2) прочитать AGENTS.md  3) прочитать progress.md  4) запустить make check  5) выбрать ОДНУ фичу из feature_list.json"

session-end:
	bash scripts/guard-subagents.sh
	bash scripts/clean-state-check.sh .

# Протокол чистого состояния перед коммитом; многосессионная фича:
#   bash scripts/clean-state-check.sh . feat-0XX  (фича сознательно остаётся active до финальной сессии)
clean-check:
	bash scripts/guard-subagents.sh
	bash scripts/clean-state-check.sh .

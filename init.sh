#!/bin/bash
# init.sh — стандартный путь инициализации и верификации harness.
# Запускается агентом в начале сессии (Startup Workflow) и перед claim'ом "done".
# Стек проекта: Python 3.13 + uv (зависимости — pyproject.toml / uv.lock).

set -e

echo "=== Harness Initialization ==="

# Гигиена веток: локальные гварды pre-commit/pre-push (AGENTS.md § Branch Hygiene).
# Идемпотентно: hooksPath указывает на версионированные .githooks/, права не критичны на Windows.
if [ -d .githooks ]; then
  git config core.hooksPath .githooks
  chmod +x .githooks/* 2>/dev/null || true
fi

# Гейт старта сессии (D-023/D-027, AGENTS.md Startup Workflow шаг 8): кусает только
# сессию, которую СТАРТУЮТ — маркер: статус шапки «◐ в работе» (запланированные
# «[ ] не начата» скипаются; ✅ в корне — напоминание про make archive). Для стартованной:
# ARCH-GATE до кода — незаполненный/«сессия не начата» = FAIL, make check красный до
# canon-check (✅/N/A). Стартованных нет — WARN, не блокер (рестартуемость).
# Fail-fast: до uv-шагов.
if [ -f scripts/guard-subagents.sh ]; then
  bash scripts/guard-subagents.sh --phase start
fi

if [ -f pyproject.toml ]; then
  echo "=== Running Python verification (uv) ==="
  command -v uv >/dev/null 2>&1 || {
    echo "ERROR: uv не найден. Установка: https://docs.astral.sh/uv/getting-started/installation/"
    exit 1
  }
  command -v python >/dev/null 2>&1 || {
    echo "ERROR: python не найден (нужен Python 3.13)."
    exit 1
  }

  # Зависимости — строго из lockfile (воспроизводимая сборка)
  uv sync --frozen

  # Layer 1 — статика
  uv run ruff check src
  uv run ruff format --check src

  # Layer 2 — тесты. Тесты, требующие PostgreSQL/Valkey, скипаются,
  # если инфра не поднята (см. src/tests/conftest.py) — make check остаётся зелёным.
  # pytest exits 5 when no tests are collected — not a failure for a fresh project.
  uv run pytest -q || [ $? -eq 5 ]

  # Синтаксис всех исходников (код, миграции, тесты — всё в src/)
  uv run python -m compileall -q src

  # Self-test гварда субагентов: он — гейт в этой цепочке, поэтому верифицируется
  # на каждом прогоне (фикстурные прогонки обеих фаз, см. шапку теста)
  if [ -f scripts/tests/test-guard-subagents.sh ]; then
    bash scripts/tests/test-guard-subagents.sh
  fi

  # Self-test ритуальных скриптов (new-session/close-session): мутации git-состояния
  # в песочнице-worktree, см. шапку scripts/tests/test-ritual-scripts.sh (S28)
  if [ -f scripts/tests/test-ritual-scripts.sh ]; then
    bash scripts/tests/test-ritual-scripts.sh
  fi
else
  echo "No recognized package manifest detected."
  echo "Ожидается pyproject.toml в корне проекта (см. feat-001 в feature_list.json)."
fi

echo "=== Verification Complete ==="
echo ""
echo "Next steps:"
echo "1. Read feature_list.json to see current feature state"
echo "2. Pick ONE unfinished feature to work on"
echo "3. Implement only that feature"
echo "4. Re-run verification before claiming done"

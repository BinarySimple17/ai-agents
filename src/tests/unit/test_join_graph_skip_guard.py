"""Регрессионный тест skip-гварда integration-тестов графа (ревью S07 Critical-1).

behavior AGENTS.md: тесты с PostgreSQL/Neo4j скипаются с понятной причиной,
если инфра не поднята — `./init.sh` остаётся зелёным без Docker. До фикса
фикстуры test_join_graph.py не зависели от `postgres_available`/`neo4j_available`:
module-scope `store` доходил до `sync()`, получал `SchemaGraphError` и давал
ERROR (exit 1) вместо skip — `make check` без Docker был красным.

Тест падает на до-фиксовом коде (subprocess → exit 1, «errors»), остаётся
в наборе (правило харнесса: Critical закрывается регрессионным тестом).
Запускает pytest в subprocess с недостижимыми DSN (env-переменные приоритетнее
.env в pydantic-settings, conftest читает os.getenv — setdefault пропускает
наши оверрайды); собственные тесты при этом не трогают живую инфру.
"""

import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]

_DEAD_ENV = {
    # Недостижимые TCP-адреса: availability-фикстуры conftest читают эти ключи
    "POSTGRES_HOST": "127.0.0.1",
    "POSTGRES_PORT": "1",
    "NEO4J_URI": "bolt://127.0.0.1:1",
}

_TIMEOUT_S = 180  # subprocess-прогон одного модуля — не зависающая команда


def test_join_graph_module_skips_without_infra():
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "src/tests/integration/test_join_graph.py",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=_REPO_ROOT,
        env={**os.environ, **_DEAD_ENV},
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_S,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"тесты графа без инфры должны скипаться, а не падать "
        f"(exit {result.returncode}):\n{output[-2000:]}"
    )
    assert "skipped" in output.lower(), f"нет skip-строки в выводе:\n{output[-2000:]}"
    assert "error" not in output.lower(), f"ERROR в выводе (падение, не skip):\n{output[-2000:]}"

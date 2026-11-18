"""Smoke-тест feat-001: пакет собирается и импортируется."""

import text_to_sql_agent


def test_package_imports() -> None:
    assert text_to_sql_agent.__version__ == "0.1.0"

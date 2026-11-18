"""Юнит-тесты классификатора ошибок адаптера (S13, ревью I3): чистая функция без БД.

Каждый класс сбоя должен резолвиться по фактическим MRO psycopg 3 — ветки
READ_ONLY/CONNECTION не исполнялись интеграционными тестами напрямую.
"""

import psycopg
import pytest

from text_to_sql_agent.application.ports.sql_executor import SQLFailureClass
from text_to_sql_agent.infrastructure.persistence.postgres_executor import _classify


@pytest.mark.parametrize(
    ("exception", "expected"),
    [
        (psycopg.errors.QueryCanceled("timeout"), SQLFailureClass.TIMEOUT),
        (psycopg.errors.ReadOnlySqlTransaction("read-only"), SQLFailureClass.READ_ONLY),
        (psycopg.errors.InsufficientPrivilege("denied"), SQLFailureClass.PERMISSION_DENIED),
        (psycopg.errors.FeatureNotSupported("unsupported"), SQLFailureClass.SYNTAX),
        (psycopg.errors.SyntaxError("bad grammar"), SQLFailureClass.SYNTAX),
        (psycopg.errors.UndefinedTable("no table"), SQLFailureClass.SYNTAX),
        (psycopg.errors.ProgrammingError("program"), SQLFailureClass.SYNTAX),
        (psycopg.OperationalError("connection"), SQLFailureClass.CONNECTION),
        (psycopg.DataError("data"), SQLFailureClass.UNKNOWN),
    ],
)
def test_classify_by_psycopg_mro(exception: psycopg.Error, expected: SQLFailureClass) -> None:
    assert _classify(exception) == expected

"""Общие fixtures тестов.

Правило из канона S00 (правило 7): тесты с PostgreSQL/Valkey скипаются с
понятной причиной, если инфра не поднята, — `./init.sh` остаётся зелёным
без Docker. Здесь только проверка TCP-доступности; тестовое чтение env —
допустимо (ограничение «env только через Settings» — для бизнес-кода).
"""

import os
import socket

import pytest


def _reachable(host: str, port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        try:
            sock.connect((host, port))
        except OSError:
            return False
    return True


@pytest.fixture(scope="session")
def postgres_available() -> bool:
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = int(os.getenv("POSTGRES_PORT", "5432"))
    if not _reachable(host, port):
        pytest.skip(
            f"PostgreSQL недоступен на {host}:{port} — поднимите инфраструктуру: make infra-up"
        )
    return True


@pytest.fixture(scope="session")
def valkey_available() -> bool:
    host = os.getenv("VALKEY_HOST", "localhost")
    port = int(os.getenv("VALKEY_PORT", "6379"))
    if not _reachable(host, port):
        pytest.skip(f"Valkey недоступен на {host}:{port} — поднимите инфраструктуру: make infra-up")
    return True

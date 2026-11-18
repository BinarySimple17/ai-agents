"""Общие fixtures тестов.

Правило из канона S00 (правило 7): тесты с PostgreSQL/Valkey скипаются с
понятной причиной, если инфра не поднята, — `./init.sh` остаётся зелёным
без Docker. Здесь только проверка TCP-доступности; тестовое чтение env —
допустимо (ограничение «env только через Settings» — для бизнес-кода).

.env подгружается в os.environ (setdefault — реальное окружение приоритетно):
пароли ролей и DSN'ы нужны integration-тестам (feat-002/013), Settings появится
в feat-003 (S03) — тогда чтение env в тестах переедет на неё.
"""

import os
import socket
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv() -> None:
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()


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
    host = os.getenv("POSTGRES_HOST", "127.0.0.1")
    port = int(os.getenv("POSTGRES_PORT", "5432"))
    if not _reachable(host, port):
        pytest.skip(
            f"PostgreSQL недоступен на {host}:{port} — поднимите инфраструктуру: make infra-up"
        )
    return True


@pytest.fixture(scope="session")
def valkey_available() -> bool:
    host = os.getenv("VALKEY_HOST", "127.0.0.1")
    port = int(os.getenv("VALKEY_PORT", "6379"))
    if not _reachable(host, port):
        pytest.skip(f"Valkey недоступен на {host}:{port} — поднимите инфраструктуру: make infra-up")
    return True


@pytest.fixture(scope="session")
def neo4j_available() -> bool:
    uri = os.getenv("NEO4J_URI", "bolt://127.0.0.1:7687")
    port = int(uri.rsplit(":", 1)[1]) if ":" in uri else 7687
    if not _reachable("127.0.0.1", port):
        pytest.skip(f"Neo4j недоступен на {uri} — поднимите инфраструктуру: make infra-up")
    return True

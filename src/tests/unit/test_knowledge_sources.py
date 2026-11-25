"""Юнит-тесты сборщиков кураторских знаний (S06, ревью-2 Important-1 — вторая линия
инварианта C1).

Проверяются: DDL-док без REFERENCES-фрагментов (санитизация FK — имена чужих
таблиц не проносятся в контент), объявление ровно своей таблицы; парсинг
markdown-заголовков «таблица X» / «таблицы X, Y» / null с отказом на неизвестной
таблице; guard `check_declared_references` — падает на необъявленном упоминании
(до-фиксовое поведение C1 — молчаливая утечка), проходит при полном объявлении.
Поведение на живой БД — integration/test_vector_memory.py.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from text_to_sql_agent.application.ports.vector_store import DocKind, KnowledgeDocument
from text_to_sql_agent.infrastructure.memory.knowledge_sources import (
    check_declared_references,
    ddl_documents,
    markdown_documents,
)

_MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "migrations"
    / "versions"
    / "20261118_0001_schema_v15.py"
)

# Таблицы демо-схемы (сид каталога agent_data_sources, миграция 0003) —
# BL-009 M1: сборщики параметризованы SoT-каталогом, константы больше нет
_TABLES = (
    "customers",
    "campaigns",
    "products",
    "orders",
    "order_items",
    "employees",
    "bonuses",
)


def _glossary(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "glossary.md"
    path.write_text(text, encoding="utf-8")
    return path


# --- DDL: санитизация FK + объявление ровно своей таблицы ---
def test_ddl_docs_sanitize_fk_and_declare_own_table() -> None:
    docs = ddl_documents(_MIGRATION, model_version="m", business_tables=_TABLES)
    assert len(docs) == 7
    orders = next(d for d in docs if d.table_name == "orders")
    # FK-ссылки вырезаны: имена customers/campaigns не проносятся в DDL-контент (C1)
    assert "REFERENCES" not in orders.content
    assert "customers" not in orders.content and "campaigns" not in orders.content
    # структура и колонки сохранены
    assert "customer_id" in orders.content and "CREATE TABLE orders" in orders.content
    assert orders.referenced_tables == ["orders"]


def test_ddl_docs_missing_table_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = tmp_path / "migration.py"
    fake.write_text("CREATE TABLE orders (\n    id integer PRIMARY KEY\n);", encoding="utf-8")
    with pytest.raises(ValueError, match="не найдены CREATE TABLE"):
        ddl_documents(fake, model_version="m", business_tables=_TABLES)


# --- markdown: форматы заголовков и отказ на неизвестной таблице ---
def test_markdown_parses_singular_plural_and_null(tmp_path: Path) -> None:
    path = _glossary(
        tmp_path,
        "# Заголовок\n"
        "## [термин: один] — таблица orders\n"  # ← одна (сингулярная форма)
        "Тело одного.\n"
        "## [термин: кросс] — таблицы orders, campaigns\n"  # ← кросс-табличный
        "Тело кросса.\n"
        "## [термин: общий] — таблица null\n"  # ← общий документ
        "Тело общего.\n",
    )
    docs = markdown_documents(
        path, doc_kind=DocKind.GLOSSARY, model_version="m", business_tables=_TABLES
    )
    assert [d.referenced_tables for d in docs] == [["orders"], ["orders", "campaigns"], []]
    assert [d.table_name for d in docs] == ["orders", "orders", None]
    assert docs[0].content.startswith("## [термин: один] — таблица orders")
    assert docs[2].source_ref == "glossary:общий"


def test_markdown_rejects_foreign_doc_kind(tmp_path: Path) -> None:
    path = _glossary(tmp_path, "## [термин: x] — таблица orders\nТело.\n")
    with pytest.raises(ValueError, match="только как glossary/few_shot"):
        markdown_documents(path, doc_kind=DocKind.DDL, model_version="m", business_tables=_TABLES)


def test_build_documents_runs_the_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    # Удаление вызова guard'а в build_documents не должно проходить незамеченным:
    # кривой документ из любого сборщика -> ValueError на индексации.
    # build_documents живёт в composition (S30 Task 2, canon-check F5) — патчим
    # имена фабрики, не apps-модуль
    import text_to_sql_agent.composition.knowledge_docs as docs_mod

    leak = KnowledgeDocument(
        doc_kind=DocKind.DDL,
        table_name="orders",
        referenced_tables=["orders"],
        content="[таблица: orders] … и bonuses.amount",
        model_version="m",
        source_ref="ddl:orders",
    )
    monkeypatch.setattr(docs_mod, "business_tables_from_catalog", lambda *a, **kw: _TABLES)
    monkeypatch.setattr(docs_mod, "ddl_documents", lambda *a, **kw: [leak])
    monkeypatch.setattr(docs_mod, "relationship_documents", lambda *a, **kw: [])
    monkeypatch.setattr(docs_mod, "markdown_documents", lambda *a, **kw: [])
    monkeypatch.setattr(docs_mod, "generate_noise_corpus", lambda *a, **kw: [])
    container = SimpleNamespace(
        settings=SimpleNamespace(
            embedding_model="m", postgres_dsn=SecretStr("postgresql://u:p@h/db")
        )
    )
    with pytest.raises(ValueError, match="bonuses"):
        docs_mod.build_documents(container)


def test_markdown_unknown_table_in_header_rejected(tmp_path: Path) -> None:
    path = _glossary(tmp_path, "## [термин: x] — таблица secret_table\nТело.\n")
    with pytest.raises(ValueError, match="вне каталога источников"):
        markdown_documents(
            path, doc_kind=DocKind.GLOSSARY, model_version="m", business_tables=_TABLES
        )


def test_markdown_without_sections_rejected(tmp_path: Path) -> None:
    path = _glossary(tmp_path, "# Просто текст без разделов\n")
    with pytest.raises(ValueError, match="не найдено ни одного раздела"):
        markdown_documents(
            path, doc_kind=DocKind.GLOSSARY, model_version="m", business_tables=_TABLES
        )


# --- guard: вторая линия инварианта C1 ---
def test_guard_rejects_undeclared_table_mention() -> None:
    docs = ddl_documents(_MIGRATION, model_version="m", business_tables=_TABLES)
    # Санитизированный DDL проходит guard: контент ⊆ объявленных таблиц
    check_declared_references(docs, business_tables=_TABLES)
    # До-фиксовая утечка: контент упоминает bonuses, объявлена только orders
    leak = next(d for d in docs if d.table_name == "orders").model_copy(
        update={"content": "[few-shot] выручка и премии: orders.total + bonuses.amount"}
    )
    with pytest.raises(ValueError, match="bonuses"):
        check_declared_references([leak], business_tables=_TABLES)


def test_guard_accepts_full_declaration(tmp_path: Path) -> None:
    path = _glossary(
        tmp_path,
        "## [термин: кросс] — таблицы orders, bonuses\nТело: orders.total + bonuses.amount.\n",
    )
    docs = markdown_documents(
        path, doc_kind=DocKind.GLOSSARY, model_version="m", business_tables=_TABLES
    )
    check_declared_references(docs, business_tables=_TABLES)  # не падает

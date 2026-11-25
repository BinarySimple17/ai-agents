"""Фабрика документов знаний для офлайн-индексатора (S30 Task 2, canon-check F1/F5).

Сборка канонических документов (ADR-003 §4) и шумового корпуса живёт в composition:
`apps` не импортирует `infrastructure` напрямую (контракт import-linter «apps ->
infrastructure НАПРЯМУЮ», находка F1 ARCH-GATE S30 — аудит 2026-11-23 её не видел),
а сборка зависимостей и их источников — только здесь (AGENTS.md «Composition —
единственное место сборки»). Логика перенесена из `src/apps/index_knowledge.py`
без изменений: поведение `make index-knowledge` то же.
"""

from pathlib import Path

from text_to_sql_agent.application.ports.vector_store import DocKind, KnowledgeDocument
from text_to_sql_agent.composition.container import Container
from text_to_sql_agent.infrastructure.memory.knowledge_sources import (
    business_tables_from_catalog,
    check_declared_references,
    ddl_documents,
    markdown_documents,
    relationship_documents,
)
from text_to_sql_agent.infrastructure.memory.noise_corpus import generate_noise_corpus

# Корень репо: composition/ на уровень глубже, чем apps/ (parents[3] против parents[2] — F5)
_REPO_ROOT = Path(__file__).resolve().parents[3]
_MIGRATION_0001 = _REPO_ROOT / "src" / "migrations" / "versions" / "20261118_0001_schema_v15.py"
_GLOSSARY_MD = _REPO_ROOT / "docs" / "glossary.md"
_FEWSHOT_MD = _REPO_ROOT / "docs" / "few_shot.md"


def build_documents(container: Container) -> list[KnowledgeDocument]:
    """Канонические документы (ADR-003 §4) + шумовой корпус (S26, D-026).

    Порядок — кураторские знанием первыми, шум после (Task 4): неймспейсы одной
    replace-коллекции; шум участвует в ранжировании, в промпт не попадает.
    """
    model_version = container.settings.embedding_model
    service_dsn = container.settings.postgres_dsn.get_secret_value()
    # SoT бизнес-таблиц — каталог источников (BL-009 M1), не константа
    business_tables = business_tables_from_catalog(service_dsn)
    docs = ddl_documents(
        _MIGRATION_0001, model_version=model_version, business_tables=business_tables
    )
    docs += relationship_documents(service_dsn, model_version=model_version)
    docs += markdown_documents(
        _GLOSSARY_MD,
        doc_kind=DocKind.GLOSSARY,
        model_version=model_version,
        business_tables=business_tables,
    )
    docs += markdown_documents(
        _FEWSHOT_MD,
        doc_kind=DocKind.FEW_SHOT,
        model_version=model_version,
        business_tables=business_tables,
    )
    docs += generate_noise_corpus(business_tables, model_version=model_version)
    # guard прав (C1): содержимое ⊆ объявленные таблицы — универсален, шум
    # объявляет таблицы по упоминаниям (ruling N2 ARCH-GATE S26)
    check_declared_references(docs, business_tables=business_tables)
    return docs

"""Тесты генератора шумового корпуса-дистрактора (S26 Task 4; D-026 доп. 1(3))."""

import re

import pytest

from text_to_sql_agent.application.ports.vector_store import DocKind
from text_to_sql_agent.infrastructure.memory.knowledge_sources import check_declared_references
from text_to_sql_agent.infrastructure.memory.noise_corpus import (
    NOISE_CORPUS_SIZE,
    NOISE_SEED,
    generate_noise_corpus,
)

_TABLES = ("customers", "campaigns", "products", "orders", "order_items", "employees", "bonuses")
_MODEL = "test-embedder"


def test_generation_is_bitwise_deterministic() -> None:
    # Самопроверка канона (D-026 доп. 1(3)): двойная генерация — побитово тот
    # же результат (фиксированный сид, только stdlib)
    first = generate_noise_corpus(_TABLES, model_version=_MODEL)
    second = generate_noise_corpus(_TABLES, model_version=_MODEL)
    assert first == second
    # Смена сида-константы ломает воспроизводимость — сид обязан быть зафиксирован
    assert NOISE_SEED == 20261121


def test_corpus_size_and_refs() -> None:
    docs = generate_noise_corpus(_TABLES, model_version=_MODEL)
    assert len(docs) == NOISE_CORPUS_SIZE
    refs = [d.source_ref for d in docs]
    assert refs == [f"noise:{i + 1:04d}" for i in range(NOISE_CORPUS_SIZE)]
    assert len(set(refs)) == NOISE_CORPUS_SIZE


def test_noise_docs_are_noise_kind_without_anchor() -> None:
    # D-026: doc_kind='noise', table_name NULL — шум не привязан к якорю-таблице
    docs = generate_noise_corpus(_TABLES, model_version=_MODEL)
    assert all(d.doc_kind is DocKind.NOISE for d in docs)
    assert all(d.table_name is None for d in docs)
    assert all(d.model_version == _MODEL for d in docs)


def test_distractor_shares_feature_with_target() -> None:
    # Дистрактор ДЕЛИТ общий признак с целью: упоминает реальные таблицы
    # (заголовок-якорь FTS-ноги) — иначе задача вырождается и hit@5 обманчиво
    # высок (Ловушки-4)
    docs = generate_noise_corpus(_TABLES, model_version=_MODEL)
    tables_re = re.compile(r"\b(" + "|".join(_TABLES) + r")\b")
    for doc in docs:
        mentioned = set(tables_re.findall(doc.content))
        assert mentioned, f"{doc.source_ref}: дистрактор без признака цели — вырожденный шум"
        # Ровно объявленные таблицы (не больше): guard-совместимость по построению
        assert mentioned == set(doc.referenced_tables), (
            f"{doc.source_ref}: упомянуто {mentioned}, объявлено {doc.referenced_tables}"
        )


def test_noise_passes_rights_guard() -> None:
    # Ruling N2 S26: шум объявляет referenced_tables по упоминаниям — guard
    # универсален, NFR-4 без исключений
    docs = generate_noise_corpus(_TABLES, model_version=_MODEL)
    check_declared_references(docs, business_tables=_TABLES)  # не падает


def test_cross_table_share_is_substantial() -> None:
    # ~2/3 дистрактора кросс-табличные: у кураторских знаний кросс-табличных
    # большинство — однотабличный шум слабо конкурирует с ними
    docs = generate_noise_corpus(_TABLES, model_version=_MODEL)
    cross = sum(1 for d in docs if len(d.referenced_tables) == 2)
    assert cross == 2 * NOISE_CORPUS_SIZE // 3


def test_fewer_than_two_tables_rejected() -> None:
    # Дистрактор «делит признак с целью» требует ≥ 2 таблиц (кросс-варианты)
    with pytest.raises(ValueError, match="≥ 2 таблиц"):
        generate_noise_corpus(("orders",), model_version=_MODEL)


def test_table_without_curated_vocab_gets_fallback() -> None:
    # I2 ревью S26 (регресс на KeyError): новая бизнес-таблица в каталоге
    # agent_data_sources (BL-009 M1: «без правки кода») не должна валить
    # индексацию — генератор берёт фолбэк-лексику, дистрактор по-прежнему
    # делит признак с целью (имя таблицы) и проходит guard прав
    docs = generate_noise_corpus(("orders", "new_metric_table"), model_version=_MODEL)
    assert len(docs) == NOISE_CORPUS_SIZE
    assert all(d.doc_kind is DocKind.NOISE for d in docs)
    tables_re = re.compile(r"\b(new_metric_table|orders)\b")
    for doc in docs:
        mentioned = set(tables_re.findall(doc.content))
        assert mentioned == set(doc.referenced_tables)
    check_declared_references(docs, business_tables=("orders", "new_metric_table"))

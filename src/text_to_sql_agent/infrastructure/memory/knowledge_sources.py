"""Сборщики кураторских документов знаний (S06 Task 6) — источники для индексации.

Канон (ADR-003 §4): содержимое — кураторское, автонаполнение «успешным по
трейсу» запрещено. Источники (hw-plan §3):
1. ddl — миграция 0001 (CREATE TABLE с inline-комментариями: PG-каталог
   inline-комменты не хранит, файл миграции — канон DDL; офлайн-тул репозитория);
2. relationship — вьюха agent_relationships_active (meaning/usage_notes);
3. glossary — docs/glossary.md (термин → определение);
4. few_shot — docs/few_shot.md («вопрос → SQL»).

Каждый чанк — с префиксом-заголовком («[таблица: orders] …») — сигнал для
эмбеддера и якорь имени таблицы/термина (ACCURACY §6.2).

Права retrieval (NFR-4 ACL-leak = 0, ревью S06 Critical-1) — по всем таблицам
документа: `referenced_tables` заполняется здесь (DDL — сама таблица;
relationship — left+right; markdown — явные метаданные заголовка «таблицы A, B»)
и проверяется guard-ом: содержимое не может упоминать таблицу вне объявленного
списка — кривой заголовок куратора падает на индексации, а не течёт в контекст.
"""

import re
from collections.abc import Sequence
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from text_to_sql_agent.application.ports.sql_executor import CONNECT_TIMEOUT_S, STATEMENT_TIMEOUT_S
from text_to_sql_agent.application.ports.vector_store import (
    RELATIONSHIP_SOURCE_REF_PREFIX,
    DocKind,
    KnowledgeDocument,
    VectorStoreError,
)
from text_to_sql_agent.infrastructure.memory.errors import shorten_error_message


def business_tables_from_catalog(dsn: str) -> tuple[str, ...]:
    """SoT списка бизнес-таблиц — каталог `agent_data_sources` (ADR-011; BL-009 M1).

    Раньше список дублировался константой (knowledge_sources / test_infra_schema /
    SYSTEM_PROMPT / сиды): новая бизнес-таблица молча не попадала в DDL-доки.
    v1 — все строки каталога (сид 0003: все таблицы демо-схемы, protocol='sql');
    сортировка — детерминизм guard-regex и сообщений об ошибках.
    """
    query = "SELECT table_name FROM agent_data_sources ORDER BY table_name"
    try:
        with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=CONNECT_TIMEOUT_S) as conn:
            conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT_S}s'")  # правило 9
            rows = conn.execute(query).fetchall()
    except psycopg.Error as exc:
        raise VectorStoreError(
            f"каталог источников недоступен: {shorten_error_message(str(exc))}"
        ) from exc
    tables = tuple(row["table_name"] for row in rows)
    if not tables:
        raise ValueError(
            "каталог agent_data_sources пуст — индексировать нечего; засейте каталог "
            "миграцией (снимок 0003) или добавьте источники (ADR-011)"
        )
    return tables


# Блок CREATE TABLE в SCHEMA_SQL миграции 0001: от заголовка до `\n);`
# (DEFAULT now() внутри строки не мешает — конец блока всегда на своей строке)
_CREATE_TABLE_RE = re.compile(r"CREATE TABLE (\w+) \(\n(.*?)\n\);", re.DOTALL)

# Заголовок раздела glossary/few_shot: `## [термин: X] — таблица Y` (одна) или
# `## [термин: X] — таблицы Y, Z` (кросс-табличный контент — ревью S06 C1)
_SECTION_RE = re.compile(
    r"^## \[(?:термин|few-shot): ([^\]]+)\] — таблиц[ыа] (.+?)\s*$", re.MULTILINE
)


def _doc(
    kind: DocKind,
    content: str,
    *,
    table_name: str | None,
    referenced_tables: list[str],
    source_ref: str,
    model_version: str,
) -> KnowledgeDocument:
    return KnowledgeDocument(
        doc_kind=kind,
        table_name=table_name,
        referenced_tables=referenced_tables,
        content=content,
        model_version=model_version,
        source_ref=source_ref,
    )


def _tables_field(field: str, *, business_tables: Sequence[str]) -> list[str]:
    """Позиция заголовка → список таблиц: «orders, customers» → [...]; «null» → []."""
    names = [part.strip() for part in field.split(",")]
    if names == ["null"]:
        return []
    unknown = [name for name in names if name not in business_tables]
    if unknown:
        raise ValueError(f"заголовок объявляет таблицы вне каталога источников: {unknown}")
    return names


def check_declared_references(
    docs: list[KnowledgeDocument], *, business_tables: Sequence[str]
) -> None:
    """Guard инварианта прав: содержимое не упоминает таблицы вне referenced_tables.

    Вторая линия инварианта C1 (первая — фильтр в SQL поиска): кривой заголовок
    куратора (забыл дописать вторую таблицу) падает ЗДЕСЬ, при индексации, а не
    течёт в контекст пользователя с чужими именами таблиц. Универсален и для
    шумового корпуса (ruling N2 S26: дистрактор объявляет таблицы, которые
    упоминает, — NFR-4 без исключений).
    """
    if not business_tables:
        raise ValueError("business_tables пуст — guard не может построить regex таблиц")
    # re.escape: имена таблиц идут в regex как литералы — метасимвол в будущем
    # имени не сделает guard непредсказуемым (m9 ревью S26)
    tables_re = re.compile(r"\b(" + "|".join(re.escape(t) for t in business_tables) + r")\b")
    for doc in docs:
        mentioned = set(tables_re.findall(doc.content))
        undeclared = mentioned - set(doc.referenced_tables)
        if undeclared:
            raise ValueError(
                f"{doc.source_ref}: содержимое упоминает таблицы {sorted(undeclared)}, "
                "не объявленные в referenced_tables — дополните заголовок раздела "
                "(«таблицы A, B»)"
            )


# FK-ссылка в DDL: ` REFERENCES customers(customer_id)` — вырезается из
# индексируемого контента: имя чужой таблицы в DDL = утечка вне прав (C1);
# семантику джойнов несёт relationship-док и граф связей (S07), не DDL
_FK_REFERENCE_RE = re.compile(r"\s*REFERENCES \w+\s*\([^)]*\)")


def ddl_documents(
    migration_path: Path, *, model_version: str, business_tables: Sequence[str]
) -> list[KnowledgeDocument]:
    """DDL бизнес-таблиц с комментариями колонок — из файла миграции 0001.

    REFERENCES-фрагменты вырезаются из контента (см. _FK_REFERENCE_RE) — иначе
    DDL orders проносил бы customers/campaigns в контекст пользователя без прав.
    Список таблиц — из каталога agent_data_sources (BL-009 M1), не константой:
    новая таблица в каталоге без CREATE TABLE в миграции падает ЗДЕСЬ.
    """
    sql = migration_path.read_text(encoding="utf-8")
    docs = []
    for table, body in _CREATE_TABLE_RE.findall(sql):
        if table not in business_tables:
            continue
        sanitized = _FK_REFERENCE_RE.sub("", body)
        content = (
            f"[таблица: {table}] DDL демо-схемы (PostgreSQL):\n"
            f"CREATE TABLE {table} (\n{sanitized}\n);"
        )
        docs.append(
            _doc(
                DocKind.DDL,
                content,
                table_name=table,
                referenced_tables=[table],
                source_ref=f"ddl:{table}",
                model_version=model_version,
            )
        )
    missing = set(business_tables) - {d.table_name for d in docs}
    if missing:
        raise ValueError(
            f"миграция {migration_path.name}: не найдены CREATE TABLE для {sorted(missing)}"
        )
    return docs


def relationship_documents(dsn: str, *, model_version: str) -> list[KnowledgeDocument]:
    """Связи с семантикой — из вьюхи agent_relationships_active (active-рёбра).

    referenced_tables — обе стороны ребра: пользователь без прав на правую
    таблицу ребро не видит (NFR-4 ACL-leak = 0, ревью S06 Critical-1).
    """
    query = (
        "SELECT edge_id, left_table, right_table, join_type, cardinality, meaning, usage_notes "
        "FROM agent_relationships_active ORDER BY edge_id"
    )
    docs = []
    try:
        with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=CONNECT_TIMEOUT_S) as conn:
            conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT_S}s'")  # правило 9
            rows = conn.execute(query).fetchall()
    except psycopg.Error as exc:
        # Типизированная ошибка стыка (прецедент адаптера памяти), не сырой psycopg
        raise VectorStoreError(
            f"реестр связей недоступен: {shorten_error_message(str(exc))}"
        ) from exc
    for row in rows:
        lines = [
            f"[связь: {row['left_table']} → {row['right_table']}] "
            f"join_type={row['join_type']}, cardinality={row['cardinality']}",
            f"Смысл: {row['meaning']}",
        ]
        if row["usage_notes"]:
            lines.append(f"Заметки: {row['usage_notes']}")
        docs.append(
            _doc(
                DocKind.RELATIONSHIP,
                "\n".join(lines),
                table_name=row["left_table"],  # якорь-таблица (для отладки/аудита)
                referenced_tables=[row["left_table"], row["right_table"]],
                source_ref=f"{RELATIONSHIP_SOURCE_REF_PREFIX}{row['edge_id']}",
                model_version=model_version,
            )
        )
    return docs


def markdown_documents(
    path: Path, *, doc_kind: DocKind, model_version: str, business_tables: Sequence[str]
) -> list[KnowledgeDocument]:
    """Разделы `## [термин|few-shot: имя] — таблица|таблицы …` кураторского файла.

    Заголовок остаётся в чанке (якорь термина/примера); позиция после тире —
    одна таблица, список через запятую (кросс-табличный контент) или `null`
    (общий документ). doc_kind — явно (опечатка в префиксе не должна молча
    проиндексировать few-shot как глоссарий — ревью S06 Minor-4); сторонний
    DocKind не маппится молча (ревью-2: неверный source_ref-префикс). Таблицы
    заголовков сверяются с каталогом agent_data_sources (BL-009 M1).
    """
    if doc_kind not in (DocKind.GLOSSARY, DocKind.FEW_SHOT):
        raise ValueError(
            f"markdown-разделы индексируются только как glossary/few_shot, получен {doc_kind}"
        )
    text = path.read_text(encoding="utf-8")
    matches = list(_SECTION_RE.finditer(text))
    if not matches:
        raise ValueError(
            f"{path.name}: не найдено ни одного раздела '## [термин|few-shot: …] — таблица …'"
        )
    docs = []
    for i, match in enumerate(matches):
        name, field = match.group(1), match.group(2)
        tables = _tables_field(field, business_tables=business_tables)
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        content = text[match.start() : body_end].strip()
        docs.append(
            _doc(
                doc_kind,
                content,
                table_name=tables[0] if tables else None,
                referenced_tables=tables,
                source_ref=f"{'few_shot' if doc_kind is DocKind.FEW_SHOT else 'glossary'}:{name}",
                model_version=model_version,
            )
        )
    return docs

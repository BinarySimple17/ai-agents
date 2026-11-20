"""Сборщики кураторских документов знаний (S06 Task 6) — источники для индексации.

Канон (ADR-003 §4): содержимое — кураторское, автонаполнение «успешным по
трейсу» запрещено. Источники (hw-plan §3):
1. ddl — миграция 0001 (CREATE TABLE с inline-комментариями: PG-каталог
   inline-комменты не хранит, файл миграции — канон DDL; офлайн-тул репозитория);
2. relationship — вьюха agent_relationships_active (meaning/usage_notes);
   привязка — left_table (строже общего документа: обе таблицы ребра в одном
   фильтре прав схема не даёт — ограничение V2, exposure не больше уже
   принятого статического DDL dev-контура, ruling ARCH-GATE S06 W2);
3. glossary — docs/glossary.md (термин → определение);
4. few_shot — docs/few_shot.md («вопрос → SQL»).

Каждый чанк — с префиксом-заголовком («[таблица: orders] …») — сигнал для
эмбеддера и якорь имени таблицы/термина (ACCURACY §6.2).
"""

import re
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from text_to_sql_agent.application.ports.vector_store import DocKind, KnowledgeDocument

# 7 бизнес-таблиц демо-схемы (миграция 0001); agent_* — служебные, не индексируются
BUSINESS_TABLES = (
    "customers",
    "campaigns",
    "products",
    "orders",
    "order_items",
    "employees",
    "bonuses",
)

# Блок CREATE TABLE в SCHEMA_SQL миграции 0001: от заголовка до `\n);`
# (DEFAULT now() внутри строки не мешает — конец блока всегда на своей строке)
_CREATE_TABLE_RE = re.compile(r"CREATE TABLE (\w+) \(\n(.*?)\n\);", re.DOTALL)

# Заголовок раздела glossary/few_shot: `## [термин: X] — таблица Y` / `## [few-shot: X] — таблица Y`
_SECTION_RE = re.compile(r"^## \[(?:термин|few-shot): ([^\]]+)\] — таблица (\S+)\s*$", re.MULTILINE)


def _doc(
    kind: DocKind,
    content: str,
    *,
    table_name: str | None,
    source_ref: str,
    model_version: str,
) -> KnowledgeDocument:
    return KnowledgeDocument(
        doc_kind=kind,
        table_name=table_name,
        content=content,
        model_version=model_version,
        source_ref=source_ref,
    )


def ddl_documents(migration_path: Path, *, model_version: str) -> list[KnowledgeDocument]:
    """DDL бизнес-таблиц с комментариями колонок — из файла миграции 0001."""
    sql = migration_path.read_text(encoding="utf-8")
    docs = []
    for table, body in _CREATE_TABLE_RE.findall(sql):
        if table not in BUSINESS_TABLES:
            continue
        content = (
            f"[таблица: {table}] DDL демо-схемы (PostgreSQL):\nCREATE TABLE {table} (\n{body}\n);"
        )
        docs.append(
            _doc(
                DocKind.DDL,
                content,
                table_name=table,
                source_ref=f"ddl:{table}",
                model_version=model_version,
            )
        )
    missing = set(BUSINESS_TABLES) - {d.table_name for d in docs}
    if missing:
        raise ValueError(
            f"миграция {migration_path.name}: не найдены CREATE TABLE для {sorted(missing)}"
        )
    return docs


def relationship_documents(dsn: str, *, model_version: str) -> list[KnowledgeDocument]:
    """Связи с семантикой — из вьюхи agent_relationships_active (active-рёбра)."""
    query = (
        "SELECT edge_id, left_table, right_table, join_type, cardinality, meaning, usage_notes "
        "FROM agent_relationships_active ORDER BY edge_id"
    )
    docs = []
    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=10) as conn:
        rows = conn.execute(query).fetchall()
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
                table_name=row["left_table"],  # см. docstring: привязка — left_table
                source_ref=f"rel:{row['edge_id']}",
                model_version=model_version,
            )
        )
    return docs


def markdown_documents(
    path: Path, *, ref_prefix: str, model_version: str
) -> list[KnowledgeDocument]:
    """Разделы `## [термин|few-shot: имя] — таблица T` кураторского markdown-файла.

    Заголовок остаётся в чанке (якорь термина/примера); `null` в позиции
    таблицы → общий документ (table_name=None).
    """
    text = path.read_text(encoding="utf-8")
    matches = list(_SECTION_RE.finditer(text))
    if not matches:
        raise ValueError(f"{path.name}: не найдено ни одного раздела '## [термин|few-shot: …]'")
    docs = []
    for i, match in enumerate(matches):
        name, table = match.group(1), match.group(2)
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        content = text[match.start() : body_end].strip()
        docs.append(
            _doc(
                DocKind.FEW_SHOT if ref_prefix == "few_shot" else DocKind.GLOSSARY,
                content,
                table_name=None if table == "null" else table,
                source_ref=f"{ref_prefix}:{name}",
                model_version=model_version,
            )
        )
    return docs

"""JOIN-скелет запроса — доменная сущность + детерминированная сборка кодом (feat-010).

«Код строит джойны, модель пишет остальное» (hw-01 §2.1): скелет — НЕИЗМЕНЯЕМЫЙ
текст FROM/JOIN/ON + обязательные предикаты из рёбер JoinPlan; модель наполняет
только SELECT-список/WHERE/GROUP BY/ORDER BY (ruling R1 ARCH-GATE S10 — тул с
4 полями, финальный SQL собирает ``assemble_sql``). «Галлюцинация связи исчезает
как класс» (ADR-007 §4.2): модель физически не пишет джойны.

``skeleton_hash`` — вход валидатора S11 («скелет не изменён», hw-01 §2.1):
sha256 канонизированной неизменяемой части (``skeleton_digest`` — именованная
функция, единый источник нормализации для build_skeleton и S11 — ruling R3
ARCH-GATE S10: collapse-whitespace + lowercase + сортировка предикатов).

Без IO, без LLM (границы domain): вход — валидированный JoinPlan
(domain/join_plan.py), выход — чистые VO и строки SQL-текста.
"""

import hashlib
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from text_to_sql_agent.domain.join_plan import JoinPlan, JoinPlanEdge


class Skeleton(BaseModel):
    """Скелет запроса: неизменяемая часть SQL + дайджест для валидатора S11.

    ``join_sections`` — готовые строки JOIN (тип/направление/ключи — из рёбер
    плана; модель не трогает); ``mandatory_predicates`` — готовые SQL-предикаты
    ребра (ADR-006 §4.6: MANDT/lang/deleted=false — якоря в WHERE); они уже
    войдут в собираемый SQL — модель их не повторяет. ``skeleton_hash`` —
    дайджест неизменяемой части (без SELECT-плейсхолдеров — ruling R3).
    """

    model_config = ConfigDict(frozen=True)

    tables: tuple[str, ...] = Field(min_length=2)  # порядок FROM/JOIN (первая — FROM)
    from_table: str = Field(min_length=1)
    join_sections: tuple[str, ...]
    mandatory_predicates: tuple[str, ...] = Field(default_factory=())
    skeleton_hash: str = Field(min_length=1)


def build_skeleton(plan: JoinPlan) -> Skeleton:
    """Собрать скелет из JoinPlan (детерминизм: одинаковый план → одинаковый скелет).

    JOIN-секции — по рёбрам плана в порядке обхода пути (первая таблица плана —
    FROM); ключи следуют своей таблице (ruling Q2 S09: from_columns принадлежат
    from_table), составные ключи — через AND. Обязательные предикаты ребра
    рендерятся по контракту реестра: ключ ``"таблица.колонка"`` — на указанную
    таблицу; ключ без квалификации (``"mandt"``) — SAP-ключ, на ОБЕ таблицы
    ребра (ADR-006 §4.7: MANDT/языковой ключ фильтруют каждую сторону).
    """
    if plan.tables[0] != plan.edges[0].from_table and plan.tables[0] != plan.edges[0].to_table:
        # Дрейф плана: первая таблица не задействована в первом ребре — скелет
        # не соберётся; план собирает код (S09), это дефект сборки, fail-closed
        raise ValueError(
            f"первая таблица плана {plan.tables[0]} не задействована в первом ребре "
            f"{plan.edges[0].rel_id} — скелет не может быть построен"
        )
    join_sections: list[str] = []
    for edge in plan.edges:
        join_sections.append(_join_section(edge))
    predicates: list[str] = []
    for rel_id, raw in plan.mandatory_predicates.items():
        edge = _edge_by_rel(plan, rel_id)
        predicates.extend(_edge_predicates(edge, raw))
    return Skeleton(
        tables=tuple(plan.tables),
        from_table=plan.tables[0],
        join_sections=tuple(join_sections),
        mandatory_predicates=tuple(predicates),
        skeleton_hash=skeleton_digest(join_sections, predicates),
    )


def assemble_sql(
    skeleton: Skeleton,
    *,
    select_list: str,
    where: str | None = None,
    group_by: str | None = None,
    order_by: str | None = None,
) -> str:
    """Финальный SELECT: скелет кода + наполнение модели (ruling R1 ARCH-GATE S10).

    Модельные поля — только 4 (select_list/where/group_by/order_by); JOIN-секции
    и обязательные предикаты входят из скелета дословно. Пользовательский WHERE
    берётся в скобки (модель может вернуть OR — не должен разорвать обязательные
    предикаты). LIMIT к внешнему запросу добавляет validate_sql (S11) — здесь
    честная сырая сборка.
    """
    lines: list[str] = [f"SELECT {select_list.strip()}", f"FROM {skeleton.from_table}"]
    lines.extend(skeleton.join_sections)
    predicates = list(skeleton.mandatory_predicates)
    user_where = (where or "").strip()
    if user_where:
        predicates.append(f"({user_where})")
    if predicates:
        lines.append("WHERE " + " AND ".join(predicates))
    group = (group_by or "").strip()
    if group:
        lines.append(f"GROUP BY {group}")
    order = (order_by or "").strip()
    if order:
        lines.append(f"ORDER BY {order}")
    return "\n".join(lines)


def skeleton_digest(join_sections: Sequence[str], mandatory_predicates: Sequence[str]) -> str:
    """Дайджест неизменяемой части скелета (ruling R3 ARCH-GATE S10).

    Нормализация: collapse-whitespace + lowercase; предикаты сортируются (dict
    реестра не упорядочен — порядок не должен менять хэш). Единый источник для
    build_skeleton (запись) и validate_sql S11 (сверка «скелет не изменён»):
    сравнение сойдётся только при общей нормализации (Ловушка 4 S10 — ловить
    реальные изменения, не фантомы пробелов/регистра).
    """

    def normalize(text: str) -> str:
        return " ".join(text.lower().split())

    payload = "\n".join(
        [
            *(normalize(section) for section in join_sections),
            *(normalize(predicate) for predicate in sorted(mandatory_predicates)),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _join_section(edge: JoinPlanEdge) -> str:
    """Готовая JOIN-строка ребра: тип/направление/ключи — из плана, не из модели."""
    conditions = " AND ".join(
        f"{edge.from_table}.{from_col} {edge.join_operator} {edge.to_table}.{to_col}"
        for from_col, to_col in zip(edge.from_columns, edge.to_columns, strict=True)
    )
    return f"{edge.join_type.upper()} JOIN {edge.to_table} ON {conditions}"


def _edge_by_rel(plan: JoinPlan, rel_id: str) -> JoinPlanEdge:
    edge = next((e for e in plan.edges if e.rel_id == rel_id), None)
    if edge is None:
        # Дрейф реестра: предикат привязан к ребру, которого нет в плане —
        # рендерить не к чему (fail-closed, честный сбой сборки)
        raise ValueError(f"mandatory_predicates привязаны к ребру {rel_id}, отсутствующему в плане")
    return edge


def _render_value(value: Any) -> str:
    """SQL-литерал значения предиката: только скаляры (fail-closed на прочих)."""
    if value is None:
        return "IS NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return f"'{value.replace(chr(39), chr(39) * 2)}'"
    raise ValueError(
        f"значение обязательного предиката {value!r} — не скаляр; реестр хранит "
        "только скалярные константы (строка/число/bool/NULL)"
    )


def _edge_predicates(edge: JoinPlanEdge, raw: dict[str, Any]) -> list[str]:
    """Предикаты ребра: qualified «таблица.колонка» → та таблица; без квалификации
    → обе таблицы ребра (SAP MANDT/языковой ключ — ADR-006 §4.7)."""

    def render(table: str, column: str, value: Any) -> str:
        rendered = _render_value(value)
        if value is None:
            return f"{table}.{column} {rendered}"
        return f"{table}.{column} = {rendered}"

    predicates: list[str] = []
    for key, value in raw.items():
        if "." in key:
            table, column = key.split(".", 1)
            if table not in (edge.from_table, edge.to_table):
                raise ValueError(
                    f"предикат {key!r} ссылается на таблицу вне ребра {edge.rel_id} "
                    f"({edge.from_table}/{edge.to_table})"
                )
            predicates.append(render(table, column, value))
        else:
            predicates.extend(
                render(table, key, value) for table in (edge.from_table, edge.to_table)
            )
    return predicates

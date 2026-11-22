"""JOIN-скелет запроса — доменная сущность + детерминированная сборка кодом (feat-010).

«Код строит джойны, модель пишет остальное» (hw-01 §2.1): скелет — НЕИЗМЕНЯЕМЫЙ
текст FROM/JOIN/ON + обязательные предикаты из рёбер JoinPlan; модель наполняет
только SELECT-список/WHERE/GROUP BY/ORDER BY (ruling R1 ARCH-GATE S10 — тул с
4 полями, финальный SQL собирает ``assemble_sql``). «Галлюцинация связи исчезает
как класс» (ADR-007 §4.2): модель физически не пишет джойны.

Текст модели — НЕ доверенный (AGENTS.md): лексический барьер ``validate_fill_text``
(комментарии, точка с запятой, дисбаланс скобок) стоит в assemble_sql ДО сборки —
наполнение не может «закрыть» скелет комментарием или вынести обязательные
предикаты из-под AND несбалансированной скобкой (ревью S10 C1). Структурная
проверка (sqlglot AST: без Join/подзапросов/Union) — граница сервиса; полная
валидация — S11.

``skeleton_hash`` — вход валидатора S11 («скелет не изменён», hw-01 §2.1):
sha256 канонизированной неизменяемой части (``skeleton_digest`` — именованная
функция, единый источник нормализации для build_skeleton и S11 — ruling R3
ARCH-GATE S10: collapse-whitespace + регистр вне строковых литералов + сортировка
предикатов ПОСЛЕ нормализации — Ловушка 4: ловит реальные изменения, не фантомы).

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

    JOIN-секции — по рёбрам плана в порядке обхода пути: первая таблица плана —
    FROM, каждое следующее ребро начинается с уже введённой таблицы (ревью S10
    I1: иначе дубль FROM или два JOIN одной таблицы — fail-closed на дрейф
    плана; план собирает код S09, но в state/чекпоинте он dict). Ключи следуют
    своей таблице (ruling Q2 S09: from_columns принадлежат from_table),
    составные ключи — через AND. Обязательные предикаты ребра рендерятся по
    контракту реестра: ключ ``"таблица.колонка"`` — на указанную таблицу; ключ
    без квалификации (``"mandt"``) — SAP-ключ, на ОБЕ таблицы ребра
    (ADR-006 §4.7: MANDT/языковой ключ фильтруют каждую сторону).
    """
    if plan.edges[0].from_table != plan.tables[0]:
        raise ValueError(
            f"первая таблица плана {plan.tables[0]} не совпадает с началом первого "
            f"ребра {plan.edges[0].from_table} ({plan.edges[0].rel_id}) — "
            "скелет не может быть построен"
        )
    introduced: set[str] = {plan.tables[0]}
    join_sections: list[str] = []
    for edge in plan.edges:
        if edge.from_table not in introduced:
            raise ValueError(
                f"ребро {edge.rel_id} начинается с {edge.from_table}, не введённой "
                "в цепочку джойнов — план не является цепочкой (дрейф сборки плана)"
            )
        if edge.to_table in introduced:
            # Ревью S10 re-review minor: повторный ввод таблицы (цикл a→b, b→a
            # или параллельное a→b) — дублирующий JOIN и невалидный SQL вместо
            # честного ValueError; легитимный путь S09 таблицы не повторяет
            raise ValueError(
                f"ребро {edge.rel_id} повторно вводит таблицу {edge.to_table} — "
                "план не является цепочкой джойнов (дрейф сборки плана)"
            )
        introduced.add(edge.to_table)
        join_sections.append(_join_section(edge))
    predicates: list[str] = []
    for rel_id, raw in plan.mandatory_predicates.items():
        edge = _edge_by_rel(plan, rel_id)
        predicates.extend(_edge_predicates(edge, raw))
    # Дедуп: неквалифицированный предикат (mandt) рендерится на ОБЕ таблицы
    # КАЖДОГО ребра — общая таблица двух рёбер получила бы дубль (ревью S10,
    # minor). dict.fromkeys — дедуп с сохранением детерминированного порядка.
    # Ревью S10 re-review I-r2: дайджест считается по ТОМУ ЖЕ дедуплицированному
    # списку, что хранится в Skeleton — иначе S11-сверка скелета давала бы
    # фантом «скелет изменён» на любом многотабличном пути
    deduped = tuple(dict.fromkeys(predicates))
    return Skeleton(
        tables=tuple(plan.tables),
        from_table=plan.tables[0],
        join_sections=tuple(join_sections),
        mandatory_predicates=deduped,
        skeleton_hash=skeleton_digest(join_sections, deduped),
    )


def validate_fill_text(text: str, *, field: str) -> None:
    """Лексический барьер наполнения модели (ревью S10 C1): текст — НЕ доверенный.

    Запрещено: комментарии ``--``/``/*`` (закрывают скелет или обязательные
    предикаты), ``;`` (вторая инструкция), дисбаланс скобок (вынос обязательных
    предикатов из-под AND: ``... AND (a=1) OR (1=1``). Барьер — в DOMAIN:
    assemble_sql не «заряжен» ни для одного вызывающего; структурная проверка
    (sqlglot: без Join/подзапросов) — граница сервиса, полная — S11.
    """
    if not text:
        return
    if "--" in text or "/*" in text or "*/" in text:
        raise ValueError(
            f"поле {field} содержит комментарий — структура SQL фиксирована скелетом, "
            "наполнение не может менять или прятать её"
        )
    if ";" in text:
        raise ValueError(f"поле {field} содержит «;» — разрешён один SELECT, вторых инструкций нет")
    depth = 0
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                break
    if depth != 0:
        raise ValueError(
            f"поле {field}: скобки не сбалансированы — наполнение не может "
            "изменять структуру WHERE/группировок скелета"
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
    честная сырая сборка. Каждое поле проходит лексический барьер
    ``validate_fill_text`` (текст модели — не доверенный, ревью S10 C1).
    """
    fields = {
        "select_list": select_list,
        "where": where or "",
        "group_by": group_by or "",
        "order_by": order_by or "",
    }
    for field, text in fields.items():
        validate_fill_text(text.strip(), field=field)
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

    Нормализация: collapse-whitespace + lowercase ВНЕ строковых литералов
    (ревью S10 I2: ``'EN'`` и ``'en'`` — разные значения, кавычки внутри SQL
    сохраняют регистр); предикаты сортируются ПОСЛЕ нормализации (порядок dict
    реестра и регистр не должны менять хэш — фантомы S11). Единый источник для
    build_skeleton (запись) и validate_sql S11 (сверка «скелет не изменён»;
    сверка ведётся с state.skeleton — сам SQL проверяется структурно по AST,
    неизменяемая часть в собранном SQL не выделяется без эвристик).
    """
    payload = "\n".join(
        [
            *(_normalize_fragment(section) for section in join_sections),
            *sorted(_normalize_fragment(predicate) for predicate in mandatory_predicates),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize_fragment(text: str) -> str:
    """collapse-whitespace + lowercase вне одинарных кавычек (литералы святы).

    Ревью S10 re-review minor: кавычки разрезают ДО collapse-whitespace —
    пробельные изменения ВНУТРИ литерала ('a  b' vs 'a b') не нормализуются,
    «литералы святы» полно, а не только по регистру.
    """
    parts = text.split("'")
    return "'".join(
        " ".join(part.lower().split()) if index % 2 == 0 else part
        for index, part in enumerate(parts)
    )


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

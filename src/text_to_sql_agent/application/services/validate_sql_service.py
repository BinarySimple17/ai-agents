"""Сервис validate_sql — sqlglot-парсинг и обход AST → нейтральный IR (feat-011, S11).

Разделение по ruling R4 ARCH-GATE: ЗДЕСЬ — механика (парсинг postgres, ровно одна
инструкция, whitelist типов узлов fail-closed, извлечение IR, подстановки текста);
ПОЛИТИКА (функции/LIMIT/ACL/скелет) — ``domain.sql_validation``. Форм-отказ
парсинга/обхода — вердикт hallucination (синтаксис не существует в мире агента —
retry оправдан, ADR-005 §4).

Whitelist узлов — exact-match по типу (ADR-005 §4 п.1: «узел вне whitelist'а →
отказ, а не „не проверили → пропустили"»; sqlglot богаче любого списка —
fail-closed превращает неполноту из молчаливой утечки в явный отказ). Строковые
литералы остаются литералами (S6 hw-01): sqlglot не парсит их содержимое —
вторые инструкции/SET внутри ``'...'`` не образуют узлов AST.

Обход вложенности: CTE/UNION/подзапросы/EXISTS/LATERAL/производные таблицы —
каждый скоуп обрабатывается теми же правилами; резолюция алиасов идёт по
скоуп-цепочке (корреляция подзапросов), граница корреляции — тело CTE (семантика
PostgreSQL: WITH не видит внешние алиасы). Видимость CTE — лексическая, по
позиции каждой ссылки (``_visible_ctes``: предыдущие элементы объемлющего WITH +
все CTE внешних WITH; своё/последующие имена не видны) — глобальное множество
имён позволяло бы CTE-тенью скрыть реальную таблицу от ACL (ревью S11 C2).

Подстановки текста при ok-вердикте: внешний LIMIT (ruling R1 ARCH-GATE) и
разворот литеральных звёзд проекции в разрешённые колонки (ruling R2) —
значения решает domain, механику AST выполняет здесь. Звезда, спрятанная в
выражении проекции (скобки/алиас/CAST), — форм-отказ: литеральная звезда не
доходит до БД (ревью S11 C1; COUNT(*) — агрегат, безопасен).
"""

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass

import sqlglot
from pydantic import BaseModel, ConfigDict
from sqlglot import exp
from sqlglot.errors import ParseError as SqlParseError
from sqlglot.errors import TokenError as SqlTokenError

from text_to_sql_agent.domain.join_plan import JoinPlan
from text_to_sql_agent.domain.skeleton import Skeleton, normalize_sql_fragment
from text_to_sql_agent.domain.sql_validation import (
    SqlAudit,
    SqlColumn,
    SqlJoin,
    SqlSource,
    ValidationOutcome,
    ValidationVerdict,
    star_expansion_columns,
    validate_sql,
)


class SqlAuditError(Exception):
    """Форм-отказ парсинга/обхода (не политика прав): вердикт — hallucination."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


# Корень: один SELECT или set-операция (UNION/EXCEPT/INTERSECT — behavior «тела
# проверяются теми же правилами»; несоответствие скелету ловит проверка Task 3,
# содержимое веток классифицируется до вердикта о структуре).
_ROOT_TYPES: tuple[type[exp.Expression], ...] = (exp.Select, exp.Union, exp.Except, exp.Intersect)

# Whitelist типов узлов — exact-match (ADR-005 §4 п.1). Держится минимальным:
# расширение — осознанное решение с тестом на adversarial-корпусе (§4 п.8).
# DML/DDL (Insert/Update/Delete/Merge/Create/Drop/Alter/Truncate), служебные
# инструкции (Command: SET/COPY/CALL/DO), блокировки (Lock: FOR UPDATE) и прочее
# не входят ПО ПОСТРОЕНИЮ — fail-closed на всём незнакомом. INTERVAL — безопасный
# литерал типового фильтра «за последние N дней» (ревью S11 I4: расширение —
# осознанное, с тестом).
_NODE_WHITELIST: frozenset[type[exp.Expression]] = frozenset(
    {
        exp.Select,
        exp.From,
        exp.Join,
        exp.Where,
        exp.Group,
        exp.Having,
        exp.Order,
        exp.Ordered,
        exp.Limit,
        exp.Offset,
        exp.Column,
        exp.Identifier,
        exp.Literal,
        exp.Boolean,
        exp.Null,
        exp.Star,
        exp.Table,
        exp.TableAlias,
        exp.Alias,
        exp.CTE,
        exp.With,
        exp.Subquery,
        exp.Lateral,
        exp.Var,
        exp.Paren,
        exp.Union,
        exp.Except,
        exp.Intersect,
        exp.Exists,
        exp.In,
        exp.Between,
        exp.Like,
        exp.ILike,
        exp.Is,
        exp.Not,
        exp.And,
        exp.Or,
        exp.EQ,
        exp.NEQ,
        exp.GT,
        exp.GTE,
        exp.LT,
        exp.LTE,
        exp.Add,
        exp.Sub,
        exp.Mul,
        exp.Div,
        exp.Mod,
        exp.DPipe,
        exp.Neg,
        exp.Cast,
        exp.DataType,
        exp.Case,
        exp.If,
        exp.Window,
        exp.Distinct,
        exp.Tuple,
        exp.GroupingSets,
        exp.Rollup,
        exp.Cube,
        exp.Count,
        exp.Sum,
        exp.Avg,
        exp.Min,
        exp.Max,
        exp.Coalesce,
        exp.Nullif,
        exp.Lower,
        exp.Upper,
        exp.Length,
        exp.Abs,
        exp.Round,
        exp.TimestampTrunc,
        exp.DateTrunc,
        exp.Anonymous,
        exp.Array,
        exp.Any,
        exp.Interval,
        exp.Extract,
        exp.CurrentDate,
        exp.CurrentTimestamp,
    }
)

# Структурные наследники exp.Func, которые функциями по смыслу не являются
# (имя попало бы в allow-list-проверку ложно): CASE/WHEN, EXISTS, конструктор
# массива, логические коннекторы (And/Or — наследники Func в sqlglot 30.x).
_STRUCTURAL_FUNCS: tuple[type[exp.Expression], ...] = (
    exp.Case,
    exp.If,
    exp.Array,
    exp.Exists,
    exp.And,
    exp.Or,
)

# Маркер пары ON, которую извлекатель не свёл к равенству колонок (OR, литералы,
# USING) — сверка со скелетом равенством даст mismatch: ON фиксирован скелетом.
_COMPLEX_ON = ("__complex_on__", "__complex_on__")


@dataclass(frozen=True, slots=True)
class _Source:
    """Источник скоупа: ``ref`` — как на него ссылаются в SQL, ``name`` — bare-имя
    сущности для проверки прав (таблица/CTE; производная таблица — её алиас)."""

    ref: str
    name: str
    kind: str  # "table" | "cte" | "sub"


def build_sql_audit(sql: str) -> SqlAudit:
    """IR запроса (публичная точка property-сверки с парсером — ruling R3, Task 4)."""
    return _build_audit(_parse_tree(sql))


def _parse_tree(sql: str) -> exp.Expression:
    """Парсинг postgres + форма: одна инструкция, корень SELECT, whitelist узлов."""
    try:
        statements = sqlglot.parse(sql, read="postgres")
    except (SqlParseError, SqlTokenError) as exc:
        # TokenError не наследует ParseError (незакрытые кавычки/комментарии) —
        # тот же исход (прецедент S10 C1-регресс)
        raise SqlAuditError(f"SQL не парсится (postgres): {type(exc).__name__}") from None
    if len(statements) != 1 or statements[0] is None:
        raise SqlAuditError("разрешён ровно один SELECT-запрос — вторых инструкций нет")
    tree = statements[0]
    if not isinstance(tree, _ROOT_TYPES):
        raise SqlAuditError("корень запроса — только SELECT (DML/DDL/SET/COPY/CALL/DO отвергаются)")
    for node in tree.walk():
        if type(node) not in _NODE_WHITELIST:
            raise SqlAuditError(
                f"конструкция {type(node).__name__} вне whitelist'а валидатора — "
                "deny-by-default: неизвестное не пропускается"
            )
    return tree


def _visible_ctes(node: exp.Expression) -> frozenset[str]:
    """CTE-имена, видимые из позиции узла (лексическая семантика PostgreSQL,
    нерекурсивный WITH; ревью S11 C2): предыдущие элементы объемлющего WITH +
    все CTE внешних WITH. Своё и последующие имена не видны — там то же имя
    резолвится в реальную таблицу, и её права проверяются честно."""
    visible: set[str] = set()
    skip_with: exp.With | None = None
    current = node
    while current.parent is not None:
        parent = current.parent
        if isinstance(parent, exp.CTE):
            # узел внутри тела CTE: видны только ПРЕДЫДУЩИЕ sibling-CTE
            with_node = parent.parent
            if isinstance(with_node, exp.With):
                for cte in with_node.expressions:
                    if cte is parent:
                        break
                    visible.add(cte.alias_or_name.lower())
                skip_with = with_node
        else:
            with_arg = parent.args.get("with_")
            if isinstance(with_arg, exp.With):
                if with_arg is not skip_with:
                    visible.update(cte.alias_or_name.lower() for cte in with_arg.expressions)
                skip_with = None  # уровень WITH отработан — дальше добавляем все
        current = parent
    return frozenset(visible)


def _build_audit(tree: exp.Expression) -> SqlAudit:
    """Извлечение IR из дерева (без политики — решения принимает domain)."""
    root_select = tree if type(tree) is exp.Select else None

    scopes = list(tree.find_all(exp.Select))
    sources_by_scope = {id(s): _scope_sources(s) for s in scopes}
    maps_by_scope = {id(s): _scope_maps(s) for s in scopes}

    columns: list[SqlColumn] = []
    seen_columns: set[tuple[str, tuple[SqlSource, ...], bool]] = set()
    for scope in scopes:
        local_sources = sources_by_scope[id(scope)]
        local_refs = {src.ref for src in local_sources}
        output_aliases = {
            expr.alias.lower()
            for expr in scope.args.get("expressions") or []
            if isinstance(expr, exp.Alias) and expr.alias
        }
        order_node = scope.args.get("order")
        # ревью волна-2 (Critical, регрессия I3-фикса bf518b7): пропуск — только
        # НЕПОСРЕДСТВЕННО упорядоченное голое unqualified имя (Ordered.this);
        # unqualified имя ВНУТРИ выражения ORDER BY PG резолвит в input-колонку —
        # она обязана пройти ACL (утечка видна через порядок строк)
        skip_ids: set[int] = set()
        if order_node is not None:
            for ordered in order_node.expressions:
                expr = ordered.this
                if (
                    isinstance(expr, exp.Column)
                    and not expr.table
                    and expr.name.lower() in output_aliases
                ):
                    skip_ids.add(id(expr))
        for node in _local_nodes(scope):
            if not isinstance(node, exp.Column):
                continue
            if isinstance(node.this, exp.Star):
                continue  # t.* — проекция звёздами, собирается в star_sources
            if id(node) in skip_ids:
                # ревью S11 I3: ORDER BY по output-алиасу — не ссылка на колонку
                # БД (приоритет имени вывода в PostgreSQL); GROUP BY остаётся
                # fail-closed (там приоритет у input-колонки)
                continue
            candidates = _resolve_candidates(node, maps_by_scope[id(scope)], local_sources)
            sources = tuple(SqlSource(name=s.name, kind=s.kind) for s in candidates)
            # ревью S11 C1 + волна-2 (Important): unqualified-ссылка, совпавшая
            # с алиасом источника СВОЕГО скоупа или объемлющего, — whole-row
            # reference (PG: колонок с таким именем нет → вся строка)
            name_lower = node.name.lower()
            whole_row = False
            if not node.table:
                if name_lower in local_refs:
                    whole_row = True
                else:
                    outer = _resolve_outer_ref(name_lower, maps_by_scope[id(scope)])
                    if outer is not None:
                        whole_row = True
                        sources = (SqlSource(name=outer.name, kind=outer.kind),)
            key = (name_lower, sources, whole_row)
            if key not in seen_columns:
                seen_columns.add(key)
                columns.append(
                    SqlColumn(name=node.name.lower(), candidates=sources, whole_row=whole_row)
                )

    star_sources: list[tuple[SqlSource, ...]] = []
    for scope in scopes:
        for _, sources in _projection_stars(
            scope, sources_by_scope[id(scope)], maps_by_scope[id(scope)]
        ):
            star_sources.append(tuple(SqlSource(name=s.name, kind=s.kind) for s in sources))

    root_maps = maps_by_scope.get(id(root_select), []) if root_select is not None else []
    return SqlAudit(
        from_table=_from_table(root_select),
        tables=_collect_tables(tree),
        columns=tuple(columns),
        joins=_collect_joins(root_select, maps_by_scope),
        where_conjuncts=_collect_where(root_select, root_maps),
        outer_limit=_collect_outer_limit(tree),
        functions=_collect_functions(tree),
        star_sources=tuple(star_sources),
    )


def _resolve_outer_ref(name: str, maps: list[dict[str, _Source]]) -> _Source | None:
    """Поиск ref'а источника в ВНЕШНИХ скоупах (внутренний→внешний, локальный
    исключён) — correlated whole-row (ревью S11 волна-2)."""
    for mapping in maps[1:]:
        source = mapping.get(name)
        if source is not None:
            return source
    return None


def _visible_ctes_for(expr: exp.Expression) -> frozenset[str]:
    """Тонкая обёртка для читаемости вызовов извлекателей (позиция = сам узел)."""
    return _visible_ctes(expr)


def _expr_source(expr: exp.Expression, cte_names: frozenset[str]) -> _Source:
    """Источник FROM/JOIN: таблица (или ссылка на CTE), производная таблица, LATERAL."""
    if isinstance(expr, exp.Table):
        if not isinstance(expr.this, exp.Identifier) or not expr.name:
            # ревью S11 I2: табличная функция (Table.this — вызов, name пуст) —
            # форм-отказ, не падение VO ((SqlSource min_length) → ValidationError)
            raise SqlAuditError(
                "табличная функция в FROM/JOIN не поддерживается валидатором — "
                "deny-by-default: источником может быть только именованная таблица/"
                "CTE/производная таблица с алиасом"
            )
        name = expr.name.lower()
        if not expr.db and name in cte_names:
            return _Source(ref=(expr.alias or name).lower(), name=name, kind="cte")
        return _Source(ref=(expr.alias or name).lower(), name=name, kind="table")
    if isinstance(expr, (exp.Subquery, exp.Lateral)):
        ref = (expr.alias or "").lower()
        if not ref:
            raise SqlAuditError(
                "производная таблица (подзапрос/LATERAL в FROM/JOIN) требует алиаса"
            )
        return _Source(ref=ref, name=ref, kind="sub")
    raise SqlAuditError(
        f"источник {type(expr).__name__} в FROM/JOIN не поддерживается валидатором — "
        "deny-by-default"
    )


def _scope_sources(scope: exp.Select) -> list[_Source]:
    """Источники скоупа в порядке появления (FROM + JOIN-секции, включая comma-join);
    видимость CTE вычисляется по позиции каждой ссылки (ревью S11 C2)."""
    sources: list[_Source] = []
    from_clause = scope.args.get("from_")
    if from_clause is not None:
        sources.append(_expr_source(from_clause.this, _visible_ctes_for(from_clause.this)))
    for join in scope.args.get("joins") or []:
        sources.append(_expr_source(join.this, _visible_ctes_for(join.this)))
    return sources


def _scope_chain(scope: exp.Select) -> list[exp.Select]:
    """Скоуп-цепочка внутренний→внешний для резолюции алиасов (корреляция).

    Граница корреляции — CTE: тело WITH не видит алиасы внешнего запроса
    (семантика PostgreSQL); подзапросы/EXISTS/LATERAL коррелируют свободно.
    """
    chain = [scope]
    crossed_cte = False
    node = scope.parent
    while node is not None:
        if isinstance(node, exp.CTE):
            crossed_cte = True
        if type(node) is exp.Select and not crossed_cte:
            chain.append(node)
        node = node.parent
    return chain


def _scope_maps(scope: exp.Select) -> list[dict[str, _Source]]:
    """Карты резолюции алиасов по скоуп-цепочке (внутренний → внешний)."""
    return [{src.ref: src for src in _scope_sources(node)} for node in _scope_chain(scope)]


def _reject_expression_stars(item: exp.Expression) -> None:
    """Форм-отказ на звезду, спрятанную в выражении проекции (ревью S11 C1):
    ``(t.*)``, ``t.* AS x``, ``(t.*)::text`` — разворот сложен, а литеральная
    звезда не должна доходить до БД (ACL-байпас). ``COUNT(*)`` — агрегат, колонок
    не раскрывает (ruling R2) — исключение. Звезда вложенного SELECT — забота
    своего скоупа (ревью волна-2: сканер не пересекает границу Select/Subquery,
    иначе поверхность парсера сужена сверх необходимого)."""
    for star in item.find_all(exp.Star):
        parent = star.parent
        under_count = False
        other_scope = False
        while parent is not None:
            if isinstance(parent, exp.Count):
                under_count = True
                break
            if parent is item:
                break
            if isinstance(parent, (exp.Select, exp.Subquery)):
                other_scope = True
                break
            parent = parent.parent
        if not under_count and not other_scope:
            raise SqlAuditError(
                "звезда '*' внутри выражения проекции (скобки/алиас/CAST) "
                "не поддерживается — литеральная звезда не доходит до БД"
            )


def _projection_stars(
    scope: exp.Select,
    sources: list[_Source],
    maps: list[dict[str, _Source]],
) -> Iterator[tuple[exp.Expression, list[_Source]]]:
    """Звёзды проекции скоупа: bare ``*`` — все источники скоупа, ``t.*`` — один.

    Общий источник IR (``_build_audit``) и разворота (``_expand_stars``) — они
    видят ровно одни и те же звёзды. Любая ДРУГАЯ форма с звездой внутри —
    форм-отказ (``_reject_expression_stars``, ревью S11 C1). Bare-звезда без
    источников FROM — форм-отказ (postgres такой SELECT не исполняет).
    """
    for item in scope.args.get("expressions") or []:
        if isinstance(item, exp.Star):
            if not sources:
                raise SqlAuditError("звезда '*' без источников FROM — форма не поддерживается")
            yield item, list(sources)
        elif isinstance(item, exp.Column) and isinstance(item.this, exp.Star):
            yield item, [_resolve_qualifier((item.table or "").lower(), maps)]
        else:
            _reject_expression_stars(item)


def _local_nodes(scope: exp.Select) -> Iterator[exp.Expression]:
    """Узлы скоупа без спуска во вложенные SELECT (CTE/подзапросы/ветки UNION —
    каждый обрабатывается собственным скоупом с теми же правилами)."""
    nested = {id(s) for s in scope.find_all(exp.Select) if s is not scope}
    for node in scope.walk():
        if node is scope:
            continue
        parent = node.parent
        inside_nested = False
        while parent is not None and parent is not scope:
            if id(parent) in nested:
                inside_nested = True
                break
            parent = parent.parent
        if not inside_nested:
            yield node


def _resolve_qualifier(qualifier: str, maps: list[dict[str, _Source]]) -> _Source:
    """Квалификатор колонки → источник (внутренний скоуп → внешний)."""
    for mapping in maps:
        source = mapping.get(qualifier)
        if source is not None:
            return source
    raise SqlAuditError(f"квалификатор {qualifier!r} не резолвится к источнику запроса")


def _resolve_candidates(
    column: exp.Column,
    maps: list[dict[str, _Source]],
    local_sources: list[_Source],
) -> tuple[_Source, ...]:
    """Кандидаты-источники колонки (Ловушка 5): квалифицированная — один,
    unqualified — все источники СВОЕГО скоупа (не «повезёт с дефолтом»)."""
    qualifier = (column.table or "").lower()
    if qualifier:
        return (_resolve_qualifier(qualifier, maps),)
    if not local_sources:
        raise SqlAuditError(f"колонка {column.name!r} без источников FROM/JOIN в скоупе")
    return tuple(local_sources)


def _from_table(root_select: exp.Select | None) -> str:
    """bare-имя источника FROM внешнего запроса ("" у set-операций); ссылка,
    привязанная к CTE или производной таблице, — не «FROM не изменён» (ревью
    S11 C2: сверка скелета сравнивает имя, вид источника обязан быть таблицей)."""
    if root_select is None:
        return ""
    from_clause = root_select.args.get("from_")
    if from_clause is None:
        return ""
    source = _expr_source(from_clause.this, _visible_ctes_for(from_clause.this))
    return source.name if source.kind == "table" else ""


def _collect_tables(tree: exp.Expression) -> tuple[str, ...]:
    """Все relation-ссылки как написано (схема квалифицируется); ссылки на CTE —
    не таблицы мира (их тела проверяются теми же правилами на своём скоупе).
    Видимость CTE — по позиции каждой ссылки (ревью S11 C2)."""
    tables: list[str] = []
    seen: set[str] = set()
    for table in tree.find_all(exp.Table):
        name = table.name.lower()
        if not table.db and name in _visible_ctes_for(table):
            continue
        qualified = f"{table.db.lower()}.{name}" if table.db else name
        if qualified not in seen:
            seen.add(qualified)
            tables.append(qualified)
    return tuple(tables)


def _normalize_join_type(join: exp.Join) -> str:
    """Нормализация типа джойна к токенам скелета (inner/left — реестр ADR-006)."""
    side = (join.side or "").lower()
    kind = (join.kind or "").lower()
    if kind == "comma":
        return "comma"
    if side in ("left", "right", "full"):
        return side
    if not side and kind in ("", "inner"):
        return "inner"
    if kind == "cross":
        return "cross"
    if kind in ("semi", "anti"):
        return kind
    return f"{side}_{kind}" if side else "unknown"


def _on_pairs(join: exp.Join, maps: list[dict[str, _Source]]) -> tuple[tuple[str, str], ...]:
    """Пары колонок ON (каждая сортируется — направление равенства не семантично)."""
    if join.args.get("on") is None:
        return (_COMPLEX_ON,)  # USING/без ON — скелет рендерит только ON-равенства
    pairs: list[tuple[str, str]] = []
    for part in _flatten_and(join.args["on"]):
        if (
            isinstance(part, exp.EQ)
            and isinstance(part.this, exp.Column)
            and isinstance(part.expression, exp.Column)
        ):
            left = _qualified_name(part.this, maps)
            right = _qualified_name(part.expression, maps)
            pairs.append(tuple(sorted((left, right))))
        else:
            return (_COMPLEX_ON,)
    return tuple(sorted(pairs))


def _qualified_name(column: exp.Column, maps: list[dict[str, _Source]]) -> str:
    source = _resolve_qualifier((column.table or "").lower(), maps)
    return f"{source.name}.{column.name.lower()}"


def _collect_joins(
    root_select: exp.Select | None,
    maps_by_scope: dict[int, list[dict[str, _Source]]],
) -> tuple[SqlJoin, ...]:
    """Джойны ВНЕШНЕГО запроса в порядке появления (сверка со скелетом — Task 3)."""
    if root_select is None:
        return ()
    joins: list[SqlJoin] = []
    for join in root_select.args.get("joins") or []:
        source = _expr_source(join.this, _visible_ctes_for(join.this))
        joins.append(
            SqlJoin(
                join_type=_normalize_join_type(join),
                table=source.name,
                on_pairs=_on_pairs(join, maps_by_scope[id(root_select)]),
            )
        )
    return tuple(joins)


def _flatten_and(node: exp.Expression) -> Iterator[exp.Expression]:
    """Плоский список конъюнктов (AND-ассоциативность, скобки прозрачны)."""
    if isinstance(node, exp.Paren):
        yield from _flatten_and(node.this)
    elif isinstance(node, exp.And):
        yield from _flatten_and(node.this)
        yield from _flatten_and(node.expression)
    else:
        yield node


def _resolve_column_tables(node: exp.Expression, maps: list[dict[str, _Source]]) -> exp.Expression:
    """Копия конъюнкта с квалификаторами колонок КОРНЕВОГО скоупа, переписанными
    на реальные имена источников (алиасы SQL → имена таблиц/CTE).
    mandatory_predicates скелета рендерятся реальными именами таблиц, модель
    пишет алиасами — сверка нормализованным равенством без ложных срабатываний
    (дерево не мутируется — исходный SQL остаётся без изменений). Колонки
    вложенных скоупов (подзапросы/EXISTS внутри конъюнкта) не трогаются — они
    резолвятся своими скоуп-мапами при извлечении IR."""
    clone = node.copy()
    nested = {id(s) for s in clone.find_all(exp.Select)}
    for column in clone.find_all(exp.Column):
        parent = column.parent
        inside_nested = False
        while parent is not None and parent is not clone:
            if id(parent) in nested:
                inside_nested = True
                break
            parent = parent.parent
        if inside_nested:
            continue
        qualifier = (column.table or "").lower()
        if not qualifier:
            continue
        column.set("table", exp.to_identifier(_resolve_qualifier(qualifier, maps).name))
    return clone


def _collect_where(
    root_select: exp.Select | None, maps: list[dict[str, _Source]]
) -> tuple[str, ...]:
    """Конъюнкты WHERE внешнего запроса — нормализованные тексты с квалификаторами,
    переписанными на реальные имена источников (сверка mandatory_predicates —
    Task 3; нормализация — единая с S10, ruling R3 S10)."""
    if root_select is None:
        return ()
    where = root_select.args.get("where")
    if where is None:
        return ()
    return tuple(
        normalize_sql_fragment(_resolve_column_tables(part, maps).sql(dialect="postgres"))
        for part in _flatten_and(where.this)
    )


def _collect_outer_limit(root: exp.Expression) -> int | None:
    """LIMIT внешнего запроса (Select или set-операции); внутренние не собираются."""
    limit = root.args.get("limit")
    if limit is None:
        return None
    value = limit.expression
    if not isinstance(value, exp.Literal) or not value.is_int:
        raise SqlAuditError("LIMIT — целочисленный литерал (переменные/выражения отвергаются)")
    return int(value.this)


def _collect_functions(tree: exp.Expression) -> tuple[str, ...]:
    """Имена функций (канонические sqlglot sql_name; Anonymous — исходное имя)."""
    names: set[str] = set()
    for node in tree.find_all(exp.Func):
        if isinstance(node, _STRUCTURAL_FUNCS):
            continue
        if isinstance(node, exp.Anonymous):
            names.add(str(node.this).lower())
        else:
            names.add(node.sql_name().lower())
    return tuple(sorted(names))


def _enforce_limit(tree: exp.Expression, enforced: int) -> bool:
    """Внешний LIMIT итогового SQL (ruling R1): добавить/склампить. True — дерево
    менялось (текст перегенерирован sqlglot), False — исходный текст без изменений."""
    limit = tree.args.get("limit")
    if limit is not None and isinstance(limit.expression, exp.Literal):
        try:
            current = int(limit.expression.this)
        except (TypeError, ValueError):
            current = None
        if current == enforced:
            return False
    tree.set("limit", exp.Limit(expression=exp.Literal.number(str(enforced))))
    return True


def _expand_stars(tree: exp.Expression, permissions: Mapping[str, Sequence[str]]) -> bool:
    """Разворот литеральных звёзд проекции в разрешённые колонки (ruling R2 ARCH-GATE).

    Политика значений — ``domain.star_expansion_columns`` (какие колонки разрешены;
    None — суб-звезда ``ref.*`` остаётся); ЗДЕСЬ механика подстановки в AST:
    элемент проекции заменяется на колонки своих источников скоупа — литеральная
    звезда не доходит до БД (ACL-байпас закрыт). Вызывается только при ok-вердикте
    (``_check_stars`` домена уже одобрил каждую звезду). True — дерево менялось
    (текст перегенерирован), False — звёзд для разворота нет.
    """
    changed = False
    for scope in tree.find_all(exp.Select):
        expressions = scope.args.get("expressions")
        if not expressions:
            continue
        sources = _scope_sources(scope)
        stars = list(_projection_stars(scope, sources, _scope_maps(scope)))
        for item, star_sources in stars:
            replacement: list[exp.Expression] = []
            for source in star_sources:
                columns = star_expansion_columns(source.name, source.kind, permissions)
                if columns is None:
                    replacement.append(
                        exp.Column(this=exp.Star(), table=exp.to_identifier(source.ref))
                    )
                else:
                    replacement.extend(
                        exp.Column(this=exp.to_identifier(c), table=exp.to_identifier(source.ref))
                        for c in columns
                    )
            index = expressions.index(item)
            expressions[index : index + 1] = replacement
            changed = True
    return changed


class ValidateSqlResult(BaseModel):
    """Результат валидации: вердикт + итоговый SQL (в ok — с исполненным LIMIT)."""

    model_config = ConfigDict(frozen=True)

    verdict: ValidationVerdict
    sql: str


class ValidateSqlService:
    """Детерминированная валидация (без LLM/IO): glue sqlglot ↔ domain (ruling R4).

    Входы обязательны (fail-closed wiring): узел FSM передаёт skeleton/join_plan/
    permissions/data_catalog из state; форм-отказ — вердикт hallucination с
    исходным SQL без изменений (ничего не исполняется).
    """

    def validate(
        self,
        sql: str,
        *,
        skeleton: Skeleton,
        join_plan: JoinPlan,
        skeleton_hash: str,
        permissions: Mapping[str, Sequence[str]],
        data_catalog: frozenset[str] | set[str],
        row_limit: int,
    ) -> ValidateSqlResult:
        try:
            tree = _parse_tree(sql)
            audit = _build_audit(tree)
        except SqlAuditError as exc:
            return ValidateSqlResult(
                verdict=ValidationVerdict(
                    outcome=ValidationOutcome.HALLUCINATION,
                    issues=(f"SQL отвергнут по форме: {exc.reason}",),
                    enforced_limit=row_limit,
                ),
                sql=sql,
            )
        verdict = validate_sql(
            audit,
            skeleton=skeleton,
            join_plan=join_plan,
            skeleton_hash=skeleton_hash,
            permissions=permissions,
            data_catalog=data_catalog,
            row_limit=row_limit,
        )
        final_sql = sql
        if verdict.outcome is ValidationOutcome.OK:
            expanded = _expand_stars(tree, permissions)
            limited = _enforce_limit(tree, verdict.enforced_limit)
            if expanded or limited:
                final_sql = tree.sql(dialect="postgres")
        return ValidateSqlResult(verdict=verdict, sql=final_sql)

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
    validate_free_sql,
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

# Сравнения, в которых живёт коррелированная колонка (равенство/неравенство/IN/
# BETWEEN/LIKE/IS) — связь утверждается этим сравнением, не всем скоупом
# (_comparison_local_tables, ревью S31 re-review I3)
_COMPARISON_TYPES: tuple[type[exp.Expression], ...] = (
    exp.EQ,
    exp.NEQ,
    exp.GT,
    exp.GTE,
    exp.LT,
    exp.LTE,
    exp.Is,
    exp.In,
    exp.Between,
    exp.Like,
    exp.ILike,
)


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
    join_pairs: list[tuple[str, str]] = []
    seen_join_pairs: set[tuple[str, str]] = set()
    # корреляция на производном источнике / скоуп без табличных источников
    # (re-review S31 C1) — флаг копится циклом колонок, сливается с JOIN-флагом
    correlation_unresolved = False
    for scope in scopes:
        local_sources = sources_by_scope[id(scope)]
        local_refs = {src.ref for src in local_sources}
        # ревью волна-3 (Critical): семантика PG — quoted идентификатор сохраняет
        # регистр, unquoted складывается в нижний; сопоставление output-алиаса
        # с именем в ORDER BY — только по ЭФФЕКТИВНЫМ именам (иначе quoted
        # mixed-case «Country» + unquoted Country — исполняемый ACL-байпас)
        output_aliases: set[str] = set()
        for expr in scope.args.get("expressions") or []:
            if isinstance(expr, exp.Alias):
                ident = expr.args.get("alias")
                if isinstance(ident, exp.Identifier):
                    output_aliases.add(ident.name if ident.quoted else ident.name.lower())
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
                    and isinstance(expr.this, exp.Identifier)
                    and (expr.this.name if expr.this.quoted else expr.this.name.lower())
                    in output_aliases
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
            # Корреляция (ревью S31 C1 + re-review C1): кандидат-источник ВНЕШНЕГО
            # скоупа (ref не среди локальных) — ссылка на внешний алиас:
            # семантическая связь «внешний источник ↔ таблицы этого скоупа»
            # (WHERE c.id = o.campaign_id в EXISTS утверждает ту же «придуманную
            # связь», что и JOIN) — сверяется с реестром как пара. Обёртка связь
            # НЕ легализует: кандидат-производный источник (CTE/подзапрос внешнего
            # скоупа) — fail-closed unresolved, та же семантика, что у JOIN-
            # операндов (ruling «CTE+CTE разрешена» отозван). Ссылки в тела CTE не
            # тянутся наружу (граница корреляции — CTE, _scope_chain)
            correlated: list[_Source] = [s for s in candidates if s.ref not in local_refs]
            if not node.table:
                if name_lower in local_refs:
                    whole_row = True
                else:
                    outer = _resolve_outer_ref(name_lower, maps_by_scope[id(scope)])
                    if outer is not None:
                        # ревью волна-3 (Important): НЕ замена, а ДОПОЛНЕНИЕ
                        # кандидатов — домен требует '*' на КАЖДОЙ таблице
                        # (schema-blind: права — единственный прокси существования
                        # колонки; при коллизии PG читает ЛОКАЛЬНУЮ колонку)
                        whole_row = True
                        extra = SqlSource(name=outer.name, kind=outer.kind)
                        if all(s != extra for s in sources):
                            sources = (*sources, extra)
                            correlated.append(outer)
            if correlated:
                local_table_names = {s.name for s in local_sources if s.kind == "table"}
                if not local_table_names:
                    # связь с скоупом БЕЗ табличных источников (FROM — CTE/
                    # подзапрос/LATERAL) парой реестра не выражается — fail-closed
                    correlation_unresolved = True
                else:
                    # Пара строится с локальными таблицами ТОГО ЖЕ сравнения
                    # (ревью re-review I3): корреляция утверждает связь с
                    # таблицами своего равенства, не со всеми таблицами скоупа
                    # (иначе локальный JOIN внутри подзапроса даёт ложный отказ).
                    # Нет сравнения/локальных колонок в нём — откат на все
                    # таблицы скоупа (консервативно, без false-positive)
                    predicate_locals = _comparison_local_tables(
                        node, maps_by_scope[id(scope)], local_refs
                    )
                    for src in correlated:
                        if src.kind != "table":
                            correlation_unresolved = True
                            continue
                        for local_name in predicate_locals or local_table_names:
                            pair = tuple(sorted((src.name, local_name)))
                            if pair not in seen_join_pairs:
                                seen_join_pairs.add(pair)
                                join_pairs.append(pair)
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
    unresolved = _collect_join_pairs(
        scopes, sources_by_scope, maps_by_scope, join_pairs, seen_join_pairs
    )
    return SqlAudit(
        from_table=_from_table(root_select),
        tables=_collect_tables(tree),
        columns=tuple(columns),
        joins=_collect_joins(root_select, maps_by_scope),
        where_conjuncts=_collect_where(root_select, root_maps),
        outer_limit=_collect_outer_limit(tree),
        functions=_collect_functions(tree),
        star_sources=tuple(star_sources),
        join_table_pairs=tuple(join_pairs),
        unresolved_join_scopes=unresolved or correlation_unresolved,
    )


def _join_tie_refs(
    join: exp.Join,
    maps: list[dict[str, _Source]],
    joined_ref: str,
    earlier_refs: set[str],
) -> tuple[bool, list[set[str]]]:
    """(Связан ли джойн, ref-множества связывающих равенств ON) — функциональный
    критерий (re-review fix-волн 3–5): связывающее равенство ЗАДЕВАЕТ
    присоединяемый источник (``joined_ref`` — sources[idx+1] для joins[idx]) и
    хотя бы один УЖЕ ПРИСОЕДИНЁННЫЙ (``earlier_refs``) и стоит в конъюнктивном
    скелете ON. Волна 3 (семантика вместо whitelist'а comma/ON TRUE) закрыла
    CROSS JOIN/ON 1=1/ON (TRUE)/самосравнение, но пропускала: равенство ЧУЖИХ
    источников (``JOIN c ON m.id = o.id`` — c не тронут, произведение m×c) и
    равенство под ``OR``/``CASE`` (``ON c.id = o.id OR TRUE`` — тавтология).
    Не считается связью: comma/CROSS JOIN (ON нет), ``ON TRUE``/``ON 1=1``,
    самосравнение (один источник по обе стороны), сравнение со скалярным
    подзапросом (колонки подзапросов — своего скоупа, не резолвятся), равенство
    внутри подзапроса ON, обёртка вне конъюнкции (``_under_conjunctive_skeleton``),
    квалификатор не из скоупа (корреляция). ``JOIN ... USING (колонки)`` — связан
    (равенство по именованным колонкам утверждено), равенств ON нет — пара по
    цепочке соседей. NullSafeEQ (IS NOT DISTINCT FROM) не достижим — отбивается
    whitelist'ом форм раньше (живой прогон). Защитная граница: ``joined_ref``
    вне скоупа (рассинхрон) — связей нет, fail-closed. Ref-множества потребляет
    ``_collect_join_pairs`` — пары из операндов ON (волна 5)."""
    if join.args.get("using"):
        return True, []
    on = join.args.get("on")
    if on is None:
        return False, []
    nested = {id(s) for s in on.find_all(exp.Select)}
    local_map = maps[0]
    tied = False
    tie_refs: list[set[str]] = []
    for eq in on.find_all(exp.EQ):
        if not _under_conjunctive_skeleton(eq, on) or _inside_nested_select(eq, nested):
            continue
        refs = _eq_operand_refs(eq.this, local_map, nested) | _eq_operand_refs(
            eq.expression, local_map, nested
        )
        # связь = равенство ЗАДЕВАЕТ присоединяемый источник и хотя бы один
        # ранее присоединённый (волна 4: «есть EQ двух источников» ≠ «джойн связан»)
        if joined_ref in refs and refs & earlier_refs:
            tied = True
            tie_refs.append(refs)
    return tied, tie_refs


def _inside_nested_select(node: exp.Expression, nested: set[int]) -> bool:
    """Среди предков узла — Select из ``nested`` (подзапросы внутри выражения).
    ``nested`` содержит только Select внутри проверяемого выражения, поэтому
    подъём выше его корня безопасен (переиспользуется _comparison_local_tables)."""
    parent = node.parent
    while parent is not None:
        if id(parent) in nested:
            return True
        parent = parent.parent
    return False


_CONJUNCTIVE_SKELETON = (exp.And, exp.Paren)


def _under_conjunctive_skeleton(node: exp.Expression, root: exp.Expression) -> bool:
    """Равенство стоит в КОНЪЮНКТИВНОМ скелете ON — whitelist предков до корня
    ВКЛЮЧИТЕЛЬНО (re-review fix-волна 5): только And/Paren. Инверсия блочного
    списка волны 4 (Or/Xor/Not/Case/If): список пропускал обёртки вне списка —
    ``ON (c.id = o.id) IS FALSE`` утверждает НЕравенство (связь отрицается),
    ``ON coalesce(c.id = o.id, FALSE)`` — условную связку; whitelist закрывает
    любые обёртки одним правилом. Корень проверяется тоже: ``ON x OR TRUE`` —
    сам ON есть Or (волна 4); равенство-корень (``ON a.id = b.id``) — скелет
    тривиально конъюнктивный. And и Paren прозрачны — конъюнкция."""
    if node is root:
        return True
    parent = node.parent
    while parent is not None:
        if not isinstance(parent, _CONJUNCTIVE_SKELETON):
            return False
        if parent is root:
            return True
        parent = parent.parent
    return False


def _eq_operand_refs(
    operand: exp.Expression, local_map: dict[str, _Source], nested: set[int]
) -> set[str]:
    """Refs (алиасы) ЛОКАЛЬНЫХ источников скоупа среди колонок операнда равенства.
    Колонки вложенных подзапросов и t.* не учитываются; unqualified и квалификатор
    вне скоупа (корреляция) не атрибутируются — консервативно (равенства нет)."""
    refs: set[str] = set()
    for col in operand.find_all(exp.Column):
        if isinstance(col.this, exp.Star) or _inside_nested_select(col, nested):
            continue
        qualifier = (col.table or "").lower()
        if qualifier and qualifier in local_map:
            refs.add(qualifier)
    return refs


def _comparison_local_tables(
    column: exp.Column,
    maps: list[dict[str, _Source]],
    local_refs: set[str],
) -> set[str]:
    """Локальные таблицы сравнения, содержащего коррелированную колонку
    (ревью S31 re-review I3): связь утверждается «внешняя таблица ↔ таблицы
    СВОЕГО равенства», не со всеми таблицами скоупа. Подъём от колонки до
    ближайшего сравнения (And/Or/Not/Case прозрачны); колонки вложенных
    подзапросов внутри сравнения — своего скоупа, не резолвятся; unqualified —
    не атрибутируются (неоднозначно). Пустое множество (нет сравнения или в нём
    нет локальных квалифицированных колонок) — вызывающий откатывается на все
    таблицы скоупа (консервативно: ложных отказов не добавляет, дыр нет)."""
    current: exp.Expression | None = column
    comparison: exp.Expression | None = None
    while current is not None and type(current) is not exp.Select:
        if isinstance(current, _COMPARISON_TYPES):
            comparison = current
            break
        current = current.parent
    if comparison is None:
        return set()
    nested = {id(s) for s in comparison.find_all(exp.Select)}
    tables: set[str] = set()
    for col in comparison.find_all(exp.Column):
        if _inside_nested_select(col, nested) or isinstance(col.this, exp.Star):
            continue
        qualifier = (col.table or "").lower()
        if qualifier and qualifier in local_refs:
            source = maps[0].get(qualifier)
            if source is not None and source.kind == "table":
                tables.add(source.name)
    return tables


def _collect_join_pairs(
    scopes: list[exp.Select],
    sources_by_scope: dict[int, list[_Source]],
    maps_by_scope: dict[int, list[dict[str, _Source]]],
    pairs: list[tuple[str, str]],
    seen: set[tuple[str, str]],
) -> bool:
    """Пары утверждённых связей JOIN по всем скоупам + флаг несверяемых скоупов
    (S31, R1 ARCH-GATE).

    Цепочка скоупа: FROM → JOIN-секции в порядке появления (cross — тот же
    Join-узел); пара — из ОПЕРАНДОВ связывающего равенства ON (присоединяемый
    источник × контрагент; реестр связывает таблицы, не алиасы; re-review
    fix-волна 5): hub-first звезда «FROM факт JOIN изм1 … JOIN изм2 …»
    утверждает факт×изм1 и факт×изм2, а соседства изм1×изм2 в SQL нет — пары из
    соседства цепочки давали ложный отказ легального хаба. Равенство-конъюнкт
    между УЖЕ СВЯЗАННЫМИ таблицами (есть путь по утверждённым парам скоупа) —
    фильтр, новую связь не заявляет (канон волны 4: «обе уже на рёбрах через
    o»). Фолбэк для форм без равенств ON (USING; 2-табличное произведение) —
    соседние операнды цепочки. Произведение без СВЯЗЫВАЮЩЕГО равенства
    (``_join_tie_refs`` — функционально: присоединяемый источник не связан
    равенством колонок с уже присоединёнными в конъюнкции ON; comma / голый
    JOIN / CROSS JOIN / ON TRUE / 1=1 / самосравнение / OR-скелет / равенство
    чужих источников) на ≥3 табличных источниках — ``unresolved``: пары
    произвольны (ревью S31 I1 + re-review fix-волн 3–4); на двух — единственная
    пара цепочки, сверяется. ЛЮБОЙ не-табличный операнд в паре (CTE/производный/
    смешанный) — ``unresolved``: обёртка CTE не делает связь легальной —
    ``WITH x AS (…orders…), y AS (…campaigns…) SELECT * FROM x CROSS JOIN y``
    соединяет свои таблицы без ребра реестра ровно как прямой джойн (ревью S31
    C1, живое воспроизведение; прежний ruling «CTE+CTE — алгебра над телами»
    отозван). FSM-вход поля не читает: структура джойнов там сверяется со
    скелетом плана (_check_skeleton). Пары корреляций (включая fail-closed на
    производном операнде) добавляет цикл колонок в ``_build_audit`` (кандидат
    из внешнего скоупа).
    """
    unresolved = False
    for scope in scopes:
        sources = sources_by_scope[id(scope)]
        maps = maps_by_scope[id(scope)]
        joins = scope.args.get("joins") or []
        has_cartesian = False
        tie_refs_by_idx: dict[int, list[set[str]]] = {}
        for idx, join in enumerate(joins):
            # sources[0] — FROM, joins[i] — sources[i+1] (порядок _scope_sources);
            # защитная граница на рассинхрон — вне диапазона refs не совпадёт
            # ни с одним равенством → джойн несвязан → fail-closed
            joined_ref = sources[idx + 1].ref if idx + 1 < len(sources) else ""
            earlier_refs = {s.ref for s in sources[: idx + 1]}
            if not isinstance(join, exp.Join):
                continue
            tied, tie_refs = _join_tie_refs(join, maps, joined_ref, earlier_refs)
            if not tied:
                has_cartesian = True
                break
            if tie_refs and idx + 1 < len(sources):
                tie_refs_by_idx[idx] = tie_refs
        if has_cartesian and sum(1 for s in sources if s.kind == "table") > 2:
            unresolved = True
            continue
        # пары утверждённых связей скоупа (граф связности — для фильтра
        # равенств-конъюнктов между уже связанными таблицами)
        asserted: dict[str, set[str]] = {}
        prev: _Source | None = None
        for idx, source in enumerate(sources):
            if prev is not None:
                tie_refs = tie_refs_by_idx.get(idx - 1)
                if tie_refs is not None:
                    if not _assert_tie_pairs(
                        tie_refs, source, prev, maps[0], asserted, pairs, seen
                    ):
                        # не-табличный операнд в связывающем равенстве
                        # (CTE/подзапрос) — пара реестром не выражается (C1)
                        unresolved = True
                elif prev.kind == "table" and source.kind == "table":
                    # фолбэк: равенств ON нет (USING; 2-табличное произведение) —
                    # пара по цепочке соседей
                    _assert_pair(prev.name, source.name, asserted, pairs, seen)
                else:
                    # любой операнд не-таблица (cte/sub, в любой комбинации) —
                    # пара реестром не выражается (C1)
                    unresolved = True
            prev = source
    return unresolved


def _assert_tie_pairs(
    tie_refs: list[set[str]],
    joined: _Source,
    prev: _Source | None,
    local_map: dict[str, _Source],
    asserted: dict[str, set[str]],
    pairs: list[tuple[str, str]],
    seen: set[tuple[str, str]],
) -> bool:
    """Пары связывающих равенств джойна: присоединяемый источник × контрагент
    (``_collect_join_pairs``). False = связывающее равенство держится на
    не-табличном операнде (CTE/подзапрос — операнды резолвятся ключами
    ``local_map``) или сам присоединяемый источник не таблица — пара реестром
    не выражается. Порядок обработки СТРУКТУРНЫЙ, не документный (re-review
    fix-волна 6, I-2): первична пара с непосредственным предшественником
    цепочки (``prev`` — sources[idx-1]) — прочие равенства-конъюнкты после неё
    отфильтровываются связностью как уже утверждённые; исход не зависит от
    порядка конъюнктов в ON. Итерация по sorted(refs)/сортированный список
    кандидатов — вердикт детерминирован (I-1: обход ``set`` зависел от
    PYTHONHASHSEED при равенстве с 3+ источниками; порядок пар = порядок
    issues для сигнала ретрая M1)."""
    if joined.kind != "table":
        return False
    candidates: list[tuple[str, str]] = []
    for refs in tie_refs:
        for ref in sorted(refs):
            if ref == joined.ref:
                continue
            other = local_map[ref]  # refs — ключи local_map (_eq_operand_refs)
            if other.kind != "table":
                return False
            pair = (joined.name, other.name)
            if pair not in candidates:
                candidates.append(pair)
    prev_name = prev.name if prev is not None and prev.kind == "table" else None
    candidates.sort(key=lambda pair: (pair[1] != prev_name, pair[0], pair[1]))
    for joined_name, other_name in candidates:
        _assert_pair(joined_name, other_name, asserted, pairs, seen)
    return True


def _assert_pair(
    a: str,
    b: str,
    asserted: dict[str, set[str]],
    pairs: list[tuple[str, str]],
    seen: set[tuple[str, str]],
) -> None:
    """Утверждение связи a↔b: в граф связности скоупа и в список сверки.
    Self-join (a==b) пары не даёт — реестр связь таблицы с собой не утверждает
    (skip на проверке пар, волны 1–2). Путь по утверждённым парам уже есть —
    равенство-конъюнкт фильтрует, новой связи не заявляет (канон волн 4–6:
    конъюнкт между связанными таблицами — предикат фильтрации; порядок
    обработки пар задаёт _assert_tie_pairs — первичная пара с предшественником
    цепочки первой)."""
    if a == b or _names_connected(asserted, a, b):
        return
    pair = tuple(sorted((a, b)))
    asserted.setdefault(a, set()).add(b)
    asserted.setdefault(b, set()).add(a)
    if pair not in seen:
        seen.add(pair)
        pairs.append(pair)


def _names_connected(asserted: dict[str, set[str]], a: str, b: str) -> bool:
    """Есть ли путь a → … → b по утверждённым парам скоупа (BFS по именам
    таблиц; размер графа — источники одного SELECT, десятки узлов максимум)."""
    if a == b:
        return True
    visited = {a}
    frontier = [a]
    while frontier:
        nxt: list[str] = []
        for name in frontier:
            for neighbour in asserted.get(name, ()):
                if neighbour == b:
                    return True
                if neighbour not in visited:
                    visited.add(neighbour)
                    nxt.append(neighbour)
        frontier = nxt
    return False


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


def _form_reject(sql: str, reason: str, row_limit: int) -> ValidateSqlResult:
    """Форм-отказ парсинга/обхода — вердикт hallucination с исходным SQL без
    изменений (ничего не исполняется); общий для обоих входов (FSM/свободный)."""
    return ValidateSqlResult(
        verdict=ValidationVerdict(
            outcome=ValidationOutcome.HALLUCINATION,
            issues=(f"SQL отвергнут по форме: {reason}",),
            enforced_limit=row_limit,
        ),
        sql=sql,
    )


def _result_with_rewrites(
    sql: str,
    tree: exp.Expression,
    verdict: ValidationVerdict,
    permissions: Mapping[str, Sequence[str]],
) -> ValidateSqlResult:
    """Ok-вердикт → перепись текста на AST (ruling R1/R2 ARCH-GATE S11): внешний
    LIMIT добавлен/скламплен, литеральные звёзды развёрнуты в разрешённые колонки —
    к исполнителю уходит только проверенный текст. Общий для обоих входов."""
    final_sql = sql
    if verdict.outcome is ValidationOutcome.OK:
        expanded = _expand_stars(tree, permissions)
        limited = _enforce_limit(tree, verdict.enforced_limit)
        if expanded or limited:
            final_sql = tree.sql(dialect="postgres")
    return ValidateSqlResult(verdict=verdict, sql=final_sql)


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
            return _form_reject(sql, exc.reason, row_limit)
        verdict = validate_sql(
            audit,
            skeleton=skeleton,
            join_plan=join_plan,
            skeleton_hash=skeleton_hash,
            permissions=permissions,
            data_catalog=data_catalog,
            row_limit=row_limit,
        )
        return _result_with_rewrites(sql, tree, verdict, permissions)

    def validate_free(
        self,
        sql: str,
        *,
        permissions: Mapping[str, Sequence[str]],
        data_catalog: frozenset[str] | set[str],
        row_limit: int,
        join_edges: frozenset[tuple[str, str]],
    ) -> ValidateSqlResult:
        """Свободный вход тул-контура (S31/D-033 п.5 вариант A): тот же парсинг,
        обход и ok-перепись (звёзды/LIMIT — к исполнителю уходит только проверенный
        текст), политика — ``validate_free_sql`` (общий ACL + инвариант реестра
        вместо скелета). Рёбра реестра читает вызывающий контур на каждый прогон
        (``SchemaGraphStore.join_edges``); форм-отказ — тот же вердикт
        hallucination с исходным SQL без изменений."""
        try:
            tree = _parse_tree(sql)
            audit = _build_audit(tree)
        except SqlAuditError as exc:
            return _form_reject(sql, exc.reason, row_limit)
        verdict = validate_free_sql(
            audit,
            permissions=permissions,
            data_catalog=data_catalog,
            row_limit=row_limit,
            join_edges=join_edges,
        )
        return _result_with_rewrites(sql, tree, verdict, permissions)

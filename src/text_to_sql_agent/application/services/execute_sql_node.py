"""Узел-сервис FSM `execute_sql` (S00 стык 1): исполнение SQL каналом `SQLExecutor`.

Два слоя (прецедент формы: generate_sql = сервис + node-фабрика):
- ``ExecuteSqlNode`` (S13/feat-013) — исполнение: читает `sql` из state, пишет
  `rows`/`row_count`/`truncated`/`error`; SQL-ошибка — структурированный
  ``{class, message, detail}`` (не traceback — S00 правило 8; message —
  фиксированный текст по классу, detail — сырой текст БД для diagnose, S9/A7
  S33). Пустой результат — валидный исход, не ошибка.
- ``make_execute_sql_node`` (S14/feat-014) — FSM-glue: RunContext из state
  (identity/trace — ruling R4 S05/R3 S08), defense-in-depth проверки каркаса
  (SQL приходит только от validate_sql) и probe COUNT при 0 строк
  (state-agent v1.7 — probe НЕ узел): COUNT по каждой таблице плана ТЕМ ЖЕ
  каналом agent_ro (капы LIMIT/timeout S13 — Ловушка 3), вердикт —
  ``FLAG_PROBE_LOST_JOIN`` (подозрение на потерянный джойн → diagnose_error)
  или молчаливое «реально пусто» → compact_result (ruling R1: валидный пустой
  ответ строится через interpret_result, НЕ short_circuit — тот терминал
  только шагового цикла).

Маршрут — routing.route_execute_sql (единственный источник рёбер; ruling R4:
заготовка execute_sql_route из S13 в граф не подключалась и удалена).
Дефекты каркаса (identity/sql/скелет отсутствуют) — fail-closed в degrade без
diagnose (``FLAG_EXECUTE_DEFECT``, дефект не чинится разбором — прецедент
resolve_access → rejected).
"""

from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from text_to_sql_agent.application.ports.sql_executor import (
    QueryRequest,
    SQLExecutionError,
    SQLExecutor,
    SQLFailureClass,
)
from text_to_sql_agent.application.ports.tracer import (
    EVENT_EXECUTE_PROBE,
    EVENT_EXECUTE_UNAVAILABLE,
    Tracer,
)
from text_to_sql_agent.application.services.s9_messages import ACL_DENIED_MESSAGE, NO_MATCH_MESSAGE
from text_to_sql_agent.domain.entities import RunContext, Trace
from text_to_sql_agent.domain.skeleton import Skeleton

SQL_KEY = "sql"
ERROR_KEY = "error"
ROWS_KEY = "rows"
ROW_COUNT_KEY = "row_count"
TRUNCATED_KEY = "truncated"

# application не импортирует orchestration — имена флагов дублируются из
# routing.FLAG_*; совпадение строк гвардит parity-тест (прецедент S05/S08/S10/S11)
PROBE_LOST_JOIN_FLAG = "probe_lost_join"
EXECUTE_DEFECT_FLAG = "execute_defect"

# Флаги шагового контура (S17, имена — routing.FLAG_STEP_*): в шаговом цикле
# 0 строк ключа — short_circuit/overflow-решения, probe одиночного запроса
# туда не входит (S17 подключит шаговое исполнение)
_STEP_LOOP_FLAGS: frozenset[str] = frozenset(
    {"step_done", "steps_pending", "step_empty_keyset", "step_keyset_overflow"}
)

_MISSING_SQL_CLASS = SQLFailureClass.SYNTAX
_MISSING_SQL_MESSAGE = "в state нет SQL для исполнения"

# Фиксированный пользовательский текст по классу сбоя (S9/A7, S33): сырой текст
# БД — внутренний error["detail"] (канал diagnose → подсказка регенерации), CLI
# печатает message. Ключи — SQLFailureClass.value; словарь покрывает весь enum —
# новый член без текста получает общий текст (fail-closed к «без деталей»).
_SQL_ERROR_MESSAGES: dict[str, str] = {
    SQLFailureClass.SEMANTICS.value: NO_MATCH_MESSAGE,
    SQLFailureClass.SYNTAX.value: (
        "не удалось обработать запрос — попробуйте переформулировать вопрос"
    ),
    SQLFailureClass.PERMISSION_DENIED.value: ACL_DENIED_MESSAGE,
    SQLFailureClass.READ_ONLY.value: ACL_DENIED_MESSAGE,
    SQLFailureClass.TIMEOUT.value: "запрос оказался слишком тяжёлым — попробуйте сузить вопрос",
    SQLFailureClass.CONNECTION.value: "хранилище временно недоступно — повторите запрос позже",
    SQLFailureClass.UNKNOWN.value: "не удалось обработать запрос — попробуйте позже",
}
_GENERIC_SQL_ERROR_MESSAGE = _SQL_ERROR_MESSAGES[SQLFailureClass.UNKNOWN.value]

# Фиксированные сообщения дефектов/пробы (S9-политика: пользовательский текст —
# фиксированный; факты пробы с именами/счётчиками — внутренний ключ error["probe"]
# (ревью S14 Important-1 — прецедент state.validation.issues S11: CLI печатает
# только message, внутренний контекст читает diagnose_error). Тексты общие с
# validate_sql_node — один источник в s9_messages (ревью S33 M-4)
_DEFECT_MESSAGE = ACL_DENIED_MESSAGE
_PROBE_LOST_JOIN_MESSAGE = NO_MATCH_MESSAGE


def _drop_stale_result(state: dict[str, Any]) -> dict[str, Any]:
    """Старый результат из входного state противоречил бы записанной ошибке."""
    for key in (ROWS_KEY, ROW_COUNT_KEY, TRUNCATED_KEY):
        state.pop(key, None)
    return state


class ExecuteSqlNode:
    """Сервис узла execute_sql: исполнение + раскладка результата в state."""

    def __init__(self, executor: SQLExecutor) -> None:
        self._executor = executor

    def run(self, state: dict[str, Any], *, ctx: RunContext) -> dict[str, Any]:
        """Возвращает НОВЫЙ dict-state с результатом (state иммутабелен для LangGraph)."""
        updated = dict(state)
        sql = updated.get(SQL_KEY)
        if not isinstance(sql, str) or not sql.strip():
            updated[ERROR_KEY] = {
                "class": _MISSING_SQL_CLASS.value,
                "message": _MISSING_SQL_MESSAGE,
            }
            return _drop_stale_result(updated)
        try:
            result = self._executor.execute(QueryRequest(sql=sql), ctx=ctx)
        except SQLExecutionError as exc:
            # A7/S33: message — фиксированный текст по классу (S9, печатается
            # CLI); detail — сырой текст БД (внутренний канал: diagnose строит
            # из него подсказку регенерации — модель видит фактические имена)
            updated[ERROR_KEY] = {
                "class": exc.error_class.value,
                "message": _SQL_ERROR_MESSAGES.get(
                    exc.error_class.value, _GENERIC_SQL_ERROR_MESSAGE
                ),
                "detail": str(exc),
            }
            return _drop_stale_result(updated)
        updated[ERROR_KEY] = None
        updated[ROWS_KEY] = result.rows
        updated[ROW_COUNT_KEY] = result.row_count
        updated[TRUNCATED_KEY] = result.truncated
        return updated


def make_execute_sql_node(
    executor: SQLExecutor,
    *,
    tracer: Tracer | None = None,
) -> Any:
    """Фабрика узла execute_sql для FSM: исполнение S13 + probe COUNT (S14)."""

    def node(state: dict[str, Any]) -> dict[str, Any]:
        passthrough_flags = dict(state.get("route_flags") or {})
        user_id = (state.get("user_id") or "").strip()
        if not user_id:
            # Defense-in-depth: identity проверена resolve_access — пустой user_id
            # здесь означает подмену каркаса; fail-closed (прецедент generate_sql_node)
            return _defect(passthrough_flags, tracer, "identity_missing")
        sql = state.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            # execute_sql достигается только от validate_sql (единственное ребро к
            # исполнению — ADR-002 §5); отсутствие SQL означает подмену каркаса —
            # fail-closed, не «исполнить пустоту» (S13-сервис оставлен в покое)
            return _defect(passthrough_flags, tracer, "sql_missing")
        ctx = RunContext(
            # trace_id ставит раннер (uuid прогона); fallback — прямые вызовы в тестах
            trace_id=state.get("trace_id") or uuid4().hex,
            user_id=user_id,
        )
        result = ExecuteSqlNode(executor).run(dict(state), ctx=ctx)
        update: dict[str, Any] = {
            key: result[key]
            for key in (ROWS_KEY, ROW_COUNT_KEY, TRUNCATED_KEY, ERROR_KEY)
            if key in result
        }
        if result.get(ERROR_KEY):
            # SQL-ошибка → diagnose_error (route_execute_sql); hint добавит
            # diagnose_error (S14) — здесь только факт сбоя
            update["route_flags"] = passthrough_flags
            return update

        # Успех; 0 строк одиночного запроса → probe COUNT (шаговый контур S17 — мимо)
        if result.get(ROW_COUNT_KEY) == 0 and not (_STEP_LOOP_FLAGS & passthrough_flags.keys()):
            probe_update = _run_probe(
                executor,
                state=state,
                ctx=ctx,
                user_id=user_id,
                passthrough_flags=passthrough_flags,
                tracer=tracer,
            )
            update.update(probe_update)
            return update
        update["route_flags"] = passthrough_flags
        return update

    return node


def _run_probe(
    executor: SQLExecutor,
    *,
    state: dict[str, Any],
    ctx: RunContext,
    user_id: str,
    passthrough_flags: dict[str, Any],
    tracer: Tracer | None,
) -> dict[str, Any]:
    """COUNT-проба по таблицам плана: потерянный джойн → diagnose, иначе — пусто.

    Проба — КОД, не LLM (state-agent v1.7); тем же каналом agent_ro с капами
    S13 (Ловушка 3: это тоже пользовательский SQL, не «служебное» подключение).
    Вердикт: подозрение на потерянный джойн — строки есть в КАЖДОЙ таблице
    плана, а джойн вернул 0 (данные по отдельности есть — вместе не сошлись);
    любая пустая таблица объясняет ноль честно — валидный пустой ответ.
    Сбой самой пробы — вердикта нет: пустой ответ остаётся валидным (S00-8).
    """
    raw_skeleton = state.get("skeleton")
    try:
        if not isinstance(raw_skeleton, dict):
            raise ValueError("skeleton отсутствует")
        skeleton = Skeleton.model_validate(raw_skeleton)
    except ValidationError as exc:
        return _defect(passthrough_flags, tracer, "skeleton_invalid", detail=f"Skeleton: {exc}")
    except ValueError:
        return _defect(passthrough_flags, tracer, "skeleton_missing")

    counts: dict[str, int] = {}
    inconclusive = False
    for table in skeleton.tables:
        try:
            probe_result = executor.execute(
                QueryRequest(sql=f"SELECT COUNT(*) AS probe_count FROM {table}"), ctx=ctx
            )
            row = probe_result.rows[0] if probe_result.rows else {}
            counts[table] = int(row.get("probe_count", 0))
        except (SQLExecutionError, IndexError, ValueError, TypeError, KeyError):
            # Проба не выполнилась — «потерянный джойн» не утверждаём: пустой
            # ответ остаётся валидным; деталь — оператору в трейс (S9)
            inconclusive = True
            if tracer is not None:
                tracer.record(
                    Trace(
                        event=EVENT_EXECUTE_UNAVAILABLE,
                        payload={"user_id": user_id, "detail": f"probe {table} не выполнилась"},
                    )
                )
            break
    lost_join = (not inconclusive) and bool(counts) and all(n > 0 for n in counts.values())
    if tracer is not None:
        tracer.record(
            Trace(
                event=EVENT_EXECUTE_PROBE,
                payload={
                    "user_id": user_id,
                    "tables": sorted(counts),
                    "counts": counts,
                    "verdict": "lost_join" if lost_join else "empty",
                },
            )
        )
    if not lost_join:
        return {"route_flags": passthrough_flags}
    # Факты пробы — внутренний ключ error["probe"] (S9-паттерн S11: CLI печатает
    # только message); diagnose_error передаст их в контекст ретрая (D-016)
    probe_summary = ", ".join(f"{table}: {count}" for table, count in counts.items())
    return {
        "error": {
            "class": "empty",
            "message": _PROBE_LOST_JOIN_MESSAGE,
            "probe": f"проба COUNT по таблицам плана — {probe_summary}; джойн вернул 0 строк",
        },
        "route_flags": {**passthrough_flags, PROBE_LOST_JOIN_FLAG: True},
    }


def _defect(
    passthrough_flags: dict[str, Any],
    tracer: Tracer | None,
    defect_class: str,
    *,
    detail: str | None = None,
) -> dict[str, Any]:
    """Дефект каркаса/стыка: fail-closed в degrade-ветку (FLAG_EXECUTE_DEFECT).

    Ретрай/разбор дефект не чинит (SQL приносит validate_sql, скелет — код) —
    degrade без diagnose; деталь — оператору в трейс (S9), пользовательский
    текст фиксированный.
    """
    if tracer is not None:
        tracer.record(
            Trace(
                event=EVENT_EXECUTE_UNAVAILABLE,
                payload={"class": defect_class, "detail": detail or defect_class},
            )
        )
    return {
        "error": {"class": defect_class, "message": _DEFECT_MESSAGE},
        "route_flags": {**passthrough_flags, EXECUTE_DEFECT_FLAG: True},
    }

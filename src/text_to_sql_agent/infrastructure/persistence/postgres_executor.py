"""Адаптер `SQLExecutor` на psycopg 3 — единственный канал SQL в данные (security §2.1).

Подключение — строго по DSN read-only роли `agent_ro` (рубеж 3; сервисный DSN
владельца схемы в этом канале не используется). Границы прав держит БД, а не код
(S13 правило 1):
роль не может DDL/DML (`default_transaction_read_only = on`), таймаут — сессионный
`statement_timeout`. Код добавляет три вещи:

- обёртка LIMIT — `SELECT * FROM (<sql>) AS q LIMIT <row_limit + 1>` с пост-срезом:
  извлечение row_limit+1 строки отличает «ровно limit» от «срезано» (флаг truncated).
  Пользовательский LIMIT/OFFSET не глотается — он применяется внутри подзапроса
  (ловушка 3: поведение документировано; AST-валидация SQL — S11);
- `SET LOCAL statement_timeout` — внутри транзакции запроса, не client-side cancel
  (значение дублирует сессионный дефолт роли — правило конфигурации pooler'а,
  ADR-005 §4);
- `set_config('app.user_id' / 'app.trace_id', …, is_local=true)` — эквивалент
  `SET LOCAL` с bind-параметрами (значения — данные, не литералы SQL): RLS-tripwire
  миграции 0004 (рубеж 5) и корреляция аудита БД (security §2.1, ADR-011).

RLS (миграция 0004): employees/bonuses отдают строки только при выставленном
`app.user_id` — fail-closed tripwire `current_setting('app.user_id', true) IS NOT
NULL`, ENABLE без FORCE (владелец postgres не обрезается; ruling ARCH-GATE S13 Q2).
Колоночный PII (`employees.salary`) RLS не решает — S05/S11.

Соединение — на вызов (MVP без пула; transaction-pooling совместим с `SET LOCAL` —
ADR-005 §4), `connect_timeout` — установка соединения вне statement_timeout
(правило 9: «чёрная дыра» не должна зависать). Классификация ошибок psycopg 3 →
`SQLFailureClass` (вход ретраев M1 в V4; MRO проверены — psycopg 3 ≠ psycopg 2):
QueryCanceled → TIMEOUT; ReadOnlySqlTransaction → READ_ONLY;
InsufficientPrivilege → PERMISSION_DENIED (whitelist-гранты);
UndefinedColumn/UndefinedTable/UndefinedFunction → SEMANTICS (несуществующий
объект — переписать выборку по фактическим колонкам мира, аудит 2026-11-23 A4/S33);
FeatureNotSupported/ProgrammingError → SYNTAX (грамматика, DML, отклонённый
обёрткой); OperationalError → CONNECTION; прочее → UNKNOWN.
"""

import psycopg
from psycopg.rows import dict_row

from text_to_sql_agent.application.ports.sql_executor import (
    CONNECT_TIMEOUT_S,
    ROW_LIMIT,
    STATEMENT_TIMEOUT_S,
    QueryRequest,
    QueryResult,
    SQLExecutionError,
    SQLFailureClass,
)
from text_to_sql_agent.domain.entities import RunContext

_DB_ERROR_MESSAGE_MAX_CHARS = 300  # Observation диагностики — не простыня psycopg-хинтов


def _classify(exc: psycopg.Error) -> SQLFailureClass:
    """psycopg 3-иерархия → класс сбоя: от специфичных к общим (порядок важен).

    psycopg 3 отличен от psycopg 2: SyntaxError ⊂ ProgrammingError, а не
    SyntaxOrAccessError; ReadOnlySqlTransaction ⊂ InternalError, а не
    OperationalError — ветки проверены по фактическим MRO.
    """
    if isinstance(exc, psycopg.errors.QueryCanceled):
        return SQLFailureClass.TIMEOUT
    if isinstance(exc, psycopg.errors.ReadOnlySqlTransaction):
        return SQLFailureClass.READ_ONLY
    if isinstance(exc, psycopg.errors.InsufficientPrivilege):
        return SQLFailureClass.PERMISSION_DENIED
    if isinstance(exc, psycopg.errors.FeatureNotSupported):
        # writable CTE в подзапросе и т.п.: SQL не принят каналом — как синтаксис
        return SQLFailureClass.SYNTAX
    # Undefined* ⊂ ProgrammingError — раньше общей ветки: «объекта нет» —
    # семантика выборки (переписать по фактическим колонкам мира), не грамматика
    # (аудит 2026-11-23 A4/S33; регенерация при temperature 0 с прежним классом
    # SYNTAX повторяла тот же запрос детерминированно)
    if isinstance(
        exc,
        (
            psycopg.errors.UndefinedColumn,
            psycopg.errors.UndefinedTable,
            psycopg.errors.UndefinedFunction,
        ),
    ):
        return SQLFailureClass.SEMANTICS
    if isinstance(exc, psycopg.errors.ProgrammingError):
        return SQLFailureClass.SYNTAX
    if isinstance(exc, psycopg.OperationalError):
        return SQLFailureClass.CONNECTION
    return SQLFailureClass.UNKNOWN


def _shorten(message: str) -> str:
    message = " ".join(message.split())
    if len(message) <= _DB_ERROR_MESSAGE_MAX_CHARS:
        return message
    return message[: _DB_ERROR_MESSAGE_MAX_CHARS - 1] + "…"


class PostgresExecutor:
    """Реализация `SQLExecutor` под read-only ролью `agent_ro` (psycopg 3)."""

    def __init__(
        self,
        dsn: str,
        *,
        row_limit: int = ROW_LIMIT,
        statement_timeout_s: int = STATEMENT_TIMEOUT_S,
    ) -> None:
        self._dsn = dsn
        self._row_limit = row_limit
        self._statement_timeout_s = statement_timeout_s

    def execute(self, request: QueryRequest, *, ctx: RunContext) -> QueryResult:
        if request.kind != "sql":
            raise SQLExecutionError(
                "канал PG исполняет только kind='sql'; kind='spec' — SAP-канал "
                "(ADR-011, после ДЗ-06)",
                SQLFailureClass.UNKNOWN,
            )
        try:
            return self._execute_sql(request.sql, ctx)
        except psycopg.Error as exc:
            error_class = _classify(exc)
            raise SQLExecutionError(
                f"БД отклонила запрос ({error_class.value}): {_shorten(str(exc))}",
                error_class,
            ) from exc

    def _execute_sql(self, sql: str, ctx: RunContext) -> QueryResult:
        # Нормализация под обёртку: хвостовые ";" — пустые операторы, семантики не
        # несут, но внутри подзапроса невалидны (LLM-выдача их регулярно вешает).
        # Это не правка текста запроса — срез только хвостовых разделителей (S13 ловушка 8).
        normalized = sql.strip().rstrip(";").strip()
        wrapped = f"SELECT * FROM ({normalized}) AS q LIMIT {self._row_limit + 1}"
        with psycopg.connect(
            self._dsn,
            row_factory=dict_row,
            connect_timeout=CONNECT_TIMEOUT_S,
            # Инвариант C1 самодостаточен: prepare=True обязан уходить в extended
            # protocol (multi-command отвергнут до исполнения); prepare_threshold=None
            # деградировал бы в simple protocol молча — пин явно (ревью S13 Minor-2)
            prepare_threshold=5,
        ) as conn:
            conn.execute(f"SET LOCAL statement_timeout = '{self._statement_timeout_s}s'")
            conn.execute("SELECT set_config('app.user_id', %s, true)", (ctx.user_id,))
            conn.execute("SELECT set_config('app.trace_id', %s, true)", (ctx.trace_id,))
            # prepare=True → extended protocol: PostgreSQL отвергает multi-command
            # («cannot insert multiple commands…»). Без него psycopg уходит в simple
            # protocol, и текст LLM вида `SELECT 1) AS q; SET statement_timeout=0;
            # SELECT pg_sleep(1000); --` исполняется стеком операторов — снимает
            # таймаут и обходит LIMIT (ревью S13 Critical-1).
            cursor = conn.execute(wrapped, prepare=True)
            columns = [description.name for description in cursor.description or ()]
            rows = cursor.fetchall()
        truncated = len(rows) > self._row_limit
        if truncated:
            rows = rows[: self._row_limit]
        return QueryResult(columns=columns, rows=rows, row_count=len(rows), truncated=truncated)

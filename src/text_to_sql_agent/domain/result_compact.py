"""ResultCompact — компактный вид результата для interpret_result (feat-015, S15).

Вход финального объяснения — ТОЛЬКО компакт (behavior feat-015): row_count +
сэмпл ≤ MAX_COMPACT_ROWS + агрегаты по числовым колонкам + флаги усечения.
Сырые строки в промпт интерпретатора не попадают (ADR-007 §4.3: компакт
~1150 токенов — бюджет входа strong-вызова). Поля — по er-data-model v1.5
(`sample`, не «rows»; ruling R2 ARCH-GATE S15).

Без IO, без LLM (границы domain): компакция — чистая функция над строками.
"""

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, field_validator

# Сэмпл строк в компактном виде: 20 строк × ~50 токенов ≈ 1000 токенов + агрегаты
# и служебные поля — канонный бюджет входа interpret_result ~1150 токенов
# (ADR-007 §4.3); больше — усечение с флагом row_count, не «молча больше»
# (Ловушка 3: усечение молча = ложный ответ).
MAX_COMPACT_ROWS = 20

# Числовые типы для агрегатов: int/float из dict_row + Decimal от NUMERIC-колонок
# psycopg. bool — подкласс int, но «сумма активных» — категориальная колонка,
# не метрика (исключается явно).
_NUMERIC_TYPES = (int, float, Decimal)


class ColumnAggregate(BaseModel):
    """Агрегаты кода по одной числовой колонке (sum/avg/min/max — без LLM).

    sum/avg — по непустым значениям (None — пропуск, не 0: ноль искажал бы
    среднее); min/max — исходные значения колонки.
    """

    sum: float
    avg: float
    min: float
    max: float


class ResultCompact(BaseModel):
    """Компакт результата: вход interpret_result, не пользовательский payload.

    `truncated` — зеркало state.truncated (усечение исполнителя LIMIT 1001,
    S13) — «неполное не выдаётся за полное» (state-agent v1.5). `approximate` —
    T2-флаг (S12): значение появляется у выпускной feat-012/017, VO несёт его
    с S15 (False — честный дефолт). Сэмпл — первые MAX_COMPACT_ROWS строк;
    остальное видно из row_count vs len(sample).
    """

    row_count: int = Field(ge=0)
    sample: list[dict[str, Any]] = Field(default_factory=list)
    aggregates: dict[str, ColumnAggregate] = Field(default_factory=dict)
    truncated: bool = False
    approximate: bool = False

    @field_validator("sample")
    @classmethod
    def _sample_within_prompt_budget(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(value) > MAX_COMPACT_ROWS:
            raise ValueError(
                f"сэмпл компактного вида больше бюджета промпта интерпретатора "
                f"({len(value)} > {MAX_COMPACT_ROWS}) — усекайте до компакции, fail-closed"
            )
        return value


def compact_rows(
    rows: list[dict[str, Any]] | None,
    *,
    truncated: bool,
    approximate: bool,
) -> ResultCompact:
    """Свернуть строки результата в компактный вид (код, без LLM).

    `rows` None — валидный путь T2-approximate (исполнение не было — S12):
    честный пустой компакт с approximate-флагом, не дефект. Агрегаты — по
    колонкам, где все непустые значения числовые; смешанная/категориальная
    колонка агрегатов не получает («сумма по смешанным типам» — ложь в payload).
    Порядок колонок агрегатов — отсортированный (детерминизм payload/промпта).
    """
    safe_rows = rows if rows else []
    aggregates: dict[str, ColumnAggregate] = {}
    if safe_rows:
        columns = sorted(safe_rows[0].keys())
        for column in columns:
            values = [row[column] for row in safe_rows if row.get(column) is not None]
            if not values:
                continue
            if not all(
                isinstance(value, _NUMERIC_TYPES) and not isinstance(value, bool)
                for value in values
            ):
                continue
            numbers = [float(value) for value in values]
            total = sum(numbers)
            aggregates[column] = ColumnAggregate(
                sum=total,
                avg=total / len(numbers),
                min=min(numbers),
                max=max(numbers),
            )
    return ResultCompact(
        row_count=len(safe_rows),
        sample=safe_rows[:MAX_COMPACT_ROWS],
        aggregates=aggregates,
        truncated=truncated,
        approximate=approximate,
    )

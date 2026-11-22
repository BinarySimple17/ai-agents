"""Юнит-тесты нормализации прав на границе адаптера (S05, ruling R5; ревью I3).

`_normalize_columns`/`_merge_columns` — единственная fail-closed реализация
CSV-парсинга и слияния ролей в рантайм-контуре: пустой/повреждённый CSV —
отказ (`RightsUnavailableError`), не тихое сужение прав; `'*'` сильнее любого
списка; дедупликация с сохранением порядка. Без БД — чистые функции адаптера.
"""

import pytest

from text_to_sql_agent.application.ports.permission_store import RightsUnavailableError
from text_to_sql_agent.infrastructure.persistence.pg_permission_store import (
    _merge_columns,
    _normalize_columns,
)


class TestNormalizeColumns:
    def test_star_stays_literal(self) -> None:
        # Разворот '*' в конкретные колонки требует DDL-интроспекции — S11/S09,
        # не S05 (ruling R5)
        assert _normalize_columns("*", "orders") == ("*",)

    def test_csv_to_tuple_preserves_order(self) -> None:
        assert _normalize_columns("employee_id,name,department", "employees") == (
            "employee_id",
            "name",
            "department",
        )

    def test_surrounding_whitespace_stripped(self) -> None:
        assert _normalize_columns(" a , b ,c ", "t") == ("a", "b", "c")

    def test_duplicates_deduplicated_in_order(self) -> None:
        assert _normalize_columns("id,name,id,dept", "t") == ("id", "name", "dept")

    def test_star_among_columns_absorbs_list(self) -> None:
        assert _normalize_columns("id,*,name", "t") == ("*",)

    @pytest.mark.parametrize("raw", ["", "   "])
    def test_empty_value_refused(self, raw: str) -> None:
        # Пустой CSV — админ-ошибка данных: отказ, а не «колонок нет = deny» —
        # отказ честнее: заставляет починить данные, не легализует молчаливый deny
        with pytest.raises(RightsUnavailableError, match="пуст"):
            _normalize_columns(raw, "orders")

    @pytest.mark.parametrize("raw", ["a,,b", ",a", "a, ,b"])
    def test_empty_entry_refused(self, raw: str) -> None:
        # Пустой элемент — повреждённая строка: отказ, не тихое сужение прав
        with pytest.raises(RightsUnavailableError, match="повреждён"):
            _normalize_columns(raw, "orders")


class TestMergeColumns:
    def test_new_table_stored(self) -> None:
        acc: dict[str, tuple[str, ...]] = {}
        _merge_columns(acc, "orders", ("id",))
        assert acc == {"orders": ("id",)}

    def test_lists_union_deduplicated(self) -> None:
        acc: dict[str, tuple[str, ...]] = {"orders": ("id", "name")}
        _merge_columns(acc, "orders", ("name", "total"))
        assert acc["orders"] == ("id", "name", "total")

    def test_star_beats_list(self) -> None:
        acc: dict[str, tuple[str, ...]] = {"orders": ("id",)}
        _merge_columns(acc, "orders", ("*",))
        assert acc["orders"] == ("*",)

    def test_list_after_star_stays_star(self) -> None:
        acc: dict[str, tuple[str, ...]] = {"orders": ("*",)}
        _merge_columns(acc, "orders", ("id",))
        assert acc["orders"] == ("*",)  # '*'-расширение не сужается вторым списком

    def test_merge_across_roles_star_and_lists(self) -> None:
        # Сценарий Boris (S09): две роли на одну таблицу — список + '*'
        acc: dict[str, tuple[str, ...]] = {}
        _merge_columns(acc, "orders", ("id",))
        _merge_columns(acc, "orders", ("*",))
        _merge_columns(acc, "orders", ("total",))
        assert acc["orders"] == ("*",)

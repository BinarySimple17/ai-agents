"""Тесты нормализатора текста (S26 Task 1; D-026 доп. 1(2))."""

from text_to_sql_agent.infrastructure.memory.text_normalizer import normalize_text


def test_combining_acute_removed_term_survives() -> None:
    # «Кана́рские» с акутом режется to_tsvector на «кан»+«рские» (D-026) —
    # после снятия акута слово цельное
    accented = "Кана́рские"
    assert normalize_text(accented) == "Канарские"
    assert "́" not in normalize_text(accented)


def test_yo_to_ye_lowercase() -> None:
    # ё/е эквивалентны для FTS-ноги (D-026): «объём» и «объем» — один терм
    assert normalize_text("объём") == "объем"
    assert normalize_text("ёлка") == "елка"


def test_yo_to_ye_uppercase_symmetric() -> None:
    # Регистр не трогаем, но замена ё→е симметрична в обоих регистрах:
    # заглавная Ё не должна переживать нормализацию, если строчная не переживает
    assert normalize_text("ЁЛКА") == "ЕЛКА"
    assert normalize_text("Ёлка") == "Елка"


def test_latin_and_digits_untouched() -> None:
    # Латиница (имена таблиц/колонок) и цифры не изменяются (Task 1)
    text = "orders.order_id, SUM(o.total) = 42"
    assert normalize_text(text) == text


def test_register_case_untouched() -> None:
    # Регистр букв НЕ нормализуется — приводит to_tsvector (D-026)
    assert normalize_text("Выручка ПО Заказам") == "Выручка ПО Заказам"


def test_idempotent() -> None:
    # Двойная нормализация = одна (хелпер применяется и к контенту, и к запросу,
    # которые сами могли быть уже нормализованы)
    once = normalize_text("Кана́рские ёлки объём")
    assert normalize_text(once) == once


def test_empty_and_plain_strings() -> None:
    assert normalize_text("") == ""
    assert normalize_text("выручка по заказам") == "выручка по заказам"

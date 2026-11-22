# BL-019 — Step-флаги: продакшен-guard и тест держат независимые литералы

**Приоритет:** P3 (косметика robustness; не блокер)
**Источник:** re-review fix-волны S14 (2026-11-22, «Ready to merge: Yes», Minor)
**Зона:** S14 → S17 (шаговый контур подключит потребителя)

## Контекст

Guard probe `_STEP_LOOP_FLAGS` (application/services/execute_sql_node.py:58-60) —
литеральный frozenset имён step-флагов; тест-кортеж `_STEP_FLAG_NAMES`
(tests/unit/test_execute_sql_node.py:29) — независимый литерал. Parity-тест
(S14, ревью Minor-1) гвардит копию В ТЕСТЕ против routing.FLAG_STEP_*: при
переименовании значения в routing dev обновит тест-кортёж, тест позеленеет,
а продакшен-frozenset молча разойдётся — probe сработает в шаговом контуре S17.

## Решение (за S17 — когда флаги шагового контура станут живыми)

Имена объявить в application (кортёж/константы в execute_sql_node или
общем месте), вывести `_STEP_LOOP_FLAGS = frozenset(...)`, тест импортирует из
модуля и сверяет с routing.FLAG_STEP_*. Следствие application-слоя без импорта
orchestration (прецедент дублирования имён с parity-тестом) — сейчас осознанное
упрощение, безопасное пока флаги каркасные (никто их не пишет до S17).

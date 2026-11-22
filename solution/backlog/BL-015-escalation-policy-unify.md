# BL-015 — Единый модуль политики эскалации M1: константы дублируются по сервисам узлов

- **Статус:** открыт <!-- легенду держать вне строки «Статус:» — archive.sh ищет «закрыт» в строке -->
- **Дата:** 2026-11-22
- **Сессия-источник:** S08 (REVIEW-GATE Minor-7)
- **Приоритет:** P3
- **Назначение:** S15 (Task 4 плана — перевод шва budget_remaining_roubles на порт BudgetGuard; там же закрыть запись; распределение backlog-триажа 2026-11-22)

## Что обнаружено

Политика эскалации M1 (D-016, ADR-007 §4.1) реализована одинаковым кодом в двух
сервисах узлов: `plan_joins_service.py` (S09) и `clarify_service.py` (S08) дублируют
`_ESCALATION_RESERVE_ROUBLES = 0.6` (канон ADR-007 §4.3), `_ROLE_UP` (cheap→mid→strong)
и паттерн «попытка 3 — роль вверх при остатке бюджета с резервом» с швом
`budget_remaining_roubles` (ruling Q7). S10 (generate_sql) и S14 (diagnose_error)
добавят третьего и четвёртого потребителя.

## Почему вне скоупа сессии

S08 не рефакторит S09-код (карта владения файлами; атомарность фичи), а унификация
раньше второго потребителя была преждевременной. Комментарии в обоих сервисах честно
ссылаются друг на друга.

## Предлагаемое решение

`application/services/retry_policy.py` (или domain-VO): константы `ESCALATION_RESERVE_ROUBLES`,
`ROLE_UP`, `MAX_NODE_ATTEMPTS` + хелпер `escalation_role(role, attempt, budget_remaining)` —
один источник для plan_joins/generate_sql/diagnose_error/clarify. При третьем потребителе
(S10) — не откладывать: расхождение значений (0.6 vs иное) сломало бы экономику E1–E6.

## Ход решения

- **S10 (2026-11-22, feat-010):** модуль создан — `application/services/m1_policy.py`
  (MAX_ATTEMPTS=3, ESCALATION_ATTEMPT=3, ESCALATION_RESERVE_ROUBLES=0.6, ROLE_UP);
  потребители: clarify (S08), plan_joins (S09), generate_sql (S10) и
  routing.MAX_NODE_RETRIES (паритет I4). Четвёртый (diagnose_error, S14) — импорт
  из того же модуля.
- **Остаток → S15 (Task 4):** шов `budget_remaining_roubles` — три одинаковые
  лямбды в `composition/container.py` (144/153/169) — переводится на порт
  BudgetGuard; там же закрыть эту запись (статус → закрыт).

## Закрытие (заполняется при закрытии)

[что решили и куда]

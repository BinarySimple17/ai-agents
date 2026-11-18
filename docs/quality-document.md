# Quality Document

<!-- ЗАПОЛНЯЕТСЯ В КОНЦЕ СЕССИИ (End of Session, шаг 5): оценка модулей.
     Новая сессия читает эту таблицу, чтобы понять, куда приоритизировать усилия.
     Шкала A–D: A = образцово, B = приемлемо, C = техдолг, D = опасно. -->

| Module | Correctness | Arch compliance | Test coverage | Verification evidence | Notes |
|---|---|---|---|---|---|
| `services/llm` | — | — | — | — | feat-003 |
| `graph/` (state machine) | — | — | — | — | feat-004 |
| `domain/` (validate/rbac) | — | — | — | — | feat-005, feat-011 |
| `services/vector` | — | — | — | — | feat-006 |
| `services/join_graph` | — | — | — | — | feat-007 |
| `services/db` (execute/seed) | A | A | B | A | feat-002 ✅ (миграции 0001–0003, тест 13 passed, чистая комната); B по покрытию: RLS-политики и исполнитель — S13; заявки/ETL-гранты — S07/S10 |
| `services/cache` | — | — | — | — | feat-016 |
| `cli/` | — | — | — | — | feat-004 |

## How to Update

1. После завершения фичи оцените затронутый модуль по 4 измерениям (A/B/C/D).
2. Оценка C или D в любой колонке — кандидат на отдельную фичу в `feature_list.json`.
3. Итоговая приёмка — по rubric: [templates/evaluator-rubric.md](../templates/evaluator-rubric.md).

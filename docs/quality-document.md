# Quality Document

<!-- ЗАПОЛНЯЕТСЯ В КОНЦЕ СЕССИИ (End of Session, шаг 5): оценка модулей.
     Новая сессия читает эту таблицу, чтобы понять, куда приоритизировать усилия.
     Шкала A–D: A = образцово, B = приемлемо, C = техдолг, D = опасно. -->

| Module | Correctness | Arch compliance | Test coverage | Verification evidence | Notes |
|---|---|---|---|---|---|
| `services/llm` (Settings + LLMProvider + OpenAIProvider + Tracer) | A | A | A | A | feat-003 ✅ + D-020 + D-021 (49 unit = 47 на моках + 2 real-SDK + 14 infra; ARCH-GATE rulings; REVIEW-GATE fix-волны → re-review; reasoning_content-фолбэк с guard по finish_reason и trace-провенансом — D-021; fuse best-effort — жёсткий контроль S15; embed — S06; smoke Layer 3 выполнен локально — D-020: LM Studio qwen3.8-9b, 3 роли, cost 0) |
| `graph/` (state machine) | — | — | — | — | feat-004 |
| `domain/` (validate/rbac) | — | — | — | — | feat-005, feat-011 |
| `services/vector` | — | — | — | — | feat-006 |
| `services/join_graph` | — | — | — | — | feat-007 |
| `services/db` (execute/seed) | A | A | A | A | feat-002 ✅ + feat-013 ✅ (миграции 0001–0004; исполнитель agent_ro — extended protocol, LIMIT 1001+пост-срез, SET LOCAL timeout, RLS-tripwire — 14 integration; чистая комната); заявки/ETL-гранты — S07/S10 |
| `services/cache` | — | — | — | — | feat-016 |
| `cli/` | A | A | A | A | feat-013 ✅ + S21 ✅ (function-calling цикл: кап 3 хода, ретраи пустых/битых не расходуют кап, деградация шлюза — 23 unit; события `agent.*` через порт Tracer + рендер ConsoleTracer/TeeTracer в stderr — 11 unit, observability §2.4, D-022; layer-3 живые прогоны на LM Studio D-020); FSM-подключение — feat-004 |

## How to Update

1. После завершения фичи оцените затронутый модуль по 4 измерениям (A/B/C/D).
2. Оценка C или D в любой колонке — кандидат на отдельную фичу в `feature_list.json`.
3. Итоговая приёмка — по rubric: [templates/evaluator-rubric.md](../templates/evaluator-rubric.md).

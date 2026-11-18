# Observability — наблюдаемость Text-to-SQL Agent

- **Статус:** Принято v1.3 (2026-11-15 — источник `/metrics`: Streamlit-демо; CLI-прогоны — спаны + query_trace, ADR-010 v1.2; v1.2 2026-11-14 — live-стек (ADR-010): Jaeger вместо Tempo, без коллектора (скрейп `/metrics` напрямую))
- **Назначение:** операционный уровень поверх prd FR-11 и NFR-1/2/6/8: что собираем,
  где хранится, что считаем, на какие пороги смотрим. Состав стека и его альтернативы —
  ADR-010; здесь — контракты сигналов и реакции. PII-политика телеметрии согласована
  с security.md §2–3.
- **Связанные документы:** prd.md (FR-11; NFR-1/2/6/8 — пороги), ADR-010 (стек),
  er-data-model.md (`query_trace` — system of record), security.md (аудит-события,
  PII), ADR-007 (рантайм-конверт, пересчёт экономики §7), RISKS.md (ранние индикаторы),
  c4-l2-container.md (Observability Plane)

---

## 1. Принцип: NFR — это SLO

Пользовательский симптом один: **«вопрос не получил хороший ответ»** — медленно
(NFR-1), дорого (NFR-2), некорректно (NFR-6), упало (NFR-8). Эти NFR и есть SLO
системы; пороги зафиксированы в prd §5 и продублированы в таблице §4.

Контур двухслойный (ADR-010): **live-слой** — спаны OTLP в Jaeger (из любого
процесса, включая CLI) и метрики на `/metrics` долгоживущего Streamlit-демо под
скрейпом Prometheus (v1.3), дашборды и поиск трейсов в Grafana,
уведомления Alertmanager (webhook: email/Telegram — уведомление, не пейджинг:
дежурства нет); **аналитический слой** — `query_trace` (навсегда): eval-сет,
отчёты, экономика по горизонту.

Объёмы: сотни–тысячи прогонов/мес × ~2 КБ на трейс = десятки МБ/мес — это обосновывает
семплирование 100% и retention по умолчанию (Prometheus ~15д, Jaeger ~14д).
«Watch the watcher»: self-metrics коллектора и up-таргеты всех бэкендов — панель
в той же Grafana.

## 2. Сигналы

### 2.1. Trace — два слоя (ADR-010 §4)

- **`query_trace` — system of record** (FR-11, er-data-model): один запрос = одна
  строка, append-only, хранится весь срок проекта. Бизнес-payload: question,
  assumptions, join_plan, sql, retry_count, latency_ms, cost_rub, cache_hit, status,
  incomplete, **`ttft_ms`**, **`node_timings` (jsonb)** — по-узловые события LLM-вызовов:

```json
[{"node": "generate_sql", "role": "mid", "model": "deepseek-v4.1-flash",
  "attempt": 2, "ttft_ms": 1400, "latency_ms": 5200,
  "tokens_in": 3100, "tokens_out": 210, "cost_rub": 0.18,
  "cache_hit": false, "timeout": false}]
```

  Единственная точка записи — адаптер `LLMProvider` (порт, c4-l2). На нём стоят
  eval-сет (R-P1), калибровка T1/T2 и пересчёт экономики §7 ADR-007 — поэтому
  `node_timings` живёт в БД независимо от retention телеметрии.
- **OTel-спаны — операционный слой:** корневой спан на прогон, по спану на узел FSM,
  LLM-вызов (атрибуты: node, role, model, attempt, tokens, cost_rub, timeout) и
  выполнение SQL (datasource, row_count). Живой waterfall в Grafana (data source
  Jaeger) — ответ «где зависло / где деньги» без ручного разбора (R-C2/R-C4).
  Retention Jaeger (Badger, локальный volume) ~14д — поэтому бизнес-истина в
  `query_trace`, не в спанах.

### 2.2. Логи — структурные события в stdout

JSON-строка на событие (не free-text), docker logs / journald + ротация. Loki
сознательно не ставится (§6) — логи не идут в стор:

```json
{"ts": "2026-11-14T12:00:03.120Z", "level": "WARN", "event": "llm_timeout",
 "trace_id": "9f2c…", "user_id": "boris", "node": "generate_sql",
 "role": "mid", "latency_ms": 15000, "err": "timeout after 15s"}
```

- **События:** старт/терминал FSM (статусы — er-data-model), аудит-события
  security §2 (`identity_rejected`, `degrade`, `validator_reject`,
  `guardrail_block(injection)`, `budget_stop`, `overflow`, `short_circuit`,
  `retry_exhausted`, `rate_limited`), `llm_call` / `llm_timeout` / `breaker_open`,
  запись трейса — все с `trace_id`.
- **Политика содержания:** текст вопроса, SQL-результаты, содержимое payload —
  **только в `query_trace`**, не в логах и не в span-атрибутах; в телеметрии —
  идентификаторы, счётчики, латентности, категории ошибок. PII-маскирование
  (security §3) применяется до записи. High-cardinality значения (`user_id`) — в
  теле события/лога, не в лейблах метрик (кардинальность — правило §2.3).
- **Best-effort:** лог и экспорт телеметрии не блокируют запрос; потеря строки
  допустима (истина — в `query_trace`).

### 2.3. Метрики — live в Prometheus (скрейп `/metrics`; источник — Streamlit-демо)

Долгоживущий процесс (Streamlit-демо) держит `/metrics` (prometheus-экспортёр OTel
SDK — тот же крошечный HTTP-сервер, что `/livez`/`/readyz`); Prometheus скрейбит
напрямую, без коллектора (из compose — `host.docker.internal`). CLI-прогон живёт
секунды и не скрейбится: его телеметрия — OTLP-спаны (async-batch) и `query_trace`;
вклад CLI-прогонов в счётчики — аналитическим слоем (`v_metrics_*`), не live-сериями
(ADR-010 v1.2).
Лейблы — только низкой кардинальности (`node`, `role`, `model`, `status`,
`class`): никакого `user_id`/вопроса в лейблах — иначе взрыв серий.

| Метрика | Тип | Лейблы | Для чего |
|---|---|---|---|
| `agent_runs_total` | counter | status (терминалы FSM) | success rate, доли исходов |
| `agent_llm_calls_total` | counter | node, role | объём по узлам |
| `agent_llm_latency_seconds` | histogram | node, role | p95 по узлам (NFR-1) |
| `agent_llm_ttft_seconds` | histogram | role | TTFT p95 (NFR-1, R-C2) |
| `agent_llm_cost_rub_total` | counter | node, role | экономика живым счётчиком (NFR-2) |
| `agent_validator_rejects_total` | counter | class: hallucination\|deny\|join\|step | R-S1/R-Q1 индикатор |
| `agent_cache_hits_total` | counter | result: hit\|assumption_miss | R-Q5 индикатор |
| `agent_breaker_open` | gauge | — | R-C4 |
| `agent_rate_limited_total` | counter | — | экономическая атака |

SQL-представления над `query_trace` (`v_metrics_daily`, `v_metrics_per_node`)
**не выброшены** — это аналитический слой: отчёты по горизонту, eval-сет, сверка
live-метрик с бизнес-истиной. Operational сигнал — Prometheus; отчёт — SQL.

## 3. Корреляция: один запрос — один trace_id

`trace_id` прогона = **OTel trace_id** корневого спана — единый ключ всех слоёв:

| Место | Как |
|---|---|
| `query_trace` | PK `trace_id` (= OTel trace_id) |
| OTel | корневой спан + дочерние (узлы, LLM-вызовы, SQL) — Jaeger находит waterfall |
| лог-события | поле `trace_id` (logging filter из текущего спана) |
| лог PostgreSQL | `SET LOCAL app.trace_id` (видно в `pg_stat_activity`) + комментарий `/* trace_id:… */` в SQL — в логах и `pg_stat_statements` |

Сценарий разбора жалобы: `trace_id` → Jaeger (waterfall: где висело) → Grafana
(контекст: что ещё деградировало в этот момент) → `query_trace` (что случилось:
статус, SQL, стоимость) → логи (почему: категория ошибки) → лог PG (какой SQL
физически выполнялся под `agent_ro`).

## 4. Пороги и реакции (симптом ≠ причина)

Симптомные — алерт-правила Prometheus → Alertmanager → webhook-уведомление;
cause-сигналы — дашборд/тикет, разбор на вехе. Пейджинга нет (дежурства нет);
при переносе в корпоративный контур таблица — готовая спецификация алертов.

| Сигнал | Класс | Источник | Порог | Реакция |
|---|---|---|---|---|
| success rate | симптом | `agent_runs_total` | < 80% (NFR-6) | уведомление → калибровка, eval (R-P1, R-Q1) |
| p95 latency | симптом | `agent_llm_latency_seconds` | > 20s (NFR-1) | уведомление → waterfall в Jaeger (R-C2/R-C4) |
| TTFT p95 | симптом | `agent_llm_ttft_seconds` | > 3s (R-C2) | уведомление → роутинг узлов (ADR-007) |
| средняя стоимость | симптом | `agent_llm_cost_rub_total` | > 0,7 ₽ (NFR-2) | уведомление → пересчёт §7 ADR-007, R-C1 |
| validator_reject / degrade доли | аудит | `agent_validator_rejects_total`, логи | вне ожидаемого окна | разбор, R-S1/R-Q5 |
| breaker / rate-limit | причина | `agent_breaker_open`, `agent_rate_limited_total` | > N за час | дашборд → разбор R-C4 |
| overflow / decomposition rate | причина | `query_trace`, логи | вне окон R-D1 | калибровка T1/T2 |
| ложные cache hit'ы | симптом | `agent_cache_hits_total{result="assumption_miss"}` | > 1% hit'ов (R-Q5) | разбор вопросных допущений clarify (ADR-009 п. 6; близость исключена с v1.2) |

## 5. Health checks

- **`/livez`** — процесс жив (без зависимостей): дёшево, для перезапуска.
- **`/readyz`** — PG и Valkey доступны (+ версия сборки в теле): для оркестратора;
  отдаёт тот же HTTP-сервер Streamlit-демо (healthchecks контейнеров — свои,
  см. ниже; app-контейнер — точка роста, v1: процесс на хосте).
- **db** — `pg_isready` в compose-healthcheck; контейнеры плоскости телеметрии —
  свои healthchecks, their up-таргеты — панель «watch the watcher» в Grafana.

Зависимости — только в readiness, не в liveness: перезапуск не лечит
недоступный PG, а только гоняет процесс.

## 6. Чего сознательно нет — и триггеры появления

Стек live-слоя (OTel SDK, Jaeger, Prometheus, Grafana, Alertmanager) —
решение ADR-010; ниже — то, что не входит даже в него:

| Решение | Что решает | Чем платим | Триггер появления |
|---|---|---|---|
| без OTel Collector — приложение экспортирует само | на контейнер меньше, меньше сопровождения | нет единой точки фильтрации/мульти-sink телеметрии | второй эмиттер, фильтрация на пути, multi-sink → вернуть коллектор (endpoint — env) |
| логи в stdout JSON, без Loki | нулевой сопровождение логов | нет центрального поиска, trace-to-logs | > 1 процесса/хоста или нужен корреляционный поиск по логам в UI |
| семплирование не включено (100%) | полнота eval-сета и спанов | объём растёт с нагрузкой | объёмы §1 перестают быть «десятками МБ» |
| уведомления, не пейджинг | дежурство не нужно | реакция минутами-часами, не мгновенно | корпоративный контур / SLA |
| retention по умолчанию (~15д/14д) | ноль настройки | нет долгой истории в live-слое | тренды за квартал → Mimir/долгий стор (или SQL-слой) |
| статические пороги алертов (не burn-rate) | просто и предсказуемо | возможен шум на границе | шум алертов → SLO burn-rate (ADR-010 revisit) |
| метрики без `user_id`-лейблов | кардинальность под контролем | нет per-user дашбордов | per-user разборы → логи/query_trace, не метрики |

Пересмотр — тем же правилом, что и для остальных обвязочных документов:
изменение порога/сигнала = правка здесь + затронутых строк RISKS/security
в одном заходе.

---

## Трассировка

Ревизии: v1.2 · Связанные ADR: ADR-002, ADR-007, **ADR-010** · Канон: hw-01/README.md §6 ·
Хранилище бизнес-трейса: er-data-model.md (`query_trace`) · Пороги: prd.md (NFR-1/2/6/8)

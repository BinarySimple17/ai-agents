# BL-016 — Семантика FSM-ретрая генератора: эскалация attempt 3 при generation-fail на retry-визите

**Приоритет:** P2 → **ЗАКРЫТ S14 (2026-11-22, ruling R3 ARCH-GATE)** — решён ребром
`generate_sql → retry_generate_sql` (см. «Решение» ниже); перенос в archive — `make archive`
**Источник:** re-review-2 S10 (Minor-5, 2026-11-22), заметка в set aside
**Зона:** S14 (diagnose_error + retry-цикл)

## Контекст

S10 закрывает ретраи M1 ВНУТРИ сервиса generate_sql (ruling R7 ARCH-GATE S10):
первый визит узла крутит попытки 1→2→3 (mid, mid+ошибка, strong при остатке
бюджета > 0,6 ₽). FSM-ретрай визит (ребро diagnose_error → generate_sql через
гейт `retry_generate_sql`, активирует S14) = РОВНО ОДНА попытка (re-review
I-r3: иначе attempt 3 исполняется дважды и кап «≤ 3 на узел» ломается).

Следствие (re-review-2 Minor-5): если визит FSM-ретрая (попытка 2, mid) дал
generation-fail (битый выход/структура), узел ставит FLAG_GENERATE_SQL_FAILED →
degrade (ruling R2) — попытка 3 (strong) в этой ветке не исполняется. Эскалация
«mid упал → strong» достигается только если визит 2 дал SQL, но downstream-валидация
снова упала.

## Решение (за S14) — ЗАКРЫТО ruling R3 ARCH-GATE S14 (2026-11-22)

Различение реализовано: GenerateSqlResult.fail_kind (generation | unavailable —
битый выход модели vs шлюз/retrieval/бюджет). На FSM-ретрай-визите
generation-fail при незатраченном fuse → флаг FLAG_GENERATE_SQL_RETRYABLE →
ребро route_generate_sql → гейт retry_generate_sql (инкремент кодом ребра) →
визит 3 — эскалация strong с бюджет-проверкой в сервисе (ruling R7 S10 —
start_attempt из счётчика гейта). Исчерпание fuse / шлюз / retrieval / бюджет →
FLAG_GENERATE_SQL_FAILED → degrade (R2 сохраняется в своей зоне — носитель
отказа при исчерпании/инфраструктуре). Детур через diagnose_error ОТВЕРГНУТ
(canon-check 2026-11-22): битый выход генератора самодиагностируем — текст
ошибки парсинга уже в retry-контексте сервиса; механический холостой транзит
мимо diagnose не нужен. Первый визит флаг не ставит (внутренние M1 1→2→3 уже
потрачены — иначе кап «≤ 3 на узел» ломался бы в 3+2 попытки).

Определение «попытки узла» зафиксировано (ruling R6): попытка M1 = шаг политики
ролей (1/2/3); первый визит — внутренний цикл ≤ 3 вызовов, FSM-ретрай-визит —
одна попытка (гейт считает визиты, fuse ≤ 3); потолок генератора — 5 LLM-вызовов
на прогон, абсолютный рубеж — llm_guard ≤ 8. Канонизировано в docstring
m1_policy.py и разделе «Ретраи» state-agent v1.8. Передано S15/BudgetGuard.

## Смежное (set aside re-review-2)

- «Суммарно ≤ 3 вызова LLM на узел»: FSM-счётчик считает визиты, не внутренние
  попытки — при internal-retry первого визита + FSM-ретраях возможно до 5
  вызовов (глобальный llm_guard ≤ 8 остаётся рубежом). Точное определение
  «попытки узла» зафиксировать при S14/BudgetGuard (S15).
- Полный DDL/DML deny-by-default (CREATE/INSERT/UPDATE/DELETE/…) и динамический
  SQL (query_to_xml и т.п.) — зона validate_sql (S11, ADR-005 рубеж 2).

# BL-016 — Семантика FSM-ретрая генератора: эскалация attempt 3 при generation-fail на retry-визите

**Приоритет:** P2 (блокирует качество S14, не S10)
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

## Решение (за S14)

Различать на retry-визите: (а) generation-fill-fail (битый выход — валидатор
модели) и (б) downstream-fail (SQL собран, но отклонён validate_sql). Для (а)
решить: вести в diagnose_error/следующую попытку (attempt 3, strong) вместо
немедленного degrade, или оставить degrade как есть. Согласовать с rule R2
(носитель отказа генератора — degrade) и fuse attempts_used (гейт уже
инкрементирует retry_count целевого узла).

## Смежное (set aside re-review-2)

- «Суммарно ≤ 3 вызова LLM на узел»: FSM-счётчик считает визиты, не внутренние
  попытки — при internal-retry первого визита + FSM-ретраях возможно до 5
  вызовов (глобальный llm_guard ≤ 8 остаётся рубежом). Точное определение
  «попытки узла» зафиксировать при S14/BudgetGuard (S15).
- Полный DDL/DML deny-by-default (CREATE/INSERT/UPDATE/DELETE/…) и динамический
  SQL (query_to_xml и т.п.) — зона validate_sql (S11, ADR-005 рубеж 2).

# BL-059 — interpret_result падает на Decimal (numeric из PostgreSQL) — json.dumps без default

- **Статус:** открыт <!-- открыт | закрыт (...) — легенда; archive.sh срезает комментарий до проверки -->
- **Дата:** 2026-11-27
- **Сессия-источник:** S32 (живой прогон Task 5, layer-3 — два прогона подряд ушли в crash на interpret_result)
- **Приоритет:** P1 (блокирует живой контур до `done` для ЛЮБОГО вопроса с numeric-колонками в результате — класс sum/avg/total; demo-поверхность ДЗ-06)
- **Назначение:** отдельная сессия (вне скоупа feat-023 — S32 не трогает interpret/compact; D-037: правки артефактов прошлых сессий/чужих компонентов — не «попутно»)

## Предмет (факт по трейсбеку, живой прогон 2026-11-27)

```
File ".../application/services/interpret_result_service.py", line 248, in _messages
    + json.dumps(compact, ensure_ascii=False),
TypeError: Object of type Decimal is not JSON serializable
```

Путь: numeric-колонка PostgreSQL (`numeric(12,2)` → psycopg возвращает `Decimal`) →
`compact_result` кладёт её в `sample` (≤20 строк) и/или агрегаты → `interpret_result._messages`
сериализует `json.dumps(compact)` без `default=` → TypeError → FSM-узел падает, прогон умирает
(EXIT=1) после полностью корректного validate/execute/compact.

Воспроизводится стабильно: вопрос «Какова общая сумма заказов с их позициями?» (run 2) и
«Покажи каждый заказ с его позициями и суммой заказа» (run 3) — оба дошли до interpret_result
и упали. Ранее живые прогоны (hw-04, S15) не ловили: вопросы давали COUNT (int) — Decimal
никогда не попадал в compact.

## Варианты фикса (для сессии-исполнителя)

1. `compact_result` нормализует значения при сборке sample/агрегатов (Decimal → float/str по
   контракту компакт-слоя) — чище по слоям: компакт — код, интерпрет — LLM; правка узла, тесты
   на numeric/fLOAT; но меняет формат компакта (проверить контракт interpret-промпта).
2. `interpret_result_service._messages`: `json.dumps(compact, ensure_ascii=False, default=str)`
   — минимально; Decimal уходит в промпт строкой (модель прочитает корректно); потеря типа для
   числовых сравнений в промпте не критична.
3. Оба слоя (defense-in-depth): нормализация в compact + default=str в interpret как страховка.

Связанное: compact_result_node агрегаты (sum/avg) по числовым колонкам — тот же Decimal-риск
в payload агрегатов; контракт `ResultCompact` (S15) описывает sample ≤ 20 — типы значений не
фиксированы; при фиксе — синк quality-document/observability в том же коммите.

# BL-005 — Централизация имён `llm.*`-событий: литералы в провайдере и рендерере

- **Статус:** закрыт (2026-11-22, S15 feat-015)
- **Дата:** 2026-11-19
- **Сессия-источник:** S21 (REVIEW-GATE re-review — Minor)
- **Приоритет:** P3
- **Назначение:** S15 (Task 4 плана — BudgetGuard подключает budget-события `llm.*`: централизация имён едет с третьим потребителем; распределение backlog-триажа 2026-11-22)

## Что обнаружено

Имена `agent.*`-событий централизованы в порте `tracer` (S21, ревью I1) — единый
источник для эмиттера и рендерера. Имена `llm.*` (`llm.complete/retry/timeout/
reject/breaker_open/budget_stop`) — пока строковые литералы в двух слоях:
`infrastructure/llm/openai_provider.py` (эмиттер) и
`infrastructure/observability/console_tracer.py` (`_TROUBLE_EVENTS`). Асимметрия
выглядит случайной: typo в одной стороне даст generic-строку/потерю рендера без
падения тестов.

## Почему вне скоупа сессии

Косметическая консолидация контракта провайдера (S03/D-021) — код стабилен,
тесты ловят поведение; объём правки чисто механический — не заслуживает
отдельной волны в S21.

## Закрытие (заполняется при закрытии)

S15 (feat-015, Task 4, ruling R5 ARCH-GATE): константы `EVENT_LLM_COMPLETE/RETRY/
TIMEOUT/REJECT/BREAKER_OPEN/BUDGET_STOP` — в порту `application/ports/tracer.py`
(прецедент `EVENT_AGENT_*`/`graph.*`); эмиттер (`openai_provider.py`) и рендерер
(`console_tracer.py`, `_TROUBLE_EVENTS`) импортируют из порта — литералов `llm.*`
в infrastructure больше нет. Новая семья «budget.*» не вводилась: `llm.budget_stop`
уже в семье; отказ бюджет-проверки в сервисе трейсится node-событием `*.unavailable`
reason=budget (прецедент R5 S14).
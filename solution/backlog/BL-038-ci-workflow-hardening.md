# BL-038 — Харднеж CI-workflow: пины actions по SHA, `permissions: contents: read`, concurrency

- **Статус:** открыт
- **Дата:** 2026-11-25
- **Сессия-источник:** S30 (REVIEW-GATE, Minor-10)
- **Приоритет:** P3
- **Назначение:** при первом живом CI-прогоне (вместе с BL-035) — правка workflow без живого прогона не верифицируется

## Что обнаружено

`.github/workflows/ci.yml`: все actions запинены на mutable major-теги (`@v4`, `@v5`, `@v10`),
нет `permissions: contents: read` (job получает дефолтный токен с лишними правами) и
`concurrency` (суперсед-прогоны). security.md упоминает supply-chain-сканирование —
для репо с таким профилем SHA-пины actions — дешёвая базовая мера.

## Почему вне скоупа сессии

Правка workflow без возможности живого прогона (сеть github недоступна) не верифицируема:
смена пинов на SHA может сломать резолв action'ов, и увидим мы это только в логах CI.

## Что делать

В S31 после первого зелёного прогона: запинить checkout/setup-python/setup-uv по полным SHA
(comments с версией), добавить `permissions: contents: read` на job-уровень и блок `concurrency`
(`group: ci-${{ github.ref }}`, `cancel-in-progress: true`). Проверить на втором прогоне.

## Закрытие (заполняется при закрытии)

[что решили и куда: feat-XXX / backlog другого проекта / отказ]

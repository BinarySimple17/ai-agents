# BL-038 — Харднеж CI-workflow: пины actions по SHA, `permissions: contents: read`, concurrency

- **Статус:** закрыт 2026-11-25 (S31, коммит 4c27e01): SHA-пины всех экшенов + permissions contents:read + concurrency cancel-in-progress
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

Реализовано в S31 (коммит 4c27e01, одним коммитом с BL-035/BL-040): (1) все экшены запины полным коммит-SHA (checkout 3d3c42e5…#v7.0.1, setup-python 6d4fd22b…#v7.1.0, setup-uv 1db35dd8…#v10.4.0, gitleaks e0c47f4f…#v3.0.0); теги и SHA сверены `gh api repos/<owner>/<repo>/commits/<tag>` 2026-11-25 (урок setup-uv: web-чек ≠ тег существует); (2) top-level `permissions: contents: read`; (3) `concurrency: group ci-${{ github.ref }} + cancel-in-progress` — суперседённый прогон той же ветки отменяется (пуш-волны подряд — экономия минут D-035).

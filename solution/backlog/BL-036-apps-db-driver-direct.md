# BL-036 — `plan_joins_demo` ходит в БД драйвером psycopg напрямую из apps

- **Статус:** открыт
- **Дата:** 2026-11-25
- **Сессия-источник:** S30 (canon-check архитектора, отложенная находка)
- **Приоритет:** P3
- **Назначение:** при следующей правке `src/apps/plan_joins_demo.py` или по желанию владельца

## Что обнаружено

`src/apps/plan_joins_demo.py:39` — `psycopg.connect(planner_dsn, ...)`: точка входа apps
исполняет SQL к PG минуя порт `SQLExecutor`/`postgres_executor`. Контракты импортов S30
этого не видят (прямой импорт внешнего пакета, не внутреннего модуля) — `git grep
text_to_sql_agent.infrastructure -- src/apps` тоже пуст. Демо-скрипт, не runtime-контур.

## Почему вне скоупа сессии

Демо-скрипт с прямым чтением (SELECT через agent_planner), не влияет на приёмку S30;
перенос соединения в composition/executor — правка поведения скрипта, отдельный шаг.

## Что делать

Вариант A: перевод демо на порт `SQLExecutor` (composition собирает). Вариант B: опциональный
контракт import-linter «apps → драйверы БД запрещён» (`forbidden` external: source apps,
forbidden [psycopg, neo4j, redis] — сегодня красный на plan_joins_demo). Решать при правке
файла; не тянуть в V4 без нужды.

## Закрытие (заполняется при закрытии)

[что решили и куда: feat-XXX / backlog другого проекта / отказ]

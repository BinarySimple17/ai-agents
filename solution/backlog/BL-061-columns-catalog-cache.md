# BL-061 — Кэширование columns_catalog по счётчику permissions

- **Статус:** открыт
- **Дата:** 2026-11-28
- **Сессия-источник:** S33
- **Приоритет:** P3

## Что обнаружено

Шов `PermissionStore.columns_catalog` (S33 R2) читает `information_schema.columns`
под `agent_planner` на каждый визит validate_sql в '*'-сцене (и на каждый
semantics-разбор diagnose — после ревью I-1 только при пустых issues). Прецедент
R3 (кэш data_catalog по счётчику settings_version в resolve_access) допускает
такой же кэш для каталога колонок: ключ — счётчик `permissions` + `graph_version`.

## Почему вне скоупа сессии

Ценовой класс «одно соединение на визит валидации/диагностики» принят ruling R2
ARCH-GATE S33 как допустимый; нагрузочных данных о деградации нет.

## Закрытие (заполняется при закрытии)

[что решили и куда: feat-XXX / backlog другого проекта / отказ]

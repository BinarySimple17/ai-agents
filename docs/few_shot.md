# Few-shot примеры «вопрос → SQL» (кураторские данные — ADR-003 §4)

> Формат: каждый пример — раздел `## [few-shot: <имя>] — таблица <таблица>`.
> Таблица — привязка для фильтра прав retrieval (основная таблица примера);
> тело раздела — контент чанка (префикс-заголовок остаётся в чанке — якорь,
> ACCURACY §6.2). Примеры кураторские, автонаполнение запрещено (ADR-003 §4).

## [few-shot: всего-заказов] — таблица orders

Вопрос: Сколько всего заказов в базе?
SQL: SELECT COUNT(*) AS orders_count FROM orders;

## [few-shot: заказы-из-РФ] — таблица orders

Вопрос: Сколько заказов у клиентов из РФ?
SQL: SELECT COUNT(*) AS orders_count
     FROM orders o
     JOIN customers c ON c.customer_id = o.customer_id
     WHERE c.country = 'RU';
Комментарий: страна — код 'RU', не 'Russia'; без JOIN заказы клиентов не
фильтруются по стране.

## [few-shot: выручка-по-кампаниям] — таблица orders

Вопрос: Какая выручка по каждой рекламной кампании?
SQL: SELECT ca.name AS campaign, SUM(o.total) AS revenue
     FROM campaigns ca
     LEFT JOIN orders o ON o.campaign_id = ca.campaign_id
     GROUP BY ca.name
     ORDER BY revenue DESC;
Комментарий: LEFT JOIN, чтобы заказы конкретной кампании не терялись; выручка
— SUM(orders.total) (см. глоссарий «выручка»).

## [few-shot: не-премированы] — таблица employees

Вопрос: Кто из сотрудников не был премирован?
SQL: SELECT e.name AS employee
     FROM employees e
     LEFT JOIN bonuses b ON b.employee_id = e.employee_id
     WHERE b.bonus_id IS NULL;
Комментарий: анти-джойн (LEFT JOIN ... IS NULL); премия — строка в bonuses.

## [few-shot: топ-клиентов] — таблица customers

Вопрос: Назови топ-5 клиентов по числу заказов.
SQL: SELECT c.name AS customer, COUNT(o.order_id) AS orders_count
     FROM customers c
     JOIN orders o ON o.customer_id = c.customer_id
     GROUP BY c.name
     ORDER BY orders_count DESC
     LIMIT 5;
Комментарий: топ-N — GROUP BY + ORDER BY + LIMIT; сортировка по агрегату.

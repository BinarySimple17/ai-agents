# S42 — BL-055: явный запрет SELECT…INTO в domain-валидаторе FSM

Этап: Промежуточная ветка `inter/hw05-hw06` · батч п.1(в) плана ветки (BL-055; окно 28–30.11.2026) | Владелец: агент | Зависимости: S40 (baseline) | Оценка: 0.5 | Статус: ✅ 2026-11-27
Фичи: — (сессия без фичи, states не меняются; затронуты behavior feat-011 — её verification-команда прогоняется, результат в отчёт, прецедент S24)
Роль по фиче: — (сессия без фичи, states не меняются)

## ⚠️ Прочитай перед началом

Исполнитель: ИИ-агент; куратор: пользователь. Предмет — **BL-055** (решение владельца 2026-11-27,
повод — ревью внешнего предложения «валидация SQL: схема + граф разрешённых соединений»; очередь
определена планированием S41 — батч п.1(в), старт по команде владельца). Третий пояс поверх двух
действующих границ класса «SELECT … INTO»:

- sqlglot парсит `SELECT … INTO` как `exp.Select` с arg `into`: гейты «корень ∈ {Select, Union,
  Subquery}» + deny-поиск Insert/Update/Delete/Create/Alter/Command его НЕ видят. Доказано прогоном
  на sqlglot 30.31.0 (pyproject: `sqlglot>=27.0`): `SELECT id INTO new_t FROM orders` →
  «PASS select», args содержит `into`.
- Класс сегодня закрыт на двух границах — **гейтом в валидаторе закрыт НЕ является**:
  1. генератор: поля заполнения скелета лексически запрещают SQL-структуры, включая `sql_exp.Into`
     («INTO — DDL-класс», re-review S10 I-r1) — `generate_sql_service.py:261-262,:281,:291,:296`;
     модель не пишет FROM, assemble_sql собирает запрос из скелета — INTO в собранный SQL не
     попадает;
  2. исполнение: `agent_ro` — `default_transaction_read_only = on` (миграция
     `20261118_0002_roles_hardening.py:65`), SELECT INTO отклоняется read-only сессией.
- В `domain/sql_validation.py` явной проверки нет: `validate_sql` (`sql_validation.py:192`) и
  хелперы `_check_*` аргумент `into` не инспектируют (grep по файлу — только docstrings
  `:25,:98,:279,:497`).

Defense in depth: валидатор — единственный гейт между assemble и execute; если появится путь к
`validate_sql` мимо сборки (новый узел, SQL из кэша, второй контур исполнения) — пояс уже стоит.
Цена — одна проверка + тест.

Границы скоупа: БЕЗ прочих находок ревью внешнего сниппета (allow-list функций — `_check_functions`;
CTE/sub/LATERAL — существующая категория; неявные соединения — BL-051(a) исполнено S39; whitelist
рёбер из FK — противоречит ADR-006; USING/NATURAL — qualify sqlglot 30.x разворачивает сам;
DuckDB/sqlfluff/pglast/EXPLAIN-зонд — не нужны FSM-контуру) — перечень закрыт в записи BL-055,
здесь не повторять. БЕЗ правки миграций и ролей (пояс 2 не трогается), БЕЗ изменений FSM-рёбер.

### Правила для исполнителя

1. Ветка `feat-validator-deny-into` от `inter/hw05-hw06`; merge — в `inter/hw05-hw06` (трек пред
   ДЗ-06; `milestone/v4` — неприкосновенная точка сдачи ДЗ-05; правило батча S41/п.1(в)).
2. Проверка — в domain-валидаторе (`sql_validation.py`, функция `validate_sql`): fail-closed отказ
   при наличии `into` — проверка корня `ast.args.get("into")` **и** `ast.find(exp.Into)`
   (вложенные случаи: подзапросы/CTE); формулировка issue в стиле существующих
   («SELECT … INTO — запись/DDL-класс, вне контура чтения»); docstring гейтов дополнить.
3. Регресс-тест `src/tests/unit/test_validate_sql.py`: SELECT INTO → отказ (по обоим способам
   детекции); контроль — обычный SELECT проходит. Тест обязан падать на до-правочном коде
   (проверка новая — выполняется по построению; stash-прогон для свидетельства, прецедент S04/S05).
4. Документы в том же коммите (точечные): `docs/security.md` §2.1/§3 (перечень гейтов валидатора —
   +INTO), S00 (если в гейтах валидатора перечислены deny-классы — дополнить). ADR-005 не трогается,
   если рубеж 2 описан без перечня операторов — сверить перед правкой.
5. Сессия трогает policy-часть domain-валидатора — ARCH-GATE canon-check ДО кода (не N/A).

## Предусловия

- [x] S40 закрыта (baseline трека): `grep -n "| S40 |" solution/sessions.md` → ✅ 2026-11-27
- [x] `make check` зелёный (baseline репозитория): EXIT=0, 838 passed, 7ef7b4d
- [x] feat-005, feat-011 в state=passing (сверка `feature_list.json`)
- [x] Сессия СТАРТУЕТ: статус в шапке переведён в «◐ в работе» и ARCH-GATE резолвлен ДО кода (canon-check architect ✅ либо обоснованный N/A) — старт-гейт (`guard-subagents.sh --phase start`, D-027) проверяет только стартованные (◐) сессии; «[ ] не начата» — план, гейт её скипает

## Задачи

### Task 1: Deny-проверка INTO в `validate_sql` (domain)

`sql_validation.py`: после корневых гейтов — проверка `ast.args.get("into")` (корневой SELECT) и
`ast.find(exp.Into)` (вложенности); нарушение → issue категории «запись/DDL вне контура чтения»
(класс результата — как у deny-поиска: отказ до исполнителя). Docstring гейтов: INTO —
запись/DDL-класс (Postgres переписывает в `CREATE TABLE AS`), sqlglot-ловушка arg `into` у
`exp.Select` (не видна deny-поиску Insert/Create).

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_validate_sql.py -q -k "into"   # новые тесты: корневой INTO, вложенный INTO (CTE/подзапрос), контроль — SELECT без INTO проходит
```

### Task 2: Регресс-тест + bite-свидетельство

Тесты в `test_validate_sql.py` (правило 3). Bite-свидетельство: stash правки валидатора → тест
падает на до-правочном коде → вернуть правку.

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_validate_sql.py -q                  # весь корпус без регрессий
uv run pytest src/tests/unit/test_diagnose.py -q                      # класс ошибки INTO → путь самокоррекции не ломается
```

### Task 3: Документы + закрытие BL-055 (тот же коммит)

`docs/security.md` (гейты валидатора), S00 (перечень deny-классов, если есть), отчёт; BL-055 —
шапка «закрыт» + «Закрытие» (коммит/сессия), строка реестра синхронно. verification feat-011
прогнать (результат в отчёт; states не меняются).

**✅ Самопроверка:**

```bash
uv run python -c "import ast;src=open('src/text_to_sql_agent/domain/sql_validation.py',encoding='utf-8').read();ast.parse(src);print('INTO' in src)"   # файл валиден, проверка присутствует
git grep -n "INTO" -- docs/security.md | head -3   # гейт описан
```

## Критерии приёмки

- [x] `SELECT id INTO t FROM orders` (и вложенные варианты) → отказ валидатора до исполнителя: корень/подзапрос/CTE → **DENIED** (degrade без retry), обычный SELECT проходит; bite-свидетельство в отчёте (stash: 3 denied-теста падают на до-правочном коде)
- [x] Корпус `test_validate_sql.py` и `make check` зелёные (EXIT=0, **844 passed**); verification feat-011 прогнана (**126 passed**, результат в отчёте; states не менялись)
- [x] `docs/security.md` v2.1 синхронен (§3 AST-whitelist +INTO, в коммите волны); S00/ADR-005 правок не требуют (deny-классов не перечисляют — вердикт architect + подтверждено ревью); BL-055 закрыт (файл + реестр; архивация — при закрытии сессии)
- [x] Вердикт ARCH-GATE записан до кода (canon-check **compliant**); states `feature_list.json` не менялись

## Ловушки

1. **`ast.find(exp.Into)` настрelится на строковые литералы?** Нет — sqlglot строит AST; но
   проверять оба способа (`args.get("into")` на корне + `find` на всё дерево): корневой arg и
   вложенный `exp.Into` — разные случаи парсинга (проверено прогоном BL-055).
2. **Категория отказа** — тот же канал, что deny-поиск (не новая категория): retry-политика и
   diagnose-классификация не должны увидеть новый класс (сверить карту `_classify` — INTO отсекается
   ДО исполнителя, до БД-ошибки дело не доходит).
3. **«INTO в подзапросе»** — `SELECT * FROM (SELECT id INTO t FROM x)` — sqlglot может
   нормализовать иначе; тест на вложенный случай обязателен (правило 2).
4. **Не тронуть `_check_functions`/ACL-логику** — правка локальна в гейтах корня/дерева; раздутый
   diff = ошибка скоупа.
5. **verify-feature не требуется** — сессия без фичи; states не меняются (в отличие от S32).

## Гейты субагентов (заполняется по факту, до закрытия сессии)

- ARCH-GATE: ✅ compliant — canon-check architect 2026-11-27, форма: детекция в services (оба способа правила 2: `args.get("into")` на корне + `find(exp.Into)` на дереве) → IR-флаг `SqlAudit.has_into` → политика в domain (`issues_denied` → DENIED, degrade без retry — канон ADR-005 §4/:173, прецедент классового denied `sql_validation.py:254`); `exp.Into` в whitelist с обязательным комментарием «не тихий allow — явный deny-гейт domain» + строка о сцепке с парсингом sqlglot (митигация — adversarial-тесты §4 п.8, uv.lock). Находки canon-check (закрыты в реализации/отчёте): буква правила 2 «AST в domain» R4-невозможна — осознанное отклонение в отчёт; тесты ассертят outcome=DENIED + наличие INTO-issue, не точный список (цель INTO парсится exp.Table → hallucination-шум в issues, исход не меняет — denied доминирует); предпосылка BL-055 «гейтом не закрыт» неточна — whitelist уже form-reject'ит (hallucination), недоставало явного denied-гейта — формулировку поправить при закрытии; S00/ADR-005 правок не требуют (deny-классов не перечисляют); FSM-рёбра/диагностика не затронуты (error.class общий `acl_denied`, diagnose INTO не видит)
- REVIEW-GATE: ✅ Ready to merge: Yes — цепочка двух ревью и двух fix-волн: ревью-1 `7ef7b4d..ed9047d` («Ready to merge: Yes», 0C/0I/3M: M-1 INTO-кейсов нет в TestAdversarialGallery при ссылке из whitelist-комментария, M-2 дубль детекции без объяснения, M-3 неоднородные ассерты «ничего не исполняется») → fix-волна `1a2ac0f` (все три закрыты: +2 INTO-кейса в галерею — ревьюер независимо подтвердил bite обоих новых кейсов через отключение гейта; докстринг-страховка; однородные ассерты) → re-review-2 `ed9047d..1a2ac0f` («Ready to merge: Yes», 0C/0I/1M: точность комментария «deny-поиск Insert/Create» — механизм из формулировки записи, в коде нет) → fix-волна-2 `05bbbc6` (только комментарии, все 3 вхождения — дословно предписанный ревьюером reword «fix all three or none»; правка санкционирована самим ревью, отдельный цикл не заводился). Итоговый диапазон `7ef7b4d..05bbbc6`; make check EXIT=0, 844 passed

## Отчёт по сессии (заполняется по факту)

- Сделано:
  - BL-055 исполнен тремя коммитами ветки `feat-validator-deny-into` (от `inter/hw05-hw06`): **ed9047d** — волна (код+тесты+docs+BL-055+журналы старта), **1a2ac0f** — fix-волна-1 по ревью (0C/0I/3M), **05bbbc6** — fix-волна-2 по re-review (комментарии). Сообщение волны амендовано до пуша (опечатка «третийпояс», локальная ветка).
  - Реализация (форма по canon-check — compliant, ruling R4): services `_detect_into` (`validate_sql_service.py`) — оба способа правила 2: корневой `args.get("into")` + `find(exp.Into)` на всём дереве (подзапросы/CTE); IR-флаг `SqlAudit.has_into` (domain без sqlglot); domain `_check_into` (`sql_validation.py`) — `issues_denied` «SELECT … INTO — запись/DDL-класс (Postgres переписывает в CREATE TABLE AS), вне контура чтения» → **DENIED**, degrade без retry (route_validate_sql: FLAG_ACL_DENIED → degrade до hallucination-ветки — diagnose INTO не видит, error.class общий `acl_denied`); `exp.Into` добавлен в `_NODE_WHITELIST` с комментарием «не тихий allow — передача на явный domain-гейт» + сцепка с парсингом sqlglot. Адресат ловушек: _check_functions/ACL/_collect_tables не тронуты (подтверждено ревью — diff-гигиена).
  - Тесты: `TestSelectIntoDeny` 4 (корень/подзапрос/CTE → DENIED + наличие INTO-issue; контроль обычный SELECT OK; ассерты `result.sql == sql` во всех denied) + 2 INTO-кейса в `TestAdversarialGallery` (§4 п.8) = **844 passed** total (838 baseline + 6); test_validate_sql.py — 128. **Bite-свидетельство** (Task 2): stash правок валидатора → на до-правочном коде 3 denied-теста падают (HALLUCINATION ≠ DENIED — form-reject whitelist'а), контроль проходит → pop; ревьюер подтвердил независимо (вторая половина: отключение `_detect_into` тоже роняет корпус — тесты держат обе половины фикса).
  - Документы: `docs/security.md` **v2.1** (§3 AST-whitelist-строка + INTO-гейт, версионная строка); S00/ADR-005 не тронуты (deny-классов не перечисляют — условие правила 4 «если перечислены» не наступило; вердикт architect, подтверждён ревью grep'ом); quality-document.md — строка домена дополнена S42-нотой; BL-055 закрыт (файл + строка реестра; make archive перенесёт файл при закрытии).
  - Верификация: `make check` EXIT=0 (прогон после волны, после каждой fix-волны — 842→844→844); **verification feat-011** (`uv run pytest src/tests/unit/test_validate_sql.py -q`) — **126 passed**, прогнана по правилу 4 файла сессии (states не менялись — сессия без фичи, ловушка 5).
- Отклонения от плана:
  - **Правило 2 (буква) → форма R4**: план предписывал AST-инспекцию (`ast.args.get("into")` + `ast.find(exp.Into)`) в domain-валидаторе, но domain видит только нейтральный IR (ruling R4: sql_validation.py не импортирует sqlglot). Интент сохранён полностью — оба способа детекции живут в services (`_detect_into`) и доставляются IR-флагом `has_into`, решение (DENIED) принимает domain. Canon-check architect: **compliant**, зафиксировано в ARCH-GATE до кода.
  - **Фактура против предпосылки записи**: BL-055 утверждал «гейтом в валидаторе закрыт НЕ является» — пробой показал: whitelist-обход `_parse_tree` уже form-reject'ил ОБЕ формы (exp.Into вне whitelist'а) как hallucination. Работа превратилась из «закрыть дыру» в «явный denied-гейт с верной классификацией» (write-попытка — сигнал безопасности, не «несуществующий синтаксис» с оправданным ретраем); для этого `exp.Into` включён в whitelist — иначе domain-гейт был бы мёртвым кодом за form-reject'ом. Формулировка уточнена при закрытии записи (предписание architect).
  - Осознанное UX-следствие (Set aside ревью, принято): запрос с INTO больше не даёт hallucination→diagnose→retry, а DENIED→degrade без retry — требование BL-055, не дефект.
- Перенесено в другие сессии:
  - Ничего. Осознанно НЕ заводится запись: асимметрия классификации (корневые INSERT/MERGE/DDL — form-reject hallucination+retry против INTO — denied) — opinion архитектора «кандидат в backlog при первом кейсе» (прецедент отложенного residual BL-051); скоуп сессии («БЕЗ изменений FSM-рёбер») это не затрагивает.

# S39 — Удаление тул-контура BL-048 + волна канона + D-041; BL-050(c); BL-051(a)

Этап: промежуточная ветка `inter/hw05-hw06` (между ДЗ-05 и ДЗ-06) | Владелец: агент (команда владельца 2026-11-27: «стартуй по очереди BL-048, BL-50(c) и BL-051(a)») | Зависимости: S31 (единый рубеж), S29/S30 (аудит/защиты) | Оценка: 2.5–3 (BL-048: коммит удаления ≈1 ч, волна ревизий ≈1–1,5 ч, D-041 ≈0,5 ч; риск-множитель canon-check ×1,5 — прикидка агента, не решение владельца) | Статус: ◐ в работе
Фичи: — (сессия без фичи, states не меняются; feature_list — ТОЛЬКО тексты feat-013/feat-020/feat-011 в волне ревизий, states/evidence не трогаются — правило verify-feature)
Роль по фиче: — (сессия без фичи, states не меняются)

## ⚠️ Прочитай перед началом

Исполнитель — агент; ветка `inter/hw05-hw06` (отпочкована от milestone/v4 `5032711` — точки заморозки ДЗ-05; CI на пуши ветки НЕ триггерится, D-035 — рубеж локальный `make check`). К моменту старта: milestone/v4 запушена (`5032711` на origin), коммиты `3112abb` (план ветки) и `b6cbf68` (BL-055, только беклог-запись) на ветке.

**Что делается в этой сессии** (очередь владельца, по очереди):
1. **BL-048** — removal-коммит тул-контура ОДНИМ коммитом (листья в §«Что делать» п.1 BL-048) + pin-тесты residual BL-051(a) в том же коммите + миграция B2-кейсов; затем волна ревизий канона (п.2 BL-048 + строки канона BL-051(a) п.1/п.3) и D-041 (п.3 BL-048).
2. **BL-050(c)** — отдельный коммит ПОСЛЕ removal: fail-closed проверка `join_operator='='` в считывателе семантики рёбер (`neo4j_graph_store.py`, `path_semantics` — единственная точка входа `join_operator` в мир агента) + юнит-тест + канон-пометки ER `er-data-model.md` и ADR-006 §4.1/§4.2.
3. **BL-051(a)** — отдельный коммит: правило «все таблицы запроса ⊆ план» в `_check_skeleton` + 2 регрессионных теста (pin-тесты residual из коммита удаления переворачиваются осознанно).

**Что сознательно НЕ делается** (границы скоупа): milestone/v2 и milestone/v3 не патчатся (KI-1 — якоря воспроизведения); артефакты hw-01…05 не трогаются (D-037); `.docs/` READ ONLY (D-011); states/evidence feature_list.json не меняются; пуш ветки — отдельно, по команде владельца; BL-049/BL-055 и хвосты S32–S36 — не в этой сессии; CHECK-ограничение BL-050 уровня 2 — в ближайшую миграцию V5 (BL-047), не сейчас; расширение IR-сверки на не-EQ — не сейчас (revisit-триггер BL-050).

### Правила для исполнителя

1. Планы и якоря — BL-048/BL-050/BL-051 (решения владельца 2026-11-26, записи BL-050(c) и BL-051(a) в archive/); каждый file:line-якорь перепроверить по коду перед правкой (могли съехать).
2. Коммиты: (1) removal (код+тесты), (2) волна канона + D-041 (доки/DECISIONS), (3) BL-050(c) (код+тест; ER/ADR-006 пометки — в волне Task 2, чтобы ADR-006 не править дважды), (4) BL-051(a) (код+тесты). Дерево консистентно после каждого (после removal-коммита канон временно упреждает удаление — волна ревизий идёт следом, это принятая структура BL-048).
3. `milestone/v2` не патчится НИКОГДА (KI-1) — никакие правки не «доезжают» до якорей задним числом.
4. Канон правится ТОЛЬКО в этой сессии (canon-check пройден, ARCH-GATE ✅) — вне сессии с canon-check правки канона запрещены.
5. Makefile/dev-цель: после удаления дефолт CLI = FSM (`make dev` остаётся рабочим — команды эталонов hw-04 семантически валидны).

## Предусловия

- [x] S31 закрыта (единый рубеж — база, на которой построены и removal, и BL-051: `git log --oneline -1 milestone/v4` → 5032711)
- [x] Ветка `inter/hw05-hw06` активна, дерево чистое: `git status --short` → пусто; `git branch --show-current` → inter/hw05-hw06
- [x] Базовый `make check` зелёный на старте (прогон до правок)
- [x] Якоря BL-048 перепроверены по коду 2026-11-27 (grep-сверка: run_sql_tool только в cli/container/tests; join_edges только порт+neo4j+тул+тесты; validate_free только validate_sql_service; FakeSchemaGraphStore только в удаляемых тестах; format_retrieval_block/RETRIEVAL_TOP_K используются test_hybrid_memory/bench_retrieval — НЕ удаляются)
- [x] Сессия СТАРТУЕТ: статус в шапке «◐ в работе», ARCH-GATE резолвлен ДО кода

## Задачи

### Task 1: Removal-коммит — тул-контур одним коммитом (BL-048 п.1)

Код:
- удалить `application/services/run_sql_tool.py`;
- `domain/sql_validation.py`: `validate_free_sql`, `_check_join_pairs`, IR-поля `join_table_pairs`/`unresolved_join_scopes` (+ комментарий-хвост в docstring `SqlAudit`);
- `application/services/validate_sql_service.py`: метод `validate_free`, механика пар/корреляций (`_join_tie_refs`, `_inside_nested_select`, `_CONJUNCTIVE_SKELETON`, `_under_conjunctive_skeleton`, `_eq_operand_refs`, `_comparison_local_tables`, `_collect_join_pairs`, `_assert_tie_pairs`, `_assert_pair`, `_names_connected`, `_COMPARISON_TYPES`), население IR в `_build_audit` (пары/флаг; ACL-часть корреляции — кандидаты внешнего скоупа и whole-row — ОСТАЁТСЯ, её потребляет FSM-ACL), импорт `validate_free_sql`;
- порт `SchemaGraphStore.join_edges` + neo4j-реализация `join_edges` (единственный потребитель — тул); fakes.py: `FakeSchemaGraphStore` (потребители — только удаляемые тесты);
- `cli.py`: `--plain`, merged-flow (`run_tool_loop`/`_run_loop`/`run_agent_flow`/`_consume_stream`/`_stream_outcome`/`_observation`/`_format_result`/`_tool_*`/`_assistant_message`/`_is_malformed_tool_call`), промпт-механика тул-контура (`_DEMO_SCHEMA_COLUMNS`/`_schema_block`/`_SYSTEM_PROMPT_TEMPLATE`/`SYSTEM_PROMPT`/`AGENT_SYSTEM_PROMPT`/`_render_system_prompt`/`_FULL_WORLD_RIGHTS`/`_AGENT_PROMPT_SUFFIX`/DEGRADED/STOP/OBSERVATION-константы/`_emit`/`MAX_TOOL_CALLS`/`EMPTY_RESPONSE_RETRIES`/`OBSERVATION_MAX_ROWS`/`MALFORMED_TOOL_CALL_TAG`/`DATA_MARKING`/`DEFAULT_ROLE`/`_print_stdout`), дефолт `main` → `_run_fsm`; **ОСТАЮТСЯ**: `format_retrieval_block` + `RETRIEVAL_TOP_K`/`RETRIEVAL_CONTEXT_*`/`RETRIEVAL_SEPARATOR`/`RETRIEVAL_BLOCK_HEADER`/`RETRIEVAL_EMPTY_BLOCK` (потребители вне тул-контура: `apps/bench_retrieval.py`, `integration/test_hybrid_memory.py`), `_FSM_CLARIFY_ROUNDS`, `main`/`_print_final_answer`/`_run_fsm`, флаг `--fsm` (совместимость артефактов hw-05);
- `composition/container.py`: импорт/поле/сборка `run_sql_tool`;
- `Makefile`: комментарий `dev` → FSM-дефолт.

Тесты: удалить `test_run_sql_tool.py`, `test_tool_loop.py`, `test_agent_flow.py`, `test_validate_free_sql.py`, `test_retrieval_flow.py` (промпт-механика удалена; `format_retrieval_block`-тесты живут в `test_hybrid_memory.py`); wiring-тест единого рубежа (`test_container_wiring.py::test_run_sql_tool_wired_behind_single_gate`); `test_join_edges_active_snapshot_pairs` (`test_join_graph.py`).

Миграция корпуса в `test_validate_sql.py`: B2-кейсы, контур-независимые по исходу (подзапрос с чужой таблицей в теле → DENIED; подзапрос в FROM с чужой таблицей → DENIED) — в `TestAdversarialGallery`; два флипа вердикта (корневой UNION своих таблиц → FSM hallucination; LATERAL-смешанный скоуп → FSM ok) уже запинены FSM-кейсами — добавить пометки-комментарии о флипе (наследие B2/BL-051). **Pin-тесты residual (BL-051(a) п.2)**: оба кейса таблицы BL-051 (EXISTS с таблицей вне плана + вложенный JOIN без ребра, права включают bonuses/campaigns) — ожидают OK с пометкой «known residual, BL-051»; LATERAL-смешанный (существующий `test_lateral_subquery_checked`) — пометка пина.

**✅ Самопроверка:**

```bash
grep -rn "run_sql_tool\|RunSqlTool\|validate_free\|join_edges\|run_tool_loop\|run_agent_flow" src/ --include="*.py" | grep -v __pycache__   # → пусто (кроме исторических комментариев в миграциях/доках, если останутся — ревизия волной)
make check    # EXIT=0; тестов меньше на ~удалённые файлы, все зелёные
git add -A && git commit   # removal-коммит (MSG-файл в .git/)
```

### Task 2: Волна ревизий канона (BL-048 п.2 + строки BL-051(a) п.1/п.3 + строки BL-050(c))

ADR-002 → v1.7 (инвариант: явные JOIN корневого скелета — рёбра плана; во вложенных телах — ACL; строка :97); ADR-005 → v1.4; prd → v1.9 (FR-2/FR-4, §9; строка :64); c4-l3 → v1.7 (убрать `run_sql` и «тул-контур» — c4-l3-agent.md:108-112); security.md v2.0 (висящее «прогнан через ОБОИХ входа» :53-54 + строка :130; **canon-check: вся таблица §3 — строки :125-126 тоже нормативны**); ADR-006 (инвариант :37/:48/:192/:197; абзац :199-232 переписать под факт FSM; §4.1/§4.2 — пункт «оператор поддержан исполнителем»: **формулировка canon-check — fail-closed в считывателе сейчас + пункт наследует гейт при постройке (BL-026), не существующий CI-механизм**); ARCHITECTURE (:51,:54); RISKS R-Q1 (:35-40) + строка :38; adr/README (:20-22; **canon-check: и строка ADR-002 :17 — «ДВА ребра (S31) | v1.6»**, в ячейках — целевые версии ADR-002 v1.7 / ADR-005 v1.4 / ADR-006 v1.9); observability (:156-157,:168; **canon-check: :167-171 — поле `contour` исчезает из payload validate_sql.* — его эмитит только тул**); model-card (:81,:113); er-data-model.md:83 (BL-050(c): «v1: только '=', прочие отклоняются рубежом»); feature_list — ТОЛЬКО тексты (states/evidence не трогать): feat-013/feat-020/feat-011 **+ behavior feat-004 (упоминание «дефолтный тул-луп (--plain) не изменился» — находка 2026-11-27 при сверке, та же природа правки)**. Каждая правка — по факту после удаления (упреждаться фактом всей сессии: BL-051(a) правило «таблицы ⊆ план» входит в формулировку «вложенные тела: таблицы ⊆ план + ACL»). Самопроверка grep — расширенная сетка (canon-check): `grep -rni "тул-контур\|free_sql\|validate_free\|--plain\|run_sql\|RunSqlTool\|ToolGateError\|join_edges\|ДВА ребра\|tool_loop\|contour"` по волне файлов.

**✅ Самопроверка:**

```bash
grep -rn "тул-контур\|free_sql\|validate_free\|--plain\|run_sql" docs/adr/ADR-002*.md docs/adr/ADR-005*.md docs/adr/ADR-006*.md docs/prd.md docs/security.md docs/ARCHITECTURE.md docs/RISKS.md docs/adr/README.md docs/observability.md docs/model-card.md docs/diagrams/c4-l3-agent.md docs/diagrams/er-data-model.md   # → остатки только исторические/«удалён»-формулировки
make check    # EXIT=0
git add -A && git commit   # волна канона + D-041
```

### Task 3: D-041 (BL-048 п.3, DECISIONS.md)

Основание «легаси ДЗ-03, не продуктовое требование»; абсолютная заморозка (ссуперседирует D-039 в части механики «отключить+deprecated»); удаление на `inter/hw05-hw06` (решение владельца 2026-11-27; milestone/v4 — точка заморозки ДЗ-05); якоря: ДЗ-03 → milestone/v2 (KI-1, не патчится), ДЗ-04 → milestone/v3 (якорь ДЗ-02 не задаётся — BL-044 закрыт); корпус — регресс FSM; действующие ограничения после удаления: контракт v1 `join_operator='='` (BL-050(c)) + residual вложенных тел, сужаемый правилом «таблицы ⊆ план» (BL-051(a)); механика S31 в истории (`git log -S_assert_tie_pairs`, 45d606a).

**✅ Самопроверка:** `grep -n "D-041" DECISIONS.md` → запись на месте; `make check` EXIT=0.

### Task 4: BL-050(c) — fail-closed `join_operator='='` в считывателе семантики (отдельный коммит)

`neo4j_graph_store.py::path_semantics`: после чтения строк — оператор ≠ `'='` → `SchemaGraphError` (именованная константа поддержанного оператора; формулировка в стиле существующих). Юнит-тест: строка реестра с `join_operator='<'` → SchemaGraphError; контроль `'='` → проходит. `plan_joins_service.py` уже переводит SchemaGraphError в честный отказ до генерации SQL (:321-322 — проверить, не править). Канон-пометки ER/ADR-006 — уже в волне Task 2; в этом коммите — только код+тест.

**✅ Самопроверка:** `uv run pytest src/tests/unit/test_neo4j_graph_store_errors.py -q` → зелёный (новый тест падает на до-правочном коде — проверить stash-прогоном или по построению); `make check` EXIT=0; отдельный коммит.

### Task 5: BL-051(a) — «таблицы ⊆ план» в `_check_skeleton` (отдельный коммит)

`domain/sql_validation.py::_check_skeleton`: все `audit.tables` ⊆ `join_plan.tables` (тела CTE/подзапросов включены — таблица вне плана во вложенном теле = hallucination, ретрай оправдан); формулировка issue в стиле существующих. Два pin-теста residual из Task 1 переворачиваются в 2 регрессионных теста (→ hallucination); контроль — вложенное тело с таблицей плана проходит (уже покрыто `test_exists_subquery_checked`/`test_cte_body_checked_when_root_structure_intact`).

**✅ Самопроверка:** `uv run pytest src/tests/unit/test_validate_sql.py -q` → зелёный (перевёрнутые тесты падали на до-правочном коде — пин OK → теперь hallucination); `make check` EXIT=0; отдельный коммит.

### Task 6: Закрытие сессии

progress.md (Current State/Next Steps/Files Modified), sessions.md строка S39 + «Текущее состояние», BL-048: статус закрыт + «Закрытие», отчёт в файле сессии, REVIEW-GATE (code-reviewer по диапазону BASE..HEAD после зелёного make check), `make close-session S=S39 MSG=…`. **Canon-check**: docs/quality-document.md — строка :12 модуля run_sql_tool: аппендить пометку «удалён S39/D-041» (датируемые evidence не переписывать), актуализировать текущие состояния :10/:23 («--plain живой»/run_agent_flow); опционально одна строка-дополнение в docs/audit-2026-11-23.md «тул-контур удалён S39/D-041» (датированный снапшот, только дополнение). В отчёте сессии и D-041 упомянуть исключение «docs в том же коммите» (removal → волна следом — структура BL-048 §2, окно ограничено сессией, canon-check note).

## Критерии приёмки

- [ ] Removal-коммит: тул-контур удалён одним коммитом (grep-самопроверка чистая), pin-тесты residual в нём же, `make check` EXIT=0
- [ ] Волна канона: 12+ документов ревизированы под факт FSM-единственного контура; feature_list — только тексты feat-013/020/011; states не тронуты (`git diff` states — пусто)
- [ ] D-041 в DECISIONS.md (якоря ДЗ-03→v2, ДЗ-04→v3; ссылки BL-050(c)/BL-051(a)/45d606a)
- [ ] BL-050(c): fail-closed в `path_semantics` + юнит-тест + ER/ADR-006 пометки (в волне Task 2)
- [ ] BL-051(a): «таблицы ⊆ план» в `_check_skeleton` + 2 регрессионных теста
- [ ] 4 коммита (removal / канон+D-041 / BL-050(c) / BL-051(a)) + журнальный коммит close-session; каждый консистентен
- [ ] milestone/v2, milestone/v3, артефакты hw-01…05, `.docs/`, states feature_list — не тронуты
- [ ] `make check` EXIT=0 в конце; сессия закрыта механикой close-session

## Ловушки

1. **ACL-часть корреляции в `_build_audit` НЕ удалять** (кандидаты внешнего скоупа, whole-row, `_resolve_outer_ref`) — её потребляет FSM-ACL (`test_correlated_whole_row_*` кусают); удаляется только построение пар/флага для реестра.
2. `format_retrieval_block`/`RETRIEVAL_TOP_K`/`RETRIEVAL_CONTEXT_MAX_CHARS` живут (потребители `bench_retrieval.py`/`test_hybrid_memory.py`) — не удалить по инерции вместе с промпт-механикой; `_DEMO_SCHEMA_COLUMNS`-синк-тест умирает вместе с картой (каталог-гвард остаётся в test_hybrid_memory).
3. `_COMPLEX_ON`-сентинел НЕ удалять: FSM-сверка скелета полагается на него (USING/без-ON → mismatch со скелетом).
4. `--fsm` флаг сохранить (артефакты hw-05 и эталоны используют его; дефолт теперь тоже FSM) — help обновить.
5. Именованные литералы: поддержанный оператор `join_operator` — константа, не голый `'='` в двух местах (AGENTS.md принцип).
6. Гвард старт-фазы кусает СТАРТУЕМУЮ сессию: шапка ◐ + ARCH-GATE ✅ до первого код-коммита; end-фаза требует обе строки гейтов.
7. Комментарии-якоря file:line в тестах/кодах (например `sql_validation.py:516-520`) после удаления съезжают — в правке ссылаться на символ, не на строку.
8. Пин-тесты residual пишутся с правами, включающими bonuses/campaigns (в дефолтных фикстурах test_validate_sql.py bonuses вне прав — иначе DENIED замаскирует residual).

## Гейты субагентов (заполняется по факту, до закрытия сессии)

- ARCH-GATE: ✅ violations (не блокирует старт) — canon-check архитектора 2026-11-27: архитектура плана канону соответствует (границы слоёв, ADR-002/005/006, deny-by-default, решения владельца); 2 warning-находки — (1) adr/README.md:17 (строка ADR-002 «ДВА ребра… v1.6» вне диапазона :20-22) и (2) quality-document.md:12/:10/:23 (модуль-строка удаляемого тула; требование AGENTS.md End-of-Session) — и notes: security.md §3 целиком (:125-126, сетка grep -i + RunSqlTool|ToolGateError|join_edges|ДВА ребра|tool_loop|contour; observability.md:167-171 — поле contour исчезает из payload), ADR-006 §4.1/4.2 формулировать «fail-closed в считывателе сейчас + пункт наследует BL-026», порядок removal→волна допустим (решение владельца BL-048 §2; исключение упомянуть в отчёте и D-041, окно не выходит за сессию). Находки закрыты дополнением Task 2/Task 6 ДО кода; новый ADR не требуется (прецедент D-033).
- REVIEW-GATE: [заполняется по факту]

## Отчёт по сессии (заполняется по факту)

- Сделано:
  - **Task 1 (removal, коммит `1533749`):** тул-контур удалён одним коммитом по листьям BL-048 п.1 — run_sql_tool.py; validate_free_sql/_check_join_pairs/IR-поля `join_table_pairs`/`unresolved_join_scopes` (ACL-часть корреляции в `_build_audit` СОХРАНЕНА — кандидаты внешнего скоупа, whole-row, `_resolve_outer_ref`; удалена только Registry-часть построения пар); `validate_free` + механика пар/корреляций S31 (`_join_tie_refs`…`_names_connected`, `_COMPARISON_TYPES`; `_COMPLEX_ON` сохранён — FSM-сверка скелета на нём); порт+реализация `join_edges`; cli.py перезаписан FSM-only (сохранены `format_retrieval_block`/`RETRIEVAL_TOP_K` — потребители bench_retrieval/test_hybrid_memory; `--fsm` сохранён, дефолт → `_run_fsm`); container.py; Makefile-комментарий; 5 тест-файлов + wiring-тест + join_edges-тест + FakeSchemaGraphStore удалены; B2-кейсы (2 контур-независимых) мигрированы в FSM-галерею, два флипа вердикта помечены; **pin-тесты residual BL-051(a)** — оба кейса BL-051 ожидают OK с правами на bonuses/campaigns. Самопроверка: grep чистый, make check EXIT=0, 835 passed (было 1002).
  - **Task 2+3 (волна канона + D-041, коммит `aa9dabb`):** 13 документов — ADR-002 v1.7, ADR-005 v1.4, ADR-006 v1.9 (§4.2 пункт гейта BL-050(c) — формулировка «fail-closed в считывателе сейчас + пункт наследует BL-026»), prd v1.9 (FR-2/FR-4/§9), security v2.0 (§3 целиком), ARCHITECTURE (инварианты 2/5), RISKS R-Q1, adr/README (строка ADR-002 :17 — находка canon-check), observability v1.6 (agent.* ретирены, contour снят), model-card (ПДн-пункт сохранён — самопроверка по diff), er v1.11, c4-l3 v1.7; feature_list — только тексты feat-004/011/013/020 (states не тронуты, json-прогон); quality-document — 3 строки (находка canon-check, Task 6); D-041 в DECISIONS.md.
  - **Task 4 (BL-050(c), коммит `a8078e8`):** `SUPPORTED_JOIN_OPERATOR = "="` + fail-closed в `path_semantics` → SchemaGraphError; TDD — тест RED на до-правочном коде, контроль зелёный (FakeConn psycopg).
  - **Task 5 (BL-051(a), коммит `ace5ea7`):** правило «все таблицы запроса ⊆ план» в `_check_skeleton` (квалификация срезается, CTE-имена уже отсечены `_collect_tables`); два pin-теста флипнуты ДО реализации (RED×2) → GREEN; полный корпус 837 passed — ни один легальный кейс не сломан.
- Отклонения от плана:
  - «docs в том же коммите» — волна канона отдельным коммитом после removal (структура BL-048 §2; canon-check note — допустимо, окно = сессия, исключение зафиксировано в D-041 и здесь).
  - ER/ADR-006-пометки BL-050(c) исполнены в волне Task 2 (не отдельным коммитом Task 4) — чтобы ADR-006 не править дважды; зафиксировано в плане сессии заранее (правило №2).
  - `_DEMO_SCHEMA_COLUMNS`-синк-тест (test_retrieval_flow.py) удалён вместе с картой промпта — каталог-гвард остаётся в test_hybrid_memory (план, ловушка 2).
- Перенесено в другие сессии:
  - BL-050 уровень 2 (CHECK-ограничение `status <> 'active' OR join_operator = '='`) — ближайшая миграция V5 (BL-047), решение владельца 2026-11-26.
  - Пометка S35 Task 3 (предмет снят контуром) — валидационная волна п.1(б) плана ветки.
  - Риск-запись «выдуманная связь во вложенном теле» (mitигации после правила) — S36 Task 7; eval-кейс — BL-025.
  - Пуш `inter/hw05-hw06` — по команде владельца (CI не триггерится, D-035).

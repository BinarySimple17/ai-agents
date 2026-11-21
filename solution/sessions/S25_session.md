# S25 — feat-020: объединённый контур ДЗ-03 (retrieval + тул run_sql + стриминг)

Этап: веха V2, ДЗ-03 (усиливающая фича — директива владельца) | Владелец: агент | Зависимости: S02, S03, S06, S13, S19, S21, S23, S24 | Оценка: 1.5 | Статус: ◐ в работе
Фичи: feat-020 — Объединённый контур ДЗ-03: retrieval + тул run_sql + стриминг; dependencies (feat-003, feat-006, feat-013) в passing
Роль по фиче: закрывает feat-020 — финальная сессия, через make verify-feature

## ⚠️ Прочитай перед началом

Директива владельца (2026-11-21, из находок S24): «взять функционал ДЗ-2 и сделать вокруг него ДЗ-3», вариант **B — стриминговый тул-луп с retrieval-контекстом**. Живая проба на шлюзе (LM Studio, qwen3.8-9b, `stream=True` + tools) подтвердила: валидные tool_calls-дельты (id/name/arguments, `finish_reason=tool_calls`, usage терминальным чанком), Hermes-тегов в content нет. Canon-check архитектора (2026-11-21, диспетчеризация в S24): план соответствует канону — вердикт и rulings зашиты в ARCH-GATE ниже; обязательные синки: S00 (стык 2 + Правки) в коммите порта, observability §2.4 в коммите CLI, feature_list behavior-поля в коммите CLI.

Целевой контур: вопрос → retrieval-блок (top_k=8, кап ≈2К токенов, D-022) в системный промпт **тул-контура** (S13-решение «схема и справочники в промпте» продолжается) → цикл ходов на `complete_stream`: дельты текста → stdout по мере генерации; `StreamFinal.tool_calls` непуст → исполнение через `RunSqlTool` (тот же канал agent_ro, LIMIT/timeout/RLS — паритет ДЗ-02) → observation в messages → следующий ход; текстовый финал — ответ (D-022, `_stream_outcome`). Голый тул-луп ДЗ-2 — за флагом `--plain` без изменений (артефакт ДЗ-02, hw-02 README синхронится); флаг `--retrieve` упраздняется (его роль взял дефолт).

Сознательно НЕ делается: перенос цикла в LangGraph FSM (V4 — этот контур его заготовка); Streamlit; справочник стран в глоссарии — делается (Task 4), но BL-010 остаётся открытым до V4 (коды из промпта не убираются, пока тул-контур без retrieval).

### Правила для исполнителя

1. Волны: (1) порт+адаптер+S00-синк, (2) CLI+observability+feature_list-поля, (3) тесты+verify-feature, (4) глоссарий, (5) layer-3+пересъём hw-03/hw-02 README. S00-синк — в том же коммите, что порт (S00 правило 3).
2. Кап ходов — зеркало `MAX_TOOL_CALLS=3` тул-контура + финальный вызов без tools (`cap_exhausted`, ruling ARCH-GATE S13 Q5); константу переиспользовать, второй не заводить. Бюджет — на прогон: BudgetGuard в провайдере кидает ДО вызова → деградация поверх напечатанного (W3).
3. Ход «текст-дельты + tool_calls в одном финале» (легальный поток OpenAI, ruling canon-check Q4): напечатанное остаётся преамбулой, ход исполняется, `agent.answer` не эмитится, ретрай не нужен.
4. `_is_malformed_tool_call` обязателен в объединённом контуре — риск I2 (S19: тег напечатан до распознания) при живых тулах ВЫШЕ, чем был в retrieval («тулов нет — нечего имитировать»).
5. Guard `NO_TEXT_FALLBACK_REASONS` не менять: при переданных тулах `tool_calls`-финал — нормальный ход (консумер-сайв conditioning: tools переданы И final.tool_calls непусты); комментарий константы в порту обновить в волне 1 («а тулов нет» станет неточным).
6. Ruling W1 S19 «стрим только в retrieval-контуре» суперседирован директивой (фиксация в отчёте S24); архивный файл S19 не править.

## Предусловия

- [x] S02, S03, S06, S13, S19, S21, S23, S24 закрыты: таблица в [sessions.md](../sessions.md) — все ✅
- [x] `make check` зелёный (baseline: 237 passed, merge eaa39dd на milestone/v2)
- [x] Фичи-зависимости в state=passing: feat-003, feat-006, feat-013 (feature_list.json); feat-020 зарегистрирована (state=active — активация при старте сессии)
- [x] Сессия СТАРТУЕТ: статус шапки «◐ в работе» и ARCH-GATE резолвлен ДО кода (строка «Гейтов» ниже)

## Задачи

### Task 1: порт — StreamFinal.tool_calls + адаптер-накопитель + S00-синк

Порт ([llm_provider.py](../../src/text_to_sql_agent/application/ports/llm_provider.py)): `StreamFinal` + поле `tool_calls: list[ToolCall] = Field(default_factory=list)` — зеркало `LLMResponse.tool_calls` буферного пути («конкатенация text_delta = LLMResponse.text; StreamFinal.tool_calls = LLMResponse.tool_calls»); `StreamChunk` не меняется. Адаптер ([openai_provider.py](../../src/text_to_sql_agent/infrastructure/llm/openai_provider.py)): накопление tool_calls-дельт стрима (id/name на первой, arguments+= на последующих, склейка по index) в полные ToolCall к финалу; комментарий `NO_TEXT_FALLBACK_REASONS` обновить. S00-синк: строка 83 (порт-таблица) + строка 115 (Стрим) + запись в «Правки».

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit/test_streaming.py -q   # прежние 34 зелёные + новые: tool_calls-дельты → собранный ToolCall в финале; text+tool_calls; abandon без изменений
make check                                           # полный набор зелёный, S00-синк в коммите
```

### Task 2: CLI — объединённый контур (дефолт) + --plain + упразднение --retrieve

[cli.py](../../src/apps/cli.py): `run_agent_flow` (дефолт) — retrieval-блок + SYSTEM_PROMPT-база (схема/справочники S13 + маркировка «ДАННЫЕ/не инструкции» в ОБОИХ источниках), цикл ходов на `complete_stream` по правилам «Прочитай»; `agent.start mode="agent"`, `agent.answer source="agent_stream"`, переиспользование `agent.tool_call`/`agent.observation`/`agent.round`/`agent.cap_exhausted`; `--plain` → прежний `run_tool_loop`; `--retrieve` и `run_retrieval_flow` упраздняются (тесты retrieval-контура адаптируются в Task 3). [container.py](../../src/text_to_sql_agent/composition/container.py) без изменений (все зависимости уже собраны). Observability §2.4 синк: mode `agent`/`tool_loop`, source `agent_stream`/`text`/`final_no_tools`, мёртвые `retrieval`/`retrieval_stream` убрать; feature_list behavior-поля feat-020 — фактические.

**✅ Самопроверка:**

```bash
uv run pytest src/tests/unit -q          # зелёные (адаптированные + новые)
make check                               # полный набор, docs в том же коммите
```

### Task 3: тесты контура + verify-feature

Новый [test_agent_flow.py](../../src/tests/unit/test_agent_flow.py) (S00 правило 7 — моки): happy path (стрим SQL → tool_calls → observation → стрим ответа с числом), ход text+tool_calls (ruling 3), кап ходов → финал без tools, пустой/битый ответ до первой дельты (empty-retry), сбой mid-stream (деградация поверх напечатанного), malformed-тег (I2), недоступность retrieval (правило 8), --plain wiring (капси, как в test_tool_loop). Адаптация test_streaming (tool_calls-сценарии) и test_retrieval_flow → сливается в test_agent_flow или остаётся для format_retrieval_block.

**✅ Самопроверка:**

```bash
make verify-feature F=feat-020   # PASS: state=passing, evidence записан скриптом
make check                       # полный набор зелёный
```

### Task 4: справочник стран в глоссарии

[docs/glossary.md](../../docs/glossary.md): секция `## [термин: справочник стран] — таблица customers` (коды RU/KZ/DE/BY из сида + статусы заказов; формат индексатора, guard объявленных таблиц) → `make index-knowledge` (21 → 22 документа).

**✅ Самопроверка:**

```bash
make index-knowledge   # upsert идемпотентен, новый source_ref в корпусе
```

### Task 5: layer-3 + пересъём артефактов hw-03 / синк hw-02

Живые прогоны на LM Studio: сценарий ДЗ-03 дефолтной командой (реальное число в стриме, этапы в stderr, --quiet чистит stderr, пайп | cat); пересъём [hw-03/](../../hw-03/README.md) артефактов (stream-*.txt: новый stdout/stderr/timeline/quiet) + README (команда без --retrieve, факты прогонов, флаг --plain упомянут); hw-02 README — команда тул-контура с --plain.

**✅ Самопроверка:**

```bash
uv run --env-file .env python -m apps.cli --user u_logist "Сколько клиентов из РФ пришло из рекламных кампаний всего?"   # стрим + реальное число
uv run --env-file .env python -m apps.cli --plain --user u_logist "Сколько всего заказов в базе?"                        # ДЗ-2 поведение сохранено
```

### Task 6: закрытие сессии

REVIEW-GATE (code-reviewer на диапазон сессии) → End of Session по AGENTS.md: sessions.md (строка S25, «Текущее состояние»), progress.md, quality-document (cli/ + services/llm), make archive, make clean-check, merge feat-020-agent-streaming → milestone/v2 (D-024).

**✅ Самопроверка:**

```bash
make check        # зелёный
make clean-check  # чистое состояние, гейты обеих фаз ✅
```

## Критерии приёмки

- [ ] feat-020 в state=passing с evidence (`make verify-feature F=feat-020`), behavior-поля соответствуют факту
- [ ] Дефолтный прогон CLI: вопрос → стрим ходов → исполнение run_sql → финальный ответ с реальным числом на stdout (D-022); `--plain` сохраняет поведение ДЗ-2; `--retrieve` отсутствует
- [ ] Кап ходов S13 переиспользован (без второй константы); ход text+tool_calls обработан по ruling 3; malformed-тег не доходит до пользователя как ответ
- [ ] S00 (стык 2 + Правки) и observability §2.4 синхронизированы в своих коммитах; глоссарий проиндексирован
- [ ] layer-3: артефакты hw-03 пересняты на новом контуре, hw-02 README синхронизирован
- [ ] REVIEW-GATE: code-reviewer по диапазону сессии, Critical/Important закрыты; отчёт заполнен; sessions.md/progress.md обновлены; make archive выполнен

## Ловушки

1. Ход «текст+tool_calls в одном финале» — забытый случай: напечатанная преамбула остаётся, ход исполняется, agent.answer не эмитится (ruling 3).
2. I2-риск с тула́ми: Hermes-тег может напечататься до распознания — `_is_malformed_tool_call` на финале обязателен; возврат зеркалит экран (S19-семантика).
3. Бюджет — на прогон: BudgetGuard в провайдере кидает ДО вызова шлюза; в цикле ходов это «деградация поверх напечатанного», не traceback (W3).
4. Шлюз без stream_options include_usage — `usage_missing=true` (I1 S19): консумер не полагается на usage в финале.
5. verify-feature требует state=active — активация сделана при старте; state=passing ставит ТОЛЬКО скрипт.
6. Гейты субагентов двухфазные (D-023/D-027): start-фаза уже пройдена (ARCH-GATE ниже); end-фаза проверит обе строки при закрытии — не удалять раздел.

## Гейты субагентов (заполняется по факту, до закрытия сессии)

- ARCH-GATE: ✅ compliant (canon-check architect 2026-11-21, диспетчеризация в S24-разведке; вердикт «по существу план канону соответствует»): (1) расширение StreamFinal полем tool_calls — прецедент D-021/W2 «расширение полями», отдельный чанк-тип StreamToolCallDelta отвергнут (ломает контракт генератора, дельты консумеру не нужны); (2) guard NO_TEXT_FALLBACK_REASONS — консумер-сайв conditioning, константу не менять; (3) события — переиспользование agent.* (новые значения mode/source, не имена — D-021-аналог) + синк observability §2.4; (4) кап ходов — зеркало MAX_TOOL_CALLS=3 + финал без tools (S13 Q5), бюджет на прогон через провайдера; (5) ход text+tool_calls — ruling в «Правилах» 3; (6) ADR-002 не затронут (модель выбирает тул, не узел — прецедент S13); blocker S24 (перепривязка ARCH-GATE) закрыт — S24 закрыта до старта S25; обязательные синки S00/observability/feature_list — в волнах задач
- REVIEW-GATE: ✅ With fixes (code-reviewer 2026-11-21, диапазон 2ccfc01..4133322 — волны 1-3; 251 passed, make check exit=0): 0C/2I/12m. Important-2 закрыта фикс-волной a1c9d6f — точная OpenAI-последовательность истории второго хода (system, user, assistant(content=None, tool_calls), tool(tool_call_id)) + заморозка сообщений в фейке (per-call ассерты). Important-1 (hw-02/hw-03 README ссылаются на упразднённый --retrieve) — плановая Task 5, ОБЯЗАНА закрыться до merge/закрытия сессии (критерий 5). Minor'ы 1-10, 12 закрыты в a1c9d6f (docstrings возврата, \n-сепаратор преамбулы, тест ruling 3×4, поведенческий пин капа, стрим без финала → текст, _consume_stream без дубля + printed⟺printer∧chunks, синтез id call-stream-N, reasoning не печатается на тул-ходе, observability non-terminal degrade, RETRIEVAL_UNAVAILABLE_NOTE, flow-половина регресса C1 на новом контуре); Minor-11 (quality-document cli-строка) — End of Session шаг 7

## Отчёт по сессии (заполняется по факту)

- Сделано:
  - <по факту>
- Отклонения от плана:
  - <по факту>
- Перенесено в другие сессии:
  - <по факту>

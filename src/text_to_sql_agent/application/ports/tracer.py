"""Порт `Tracer` — журнал событий узлов/адаптеров (FR-11 node_timings; S00 таблица портов).

Реализация S03 — `infrastructure/observability/jsonl_tracer.py` (JSONL вне git,
путь из `Settings.trace_jsonl_path`); стек OTel — ADR-010, вне скоупа S03.
События `agent.*` — этапы цикла CLI (S21/BL-004, D-022): имена и лимит превью —
константы ниже, единый источник для эмиттера (apps.cli) и рендерера
(ConsoleTracer) — ревью S21 Important-1/2.
"""

from typing import Protocol, runtime_checkable

from text_to_sql_agent.domain.entities import Trace

EVENT_AGENT_START = "agent.start"  # вопрос принят (question, trace_id, mode)
EVENT_AGENT_ROUND = "agent.round"  # вызов модели (call — порядковый номер)
EVENT_AGENT_TOOL_CALL = "agent.tool_call"  # модель позвала тул (name, sql)
EVENT_AGENT_OBSERVATION = "agent.observation"  # результат тулa / отказ (см. observability §2.4)
EVENT_AGENT_EMPTY_RETRY = "agent.empty_retry"  # пустой/битый ответ — повтор (left)
EVENT_AGENT_CAP_EXHAUSTED = "agent.cap_exhausted"  # кап ходов — финальный вызов без тулов
EVENT_AGENT_DEGRADE = "agent.degrade"  # деградация-исход (reason)
EVENT_AGENT_ANSWER = "agent.answer"  # финальный ответ (source)
EVENT_AGENT_RETRIEVE = "agent.retrieve"  # retrieval контура ДЗ-03 (docs, source_refs)
EVENT_GRAPH_NODE = "graph.node"  # FSM: содержательный узел вошёл (node; S04, ADR-002 §2)
# FSM resolve_access: сбой чтения прав — деталь сбоя оператору (JSONL), не
# пользователю (S9/ревью S05 Important-1: отказ в state — фиксированный текст)
EVENT_RESOLVE_RIGHTS_UNAVAILABLE = "resolve.rights_unavailable"
# FSM clarify (S08): вердикт классификатора + приостановка на уточняющем вопросе
# (interrupt + checkpointer; оператору — сцена «вопрос ждёт ответа»). Событие
# clarify.interrupt пишет РАННЕР (факт паузы; resume-визит узла ре-исполняется —
# узел не отличает паузу от продолжения). Единственный interrupt-узел канона —
# clarify (state-agent v1.6); появление других interrupt-узлов — переименование
# в graph.interrupt (ревью S08 Minor-10)
EVENT_CLARIFY_VERDICT = "clarify.verdict"
EVENT_CLARIFY_INTERRUPT = "clarify.interrupt"
# FSM clarify (S08): сбой классификации (LLM/retrieval) — деталь сбоя оператору
# (JSONL), не пользователю (S9-политика — прецедент resolve.rights_unavailable)
EVENT_CLARIFY_UNAVAILABLE = "clarify.unavailable"
# FSM (S08, ревью Important-2): сбой хранилища сессий (Valkey) — деталь оператору,
# не пользователю; раннер переводит в валидный fail-исход
EVENT_CHECKPOINT_UNAVAILABLE = "checkpoint.unavailable"
# FSM plan_joins (S10): вердикт планера (исходы PlanJoinsResult) + сбой ноги
# (retrieval/граф/ранжирование) — деталь оператору в JSONL, не пользователю (S9);
# payload — факты (outcome/graph_version/candidate_tables), без текстов плана
EVENT_PLAN_JOINS_VERDICT = "plan_joins.verdict"
EVENT_PLAN_JOINS_UNAVAILABLE = "plan_joins.unavailable"
# FSM build_skeleton (S10): дефект каркаса на узле чистого кода (план не дошёл/
# не валиден) — fail-closed в degrade, деталь дефекта оператору (S9-прецедент)
EVENT_SKELETON_UNAVAILABLE = "skeleton.unavailable"
# FSM generate_sql (S10): вердикт генератора (sql/fail, эскалация M1) + сбой —
# payload факты (outcome/llm_calls/escalated/tables), БЕЗ SQL-текста (D-022-аналог)
EVENT_GENERATE_SQL_VERDICT = "generate_sql.verdict"
EVENT_GENERATE_SQL_UNAVAILABLE = "generate_sql.unavailable"
# FSM validate_sql (S11): вердикт рубежа безопасности (ok/hallucination/denied —
# ADR-005 §4) + сбой (дефект каркаса/недоступный каталог — fail-closed в degrade,
# деталь оператору); payload факты (outcome/issue-счёт/enforced_limit), БЕЗ
# SQL-текста (D-022-аналог)
EVENT_VALIDATE_SQL_VERDICT = "validate_sql.verdict"
EVENT_VALIDATE_SQL_UNAVAILABLE = "validate_sql.unavailable"
# FSM diagnose_error (S14): вердикт разбора (класс/подсказка в state.diagnosis,
# futile → degrade без ретрая — ADR-005) + сбой разбора (шлюз/M1 — ретрай не
# назначается, деталь оператору); payload факты (error_class/retry_futile/
# llm_calls), БЕЗ SQL-текста (D-022-аналог)
EVENT_DIAGNOSE_VERDICT = "diagnose.verdict"
EVENT_DIAGNOSE_UNAVAILABLE = "diagnose.unavailable"
# FSM execute_sql (S14): факт COUNT-пробы при 0 строк (state-agent v1.7 — probe
# не узел, код визита execute_sql); payload факты (tables/counts/verdict), БЕЗ
# SQL-текста (D-022-аналог)
EVENT_EXECUTE_PROBE = "execute.probe"
# FSM execute_sql (S14): fail-closed дефект каркаса (identity/sql/скелет — подмена)
# или сбой самой COUNT-пробы — деталь оператору, не пользователю (S9-прецедент
# skeleton.unavailable); маршрут — degrade без diagnose (FLAG_EXECUTE_DEFECT)
EVENT_EXECUTE_UNAVAILABLE = "execute.unavailable"
# FSM compact_result (S15): код-узел компакции — факт свёртки (row_count/сэмпл/
# агрегаты-счёт) для операторского следа; payload факты, БЕЗ строк данных
# (JSONL-приватность — R7 ARCH-GATE S15)
EVENT_COMPACT_VERDICT = "compact.verdict"
# FSM interpret_result (S15): вердикт финального объяснения (strong) + сбой;
# payload факты (outcome/flags/cost_rub/row_count), БЕЗ текста ответа и БЕЗ
# строк данных (R7 ARCH-GATE S15)
EVENT_INTERPRET_VERDICT = "interpret.verdict"
EVENT_INTERPRET_UNAVAILABLE = "interpret.unavailable"
# FSM degrade (S15): отказ по правам бизнес-понятиями (mid + санитайзер R1);
# payload факты (cause/has_variant), текст отказа не дублируется; reason —
# llm/budget у сбоя (R5 ARCH-GATE S15)
EVENT_DEGRADE_VERDICT = "degrade.verdict"
EVENT_DEGRADE_UNAVAILABLE = "degrade.unavailable"

# --- Семья llm.* (BL-005, S15 Task 4): единый источник имён для эмиттера
# (openai_provider) и рендерера (console_tracer) — прецедент EVENT_AGENT_*/graph.* ---
EVENT_LLM_COMPLETE = "llm.complete"
EVENT_LLM_RETRY = "llm.retry"
EVENT_LLM_TIMEOUT = "llm.timeout"
EVENT_LLM_REJECT = "llm.reject"
EVENT_LLM_BREAKER_OPEN = "llm.breaker_open"
EVENT_LLM_BUDGET_STOP = "llm.budget_stop"

# BL-004 (требование владельца): результат тулa показывается строками данных,
# хоть обрезанными; единый контракт эмиссии (apps.cli) и рендера (ConsoleTracer)
OBSERVATION_PREVIEW_ROWS = 5


@runtime_checkable
class Tracer(Protocol):
    def record(self, trace: Trace) -> None:
        """Записать одно событие наблюдаемости (fmt-нейтрально для адаптера OTel)."""
        ...

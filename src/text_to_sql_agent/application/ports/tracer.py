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

# BL-004 (требование владельца): результат тулa показывается строками данных,
# хоть обрезанными; единый контракт эмиссии (apps.cli) и рендера (ConsoleTracer)
OBSERVATION_PREVIEW_ROWS = 5


@runtime_checkable
class Tracer(Protocol):
    def record(self, trace: Trace) -> None:
        """Записать одно событие наблюдаемости (fmt-нейтрально для адаптера OTel)."""
        ...

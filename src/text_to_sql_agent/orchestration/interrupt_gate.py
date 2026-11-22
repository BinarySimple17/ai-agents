"""Адаптер `InterruptGate` на LangGraph (ruling R2/R3 ARCH-GATE S08).

Граница движка — здесь единственное место, где механика ``interrupt()`` видна
как код (ADR-002 §5: LangGraph знают только orchestration/composition; прецедент
формы — runner.py). Узел clarify (application) вызывает порт ``ask``; пауза
прогона и доставка ответа резюмированным прогоном — работа checkpointer'а
(Valkey, TTL 10 мин — S00 стык 1), который подключает compile() в graph.py.
"""

from langgraph.types import interrupt


class LangGraphInterruptGate:
    """Реализация порта InterruptGate: interrupt() с вопросом, ответ — resume-значение.

    Первый вызов в исполнении узла приостанавливает прогон (GraphInterrupt);
    при resume по тому же thread_id ре-исполнение узла доходит до interrupt()
    и получает ответ пользователя значением — второй LLM-вызов классификации
    недопустим (ruling R6 ARCH-GATE S08).
    """

    def ask(self, question: str) -> str:
        return interrupt(question)

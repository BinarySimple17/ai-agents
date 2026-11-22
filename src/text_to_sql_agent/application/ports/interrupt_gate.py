"""Порт `InterruptGate` — интерактивная пауза содержательного узла (ruling R2/R3
ARCH-GATE S08, ADR-002 §5).

Application не знает движок графа (LangGraph знают только orchestration/composition):
узел clarify «задаёт вопрос пользователю и ждёт ответа» через этот порт. Реализация
S08 — ``orchestration/interrupt_gate.py`` (LangGraphInterruptGate: ``interrupt()`` +
checkpointer-сессия в Valkey, TTL 10 мин — S00 стык 1); подмена в тестах — фейк
(возвращает заготовленный ответ, S00 правило 7).

Контракт: ``ask`` приостанавливает прогон до ответа пользователя; возвращённый
string — ответ, резюмирующий ТОТ ЖЕ прогон (тот же thread — Ловушка 2 S08).
Адаптер обязан быть вызван только внутри исполнения узла графа (механика
``interrupt()`` LangGraph).
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class InterruptGate(Protocol):
    def ask(self, question: str) -> str:
        """Задать пользователю один уточняющий вопрос; вернуть его ответ.

        Реализация приостанавливает прогон (interrupt + checkpointer); при
        resume ответ приходит значением — второй вызов LLM/вычислений узлом
        недопустим (ruling R6 ARCH-GATE S08).
        """
        ...

"""Порт-модуль сессий checkpointer'а (ruling R2 ARCH-GATE S08): ошибка стыка.

Контракт сейвера — ``BaseCheckpointSaver`` (LangGraph): application знать движок
не должна (ADR-002 §5), поэтому интерфейс не дублируется портом — здесь только
ОШИБКА СТЫКА: сбой хранилища сессий (Valkey недоступен, деградация соединения).
Адаптер (``infrastructure/cache/valkey_checkpointer.py``) обязан оборачивать в
неё сырые redis-ошибки; раннер (orchestration — граница движка) ловит её и
переводит в валидный fail-исход (S00 правило 8: «не traceback», ревью S08
Important-2 — сбой Valkey в FSM-пути выходил наружу).

Порт-модуль без Protocol — обоснование: сейвер передаётся в compile() движка
как объект, duck-typing контракта задан langgraph; стыковая ошибка —
проектный контракт (прецедент: ``VectorStoreError`` живёт в модуле порта).
"""


class CheckpointStoreError(Exception):
    """Сбой хранилища сессий (Valkey): соединение/операция — человекочитаемое
    сообщение, не сырая redis-ошибка."""

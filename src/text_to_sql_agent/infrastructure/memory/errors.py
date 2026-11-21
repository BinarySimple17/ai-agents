"""Общий формат сообщений стыков памяти знаний (едино для адаптеров).

Сообщения ошибок стыка (VectorStoreError) — человекочитаемая строка, не
простыня psycopg-хинтов; единый хелпер у адаптера и сборщиков — чтобы формат
не разъехался при правке (m8/new-m1 ревью S26).
"""

_ERROR_MESSAGE_MAX_CHARS = 300


def shorten_error_message(message: str) -> str:
    message = " ".join(message.split())
    if len(message) <= _ERROR_MESSAGE_MAX_CHARS:
        return message
    return message[: _ERROR_MESSAGE_MAX_CHARS - 1] + "…"

"""DegradationCase — типизированный вердикт-причина отказа (feat-015, ruling R8).

Код собирает причину из кодовых каналов state (validation.outcome — S11,
route_flags — routing, join_plan/permissions) ДО промпта: сырые
route_flags/validation-словари и «чужие» имена в промпт не идут — модель
формулирует отказ по ситуации и доступным темам (ADR-005:171 «код фиксирует,
чего не хватает; LLM формулирует»).

``fallback_text`` — детерминированные шаблоны отказа (без LLM): носитель
санитайзер-нарушений, недоступности шлюза (NFR-8) и сбоев формулировки —
живой остаток «degrade-кода» ADR-007/hw-01 (ruling R1 ARCH-GATE S15).

Без IO, без LLM (границы domain).
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class DegradationCause(StrEnum):
    """Причина деградации — носитель маршрута уже решён (degrade без retry)."""

    RIGHTS = "rights"  # вне прав (validate_sql denied, ADR-005 — сигнал безопасности)
    NO_PATH = "no_path"  # «нет активного пути» (plan_joins, заявка подана S10)
    GENERATOR = "generator"  # отказ генератора (исчерпание M1/бюджет — ruling R2 S10)
    TECHNICAL = "technical"  # дефект каркаса / лимит / дедлайн (S9: без деталей)


class DegradationCase(BaseModel):
    """Вердикт-причина + темы для формулировки (промпт строит сервис из него).

    ``situation`` — бизнес-формулировка ситуации (код, без имён недоступных
    таблиц); ``available_topics`` — доступные пользователю темы (свои имена —
    ADR-005:78 «граница знаний = граница прав»); ``forbidden_topics`` —
    недоступные имена ТОЛЬКО для санитайзера (R1): в промпт не попадают
    (R8 — модель не может «спросить» то, чего не видела).
    """

    model_config = ConfigDict(frozen=True)

    cause: DegradationCause
    situation: str = Field(min_length=1)
    available_topics: tuple[str, ...] = ()
    forbidden_topics: tuple[str, ...] = ()


# --- Шаблоны отказа (код, S9-инварианты по построению) ---

_RIGHTS_TEMPLATE = "Часть данных по этому вопросу недоступна вашей роли."
_RIGHTS_VARIANT_SUFFIX = " Доступны данные по темам: {topics} — могу показать по ним."
_NO_PATH_TEMPLATE = (
    "Данные по этому вопросу пока не связаны в базе — заявка на связь уже "
    "передана владельцу справочника. Попробуйте спросить по одной теме: {topics}, "
    "или повторите вопрос позже."
)
_NO_PATH_NO_TOPICS = "Данные по этому вопросу пока не связаны в базе — повторите вопрос позже."
_GENERATOR_TEMPLATE = (
    "Не удалось подготовить запрос к данным — попробуйте переформулировать "
    "вопрос или повторите позже."
)
_TECHNICAL_TEMPLATE = (
    "Запрос не выполнен из-за технической ошибки — попробуйте повторить вопрос позже."
)


def fallback_text(case: DegradationCase) -> str:
    """Шаблонный отказ по причине (0 вызовов LLM; S9-инварианты по построению)."""
    if case.cause is DegradationCause.RIGHTS:
        if case.available_topics:
            topics = ", ".join(case.available_topics)
            return _RIGHTS_TEMPLATE + _RIGHTS_VARIANT_SUFFIX.format(topics=topics)
        return _RIGHTS_TEMPLATE
    if case.cause is DegradationCause.NO_PATH:
        if case.available_topics:
            return _NO_PATH_TEMPLATE.format(topics=", ".join(case.available_topics))
        return _NO_PATH_NO_TOPICS
    if case.cause is DegradationCause.GENERATOR:
        return _GENERATOR_TEMPLATE
    return _TECHNICAL_TEMPLATE

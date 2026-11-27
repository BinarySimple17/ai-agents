"""Тест-инвариант капа LLM-вызовов (E6a аудита 2026-11-23, S33 Task 4).

Ruling ARCH-GATE S33 (consult архитектора): кап — ВЕРХНЯЯ ГРАНИЦА НОРМАЛЬНОГО
ПРОГОНА + запас под одну внутреннюю волну M1, НЕ бюджет худшей комбинации
ретраев (ADR-007 §4.1). Числа выводятся из констант политики (m1_policy,
routing) и дефолта Settings — не хардкодятся в формулах; замена знака в
intent-пинах = пересмотр канона (ADR-007 §4.1, ruling E6a S33), не правка
теста.

Расчёт достижимых путей (все узлы без LLM не считать: validate/check_cost/
skeleton/compact — код):
- нормальный прогон (hw-05 happy-path): clarify 1 + plan_joins 1 +
  generate 1 + interpret 1 = 4;
- одна внутренняя волна M1 (один визит одного узла израсходовал MAX_ATTEMPTS
  попыток, успех на последней; остальные — с первой) + её ретрай-визит
  генератора: 4 + (MAX_ATTEMPTS − 1) + 1 = 7 → кап 8 = 7 + запас 1;
- наблюдаемые деград-сцены hw-05 после S33 (diagnose-подсказки semantics —
  код, без LLM; трейсы — hw-05/fsm-degrade-*-stderr.txt, Task 6): ≈ 5;
- худшие достижимые пути (каждый узел — успех с последней попытки; все
  визиты clarify ×2 раунда по MAX_ATTEMPTS, генератор — MAX_NODE_RETRIES
  визитов, между ретраями — полные волны diagnose):
  done = 6 + 3 + 5 + 6 + 1 = 21; degrade (финальный diagnose исчерпан,
  fuse) = 24. Превышение капа — НАМЕРЕННО: (а) дорогие ветви раньше срезает
  ₽-гейт (ADR-007 §4.3 — максимум одна эскалация SQL на прогон), (б) дешёвые
  штормы ретраев ₽-кап не видит — ранний срез делает только кап вызовов,
  (в) терминал — честный budget_stop (incomplete, S15), не сбой.
"""

from tests.unit.test_settings import REQUIRED_ENV
from text_to_sql_agent.application.services.m1_policy import MAX_ATTEMPTS
from text_to_sql_agent.config.settings import Settings
from text_to_sql_agent.orchestration.routing import MAX_NODE_RETRIES

# Свод путей — из констант политики (единый источник: правка MAX_ATTEMPTS /
# fuse biting тест при рассинхроне с ADR-007 §4.1)
_NORMAL_PATH_CALLS = 1 + 1 + 1 + 1  # clarify + plan_joins + generate + interpret
_WAVE_EXTRA_CALLS = MAX_ATTEMPTS - 1  # волна: две неудачи, успех на третьей
_GENERATOR_RETRY_CALLS = 1  # FSM-ретрай-визит генератора — ровно один вызов
_CLARIFY_VISITS = 2  # начальный + interrupt-resume (routing.route_clarify)
_GENERATOR_VISITS = MAX_NODE_RETRIES  # fuse генератора: первый визит + ретраи
_GENERATOR_MAX_CALLS = MAX_ATTEMPTS + (_GENERATOR_VISITS - 1) * _GENERATOR_RETRY_CALLS
_DIAGNOSE_CALLS_PER_VISIT = MAX_ATTEMPTS  # внутренние M1-попытки разбора
# Наблюдаемые деград-сцены hw-05 после S33 (трейсы Task 6: diagnose visits
# 3 × «вызовов 0»; суммарно ~5 вызовов на сцену) — константа с якорем на трейсы
_HW05_DEGRADE_SCENE_CALLS = 5


def _path_one_wave() -> int:
    """Нормальный прогон + одна волна M1 в любом узле + её ретрай генератора.

    Волна может сесть в clarify-v1/v2, plan_joins, generate-v1 или diagnose
    (execute timeout/empty — после S33 единственные LLM-диагнозы на пути
    done): максимум = норма + (MAX_ATTEMPTS − 1) + 1 ретрай-визит.
    """
    return _NORMAL_PATH_CALLS + _WAVE_EXTRA_CALLS + _GENERATOR_RETRY_CALLS


def _path_worst_done() -> int:
    """Худший достижимый done-путь: все узлы — успех с последней попытки.

    clarify ×2 визита по MAX_ATTEMPTS + plan MAX_ATTEMPTS + генератор
    MAX_NODE_RETRIES визитов (первый полный + ретраи по 1) + diagnose между
    ретраями (полные волны) + interpret.
    """
    return (
        _CLARIFY_VISITS * MAX_ATTEMPTS
        + MAX_ATTEMPTS  # plan_joins
        + _GENERATOR_MAX_CALLS
        + (_GENERATOR_VISITS - 1) * _DIAGNOSE_CALLS_PER_VISIT  # diagnose между ретраями
        + 1  # interpret
    )


def _path_worst_degrade() -> int:
    """Худший достижимый degrade-путь: то же + финальный diagnose (fuse
    исчерпан, вердикта нет) + формулировка отказа degrade."""
    return _path_worst_done() + _DIAGNOSE_CALLS_PER_VISIT + 1  # финальный diagnose + degrade


def test_cap_covers_normal_run_with_margin() -> None:
    cap = Settings(**REQUIRED_ENV, _env_file=None).fsm_max_llm_calls
    assert _NORMAL_PATH_CALLS <= cap - 1  # норма никогда не режется гвардом


def test_cap_covers_single_m1_wave_with_margin() -> None:
    # Обоснование значения 8 (ruling E6a S33): кап = норма + одна волна + запас 1
    cap = Settings(**REQUIRED_ENV, _env_file=None).fsm_max_llm_calls
    assert _path_one_wave() == MAX_ATTEMPTS + 4  # 7 при MAX_ATTEMPTS=3 — расчёт, не хардкод
    assert _path_one_wave() <= cap - 1


def test_cap_covers_observed_hw05_degrade_scenes() -> None:
    # Сцены hw-05 (Task 6) после S33 укладываются с запасом — E6a «8 из 8»
    # был артефактом до-фиксового diagnose-LLM на детерминированных повторах
    cap = Settings(**REQUIRED_ENV, _env_file=None).fsm_max_llm_calls
    assert _HW05_DEGRADE_SCENE_CALLS <= cap - 1


def test_cap_intentionally_cuts_worst_achievable_paths() -> None:
    # Intent-pin (ruling E6a S33): худшие достижимые пути ПРЕВЫШАЮТ кап —
    # ранний срез штормов — назначение гварда (ADR-007 §4.1); замена знака
    # здесь = пересмотр канона, не правка теста
    cap = Settings(**REQUIRED_ENV, _env_file=None).fsm_max_llm_calls
    assert _path_worst_done() > cap
    assert _path_worst_degrade() > cap


def test_guard_budget_stop_is_honest_terminal_contract() -> None:
    # Терминал среза — budget_stop с incomplete=true (S15), не сбой: контракт
    # честного частичного ответа (ADR-007 §4.1, state-agent «неполное не
    # выдаётся за полное») — пин связи капа с semantics budget_stop
    from text_to_sql_agent.orchestration.routing import FSMGuardCaps, llm_guard

    settings = Settings(**REQUIRED_ENV, _env_file=None)
    caps = FSMGuardCaps(
        max_llm_calls=settings.fsm_max_llm_calls,
        budget_rub=settings.budget_per_request_rub,
    )
    state = {"budget_used": {"llm_calls": _path_worst_degrade()}, "route_flags": {}}
    assert llm_guard(state, caps) == "budget_stop"  # срез, не проход
    state_ok = {"budget_used": {"llm_calls": _NORMAL_PATH_CALLS}, "route_flags": {}}
    assert llm_guard(state_ok, caps) is None  # норма проходит

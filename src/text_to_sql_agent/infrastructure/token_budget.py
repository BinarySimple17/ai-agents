"""Адаптер BudgetGuard поверх резидентного аккумулятора S03 (feat-015, S15).

Единственный учёт расхода остаётся в адаптере шлюза (решение ARCH-GATE S03,
S00 стык 2: «исключение до вызова» поднимает адаптер) — guard лишь read-side
зеркало: composition инжектирует callables над `OpenAIProvider`
(`budget_used_rub` / `reset_request_budget`), не расширяя порт LLMProvider
(ruling R4 ARCH-GATE S15). Определение «попытки узла» для учёта — ruling
R6 ARCH-GATE S14 (application.ports.budget_guard docstring).
"""

from collections.abc import Callable


class TokenBudgetGuard:
    """Read-side снапшот расхода прогона: spent/remaining/reset (порт S15)."""

    def __init__(
        self,
        *,
        cap_rub: float,
        spent_getter: Callable[[], float],
        resetter: Callable[[], None],
    ) -> None:
        self._cap_rub = cap_rub
        self._spent_getter = spent_getter
        self._resetter = resetter

    def spent_rub(self) -> float:
        """Фактический расход по usage вызовов прогона (S03-аккумулятор)."""
        return round(self._spent_getter(), 6)

    def remaining_rub(self) -> float:
        """Остаток до капа запроса; floor 0 — округления usage не дают
        отрицательного остатка эскалациям M1 (резерв 0,6 ₽, ADR-007 §4.3)."""
        return max(self._cap_rub - self._spent_getter(), 0.0)

    def reset(self) -> None:
        """Сброс пер-запросного аккумулятора (обязательство S04 — тот же метод)."""
        self._resetter()

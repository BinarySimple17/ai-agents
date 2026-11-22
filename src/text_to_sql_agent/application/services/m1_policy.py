"""Политика ретраев M1 (D-016, ADR-007 §4.1) — общие константы генерирующих узлов.

Третий потребитель (generate_sql, S10) — унификация, предсказанная комментарием
констант clarify (S08): значения канона («≤ 3 попыток на узел», «эскалация на
попытке 3», «резерв 0,6 ₽ под interpret_result») не должны разъезжаться между
план_joins/clarify/generate_sql. Роль попытки выбирает СЕРВИС из этих констант
(прецедент S09; FSM-рёбра ретрая через diagnose_error активирует S14 — ruling
R7 ARCH-GATE S10: сервис принимает стартовую роль/попытку параметром).
"""

from text_to_sql_agent.application.ports.llm_provider import Role

# Три попытки на узел (S00 стык 1; per-node fuse routing.MAX_NODE_RETRIES)
MAX_ATTEMPTS = 3
# Номер попытки с эскалацией роли вверх (D-016: попытка 3 — уровень вверх)
ESCALATION_ATTEMPT = 3
# Резерв бюджета: эскалация только при остатке ВЫШЕ резерва (ADR-007 §4.3;
# после эскалации прогон сохраняет запас на interpret_result)
ESCALATION_RESERVE_ROUBLES = 0.6
# D-016: попытка 3 — роль вверх (cheap→mid→strong); вершина — сама на себя
ROLE_UP: dict[Role, Role] = {"cheap": "mid", "mid": "strong", "strong": "strong"}

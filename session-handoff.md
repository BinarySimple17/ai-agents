# Session Handoff

<!-- ЗАПОЛНЯЕТСЯ В КОНЦЕ КРУПНОЙ СЕССИИ: снимок состояния для передачи
     следующей сессии. Для мелких задач достаточно обновить progress.md. -->

## Current Objective

- Goal: S05/feat-005 resolve_access — первый содержательный узел FSM: права из таблиц настроек + DevTokenAuth (веха V4, ДЗ-05)
- Current status: код + тесты собраны и закоммичены; **REVIEW-GATE pending** — code-reviewer оборван дневным лимитом API-ключа (75,18/75,00 ₽); решение владельца (2026-11-21) — пауза, континуация 2026-11-22
- Branch / commit: `feat-005-resolve-access`, **1aa3aa0** (на вершине milestone/v4 @ 298e36c)

## Completed This Session

- [x] ARCH-GATE: canon-check архитектора → verdict **violations** → rulings R1–R6 закрыты до кода (R1 гранты 0002 достаточны; R2 порт `PermissionStore`, не AccessPolicy; R3 ОБА счётчика settings_version; R4 identity-проводка verify→state; R5 CSV/'\*' на границе; R6 rejected через существующее ребро, graph_version=0 до S07)
- [x] Task 1: порт `AuthProvider` + `DevTokenAuth` (compare_digest, fail-closed), `Settings.agent_dev_tokens`/`dev_token_map` (fail-fast), CLI `--fsm --token` — verify ДО graph.invoke
- [x] Task 2: порт `PermissionStore` + `PgPermissionStore` (agent_planner, CSV-нормализация, `'*'` литералом, `RightsUnavailableError`)
- [x] Task 3: `ResolveAccessService` — кэш per-user со сверкой ОБЕИХ версий `'permissions'`+`'user_roles'` (дыра revoke)
- [x] Task 4: узел fail-closed (identity_missing/unknown_user/rights_unavailable → `FLAG_IDENTITY_FAILED` → terminal_rejected, routing не тронут); подмена через `node_overrides` в composition; тесты через Container
- [x] Синк: AgentState.user_id + initial_state + AnswerQuestion.run; S00 (стык 1 + порт), ARCHITECTURE, .env.example
- [x] Тесты **456 passed** (+39); layer-3: `--fsm --token dev-analyst-token` → done (10 узлов); без/чужой токен → rejected exit 1

## Verification Evidence

| Check | Command | Result | Notes |
|---|---|---|---|
| Полный набор | `make check` (до паузы) | 456 passed | unit 385 + integration 71 |
| Фича-тесты | `uv run pytest src/tests/integration/test_resolve_access.py -q` | 10 passed | сиды, bump прав/ролей, FSM через Container |
| layer-3 | `uv run python -m apps.cli "…" --fsm --token dev-analyst-token` | done, 10 узлов | без/чужой токен → rejected exit 1 |
| REVIEW-GATE | code-reviewer 298e36c..1aa3aa0 | **ОБОРВАН** | дневной лимит ключа 75,18/75,00 ₽ — повторить 2026-11-22 |

## Files Changed

- Новые: `application/ports/{auth_provider,permission_store}.py`, `infrastructure/persistence/{dev_token_auth,pg_permission_store}.py`, `application/services/{resolve_access_service,resolve_access_node}.py`, `src/tests/unit/{test_auth_provider,test_resolve_access_node}.py`, `src/tests/integration/test_resolve_access.py`
- Изменённые: `domain/entities.py` (AgentState.user_id), `config/settings.py` (agent_dev_tokens/dev_token_map), `application/services/stub_nodes.py` (initial_state user_id), `application/use_cases/answer_question.py`, `orchestration/{graph,runner}.py` (node_overrides), `composition/container.py`, `src/apps/cli.py` (--token), `tests/unit/{test_container_wiring,test_domain_entities}.py`, `solution/sessions/S00_session.md`, `docs/ARCHITECTURE.md`, `.env.example`

## Decisions Made

- Рulings R1–R6 ARCH-GATE S05 (канон-чек архитектора, до кода) — детально в [solution/sessions/S05_session.md](solution/sessions/S05_session.md)
- Отклонение: юнит-тест переименован в `test_resolve_access_node.py` (basename-коллизия с integration-файлом без __init__.py)

## Blockers / Risks

- **Дневной лимит API-ключа** (75 ₽/день): субагенты (architect/code-reviewer) крупного размера упираются — ревью-диспетчеризацию делать с утра/после сброса; экономичная диспетчеризация (файлы перечислить, не просить читать всё)
- Риск при ревью: `make_guarded_route` оценивает решение ребра ДО гварда — fail-closed rejected срабатывает раньше budget_stop (сознательное поведение, тесты фиксируют)

## Next Session Startup

1. Read `AGENTS.md`.
2. Read `feature_list.json` (feat-005 active) and `progress.md` (S05 пауза).
3. Review this handoff.
4. Сессия S05 уже ◐ (старт-гейт пройден, ARCH-GATE ✅) — сразу континуация: code-reviewer (BASE=298e36c HEAD=1aa3aa0) → fix-волна → REVIEW-GATE ✅ → `make verify-feature F=feat-005` → merge в `milestone/v4` → End of Session S05 + `make archive`.

## Recommended Next Step

- Повторить диспетчеризацию `code-reviewer` по 298e36c..1aa3aa0 (после сброса лимита), затем verify-feature и merge — feat-005 закрывается целиком в S05.

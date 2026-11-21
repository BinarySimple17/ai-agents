"""Юнит-тесты чистой логики путей графа (S07, feat-007) — без Neo4j.

Ловушка-4 сессии: LLM не строит пути — `find_join_paths` детерминированный
код. Ранжирование (`_rank_paths`) и развёртка направлений (`_build_path`) —
чистые функции адаптера, тестируются на подставных объектах (SDK-типы Neo4j
не нужны: `_build_path` читает только mapping-интерфейсы start_node/end_node).
"""

from typing import Any

from text_to_sql_agent.application.ports.schema_graph_store import JoinEdge, JoinPath
from text_to_sql_agent.infrastructure.graph.neo4j_graph_store import _build_path, _rank_paths


class _FakeNode:
    def __init__(self, name: str) -> None:
        self._name = name

    def __getitem__(self, key: str) -> str:
        return self._name


class _FakeRel:
    def __init__(self, rel_id: str, start: str, end: str, weight: float) -> None:
        self._rel_id = rel_id
        self._weight = weight
        self.start_node = _FakeNode(start)
        self.end_node = _FakeNode(end)

    def __getitem__(self, key: str) -> Any:  # noqa: ANN401 — mapping-интерфейс Neo4j
        return self._rel_id if key == "rel_id" else self._weight


def _edge(rel_id: str, frm: str, to: str, weight: float) -> JoinEdge:
    return JoinEdge(rel_id=rel_id, from_table=frm, to_table=to, weight=weight)


def _path(edges: list[JoinEdge]) -> JoinPath:
    return JoinPath(edges=edges, total_weight=sum(e.weight for e in edges))


def test_rank_orders_by_weight_then_hops_then_rel_ids():
    """Порядок: убывание веса → меньше хопов → лексикографика rel_id (детерминизм)."""
    heavy2 = _path([_edge("e_b", "a", "b", 1.5), _edge("e_c", "b", "c", 0.5)])
    heavy3 = _path(
        [_edge("e_a", "a", "x", 1.0), _edge("e_d", "x", "y", 0.5), _edge("e_f", "y", "c", 0.5)]
    )
    light1 = _path([_edge("e_e", "a", "c", 1.0)])
    ranked = _rank_paths([heavy3, light1, heavy2], k=5)
    assert [p.total_weight for p in ranked] == [2.0, 2.0, 1.0]
    # равный вес 2.0: путь из 2 хопов раньше 3-хопового
    assert ranked[0] is heavy2 and ranked[1] is heavy3


def test_rank_caps_k():
    paths = [_path([_edge(f"e{i}", "a", "b", 1.0 - i * 0.1)]) for i in range(5)]
    assert [p.edges[0].rel_id for p in _rank_paths(paths, k=2)] == ["e0", "e1"]


def test_rank_is_deterministic_on_equal_keys():
    """Полный тай на вес/хопы — лексикографика rel_id, не порядок входа."""
    p1 = _path([_edge("e_x", "a", "b", 1.0)])
    p2 = _path([_edge("e_a", "a", "b", 1.0)])
    assert _rank_paths([p1, p2], k=5)[0] is p2
    assert _rank_paths([p2, p1], k=5)[0] is p2


def test_build_path_natural_direction():
    nodes = [_FakeNode("customers"), _FakeNode("orders")]
    rels = [_FakeRel("e_customers_orders", "customers", "orders", 1.0)]
    path = _build_path(nodes, rels)
    assert path is not None
    assert path.tables == ["customers", "orders"]
    assert path.edges[0].rel_id == "e_customers_orders"


def test_build_path_reverses_against_registry_direction():
    """Обход против направления связи (left→right реестра) разворачивает from/to:
    путь orders→campaigns идёт по ребру e_campaigns_orders (campaigns→orders)."""
    nodes = [_FakeNode("orders"), _FakeNode("campaigns")]
    rels = [_FakeRel("e_campaigns_orders", "campaigns", "orders", 1.2)]
    path = _build_path(nodes, rels)
    assert path is not None
    assert path.tables == ["orders", "campaigns"]
    assert path.edges[0].from_table == "orders"
    assert path.edges[0].to_table == "campaigns"
    assert path.total_weight == 1.2


def test_build_path_fail_closed_on_disconnected_segment():
    nodes = [_FakeNode("orders"), _FakeNode("bonuses")]
    rels = [_FakeRel("e_employees_bonuses", "employees", "bonuses", 1.0)]
    assert _build_path(nodes, rels) is None

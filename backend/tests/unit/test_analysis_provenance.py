"""EvidenceGraph -- provenance node/edge bookkeeping and backward tracing."""

from __future__ import annotations

from src.core.analysis.provenance import EvidenceEdge, EvidenceGraph, EvidenceNode


# --------------------------------------------------------------------------- #
# EvidenceNode / EvidenceEdge
# --------------------------------------------------------------------------- #
def test_node_round_trips_through_dict() -> None:
    node = EvidenceNode(id="claim-0", kind="claim", label="15", data={"unit": "count"})

    restored = EvidenceNode.from_dict(node.to_dict())

    assert restored.id == node.id
    assert restored.kind == node.kind
    assert restored.label == node.label
    assert restored.data == node.data


def test_edge_round_trips_through_dict() -> None:
    edge = EvidenceEdge(source="code-0", target="execution-0", relation="produced")

    restored = EvidenceEdge.from_dict(edge.to_dict())

    assert restored.source == edge.source
    assert restored.target == edge.target
    assert restored.relation == edge.relation


# --------------------------------------------------------------------------- #
# EvidenceGraph.add_node / add_edge
# --------------------------------------------------------------------------- #
def test_add_node_assigns_sequential_ids_per_kind() -> None:
    graph = EvidenceGraph()

    first = graph.add_node("execution", "step one")
    second = graph.add_node("execution", "step two")
    other_kind = graph.add_node("code", "some code")

    assert first == "execution-0"
    assert second == "execution-1"
    assert other_kind == "code-0"


def test_add_edge_ignores_edges_touching_unknown_nodes() -> None:
    graph = EvidenceGraph()
    real = graph.add_node("execution", "step")

    graph.add_edge(real, "missing-node", "produced")
    graph.add_edge("missing-node", real, "produced")

    assert graph.edges == []


def test_ensure_dataset_reuses_the_node_for_the_same_hash() -> None:
    graph = EvidenceGraph()

    first = graph.ensure_dataset("hash-a", "sales.csv")
    second = graph.ensure_dataset("hash-a", "sales.csv")

    assert first == second
    assert len(graph.nodes) == 1


def test_ensure_dataset_creates_separate_nodes_for_different_hashes() -> None:
    graph = EvidenceGraph()

    first = graph.ensure_dataset("hash-a", "sales.csv")
    second = graph.ensure_dataset("hash-b", "sales.csv")

    assert first != second
    assert len(graph.nodes) == 2


# --------------------------------------------------------------------------- #
# EvidenceGraph.last
# --------------------------------------------------------------------------- #
def test_last_returns_the_most_recently_added_node_of_a_kind() -> None:
    graph = EvidenceGraph()
    graph.add_node("execution", "first")
    second = graph.add_node("execution", "second")
    graph.add_node("code", "unrelated")

    assert graph.last("execution") == second


def test_last_returns_none_when_no_node_of_that_kind_exists() -> None:
    assert EvidenceGraph().last("execution") is None


# --------------------------------------------------------------------------- #
# EvidenceGraph.trace
# --------------------------------------------------------------------------- #
def test_trace_returns_ancestors_roots_first_ending_with_the_node_itself() -> None:
    graph = EvidenceGraph()
    dataset_id = graph.add_node("dataset", "sales.csv", content_hash="hash-a")
    code_id = graph.add_node("code", "compute total")
    execution_id = graph.add_node("execution", "compute total")
    claim_id = graph.add_node("claim", "15")

    graph.add_edge(code_id, execution_id, "produced")
    graph.add_edge(dataset_id, execution_id, "derived_from")
    graph.add_edge(execution_id, claim_id, "supports")

    traced = [node.id for node in graph.trace(claim_id)]

    assert traced[-1] == claim_id
    assert traced.index(execution_id) < traced.index(claim_id)
    assert dataset_id in traced
    assert code_id in traced


def test_trace_of_an_unknown_node_is_empty() -> None:
    assert EvidenceGraph().trace("missing") == []


def test_trace_of_an_isolated_node_is_just_itself() -> None:
    graph = EvidenceGraph()
    node_id = graph.add_node("dataset", "sales.csv")

    assert [node.id for node in graph.trace(node_id)] == [node_id]


# --------------------------------------------------------------------------- #
# Round trip
# --------------------------------------------------------------------------- #
def test_graph_round_trips_through_dict() -> None:
    graph = EvidenceGraph()
    code_id = graph.add_node("code", "compute total")
    execution_id = graph.add_node("execution", "compute total", output="15")
    graph.add_edge(code_id, execution_id, "produced")

    restored = EvidenceGraph.from_dict(graph.to_dict())

    assert set(restored.nodes) == {code_id, execution_id}
    assert restored.nodes[execution_id].data == {"output": "15"}
    assert [edge.to_dict() for edge in restored.edges] == [edge.to_dict() for edge in graph.edges]


def test_from_dict_continues_id_numbering_without_collisions() -> None:
    graph = EvidenceGraph()
    graph.add_node("execution", "first")
    graph.add_node("execution", "second")

    restored = EvidenceGraph.from_dict(graph.to_dict())
    third = restored.add_node("execution", "third")

    assert third == "execution-2"
    assert len(restored.nodes) == 3


def test_empty_graph_round_trips() -> None:
    restored = EvidenceGraph.from_dict({})

    assert restored.nodes == {}
    assert restored.edges == []

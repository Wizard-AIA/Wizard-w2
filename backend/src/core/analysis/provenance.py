"""The evidence and provenance graph -- PLAN.md Layer 3 / ADR (evidence-provenance).

A number in an answer is only as trustworthy as the chain behind it: which execution produced
it, from which code, against which dataset version, checked by which validation. This module is
that chain, built as a small typed graph rather than a free-text trail, so a claim can be traced
back to its roots programmatically instead of by reading logs.

Claims are never invented here: they come from `grounding.check_grounding`'s own number
extraction (see `grounded_values` on `GroundingReport`), so there is exactly one place in the
codebase that decides what a "grounded number" is.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal


NodeKind = Literal[
    "question", "objective", "hypothesis", "dataset", "source", "step", "code", "execution", "result", "validation", "claim"
]

#: Edges read source -> target, e.g. ("code-0", "execution-0", "produced").
Relation = Literal["derived_from", "produced", "supports", "validates", "informs"]


@dataclass
class EvidenceNode:
    """One node in the provenance graph: a piece of the chain behind an answer."""

    id: str
    kind: NodeKind
    label: str
    data: dict[str, Any] = field(default_factory=dict)
    at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "label": self.label, "data": self.data, "at": self.at}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvidenceNode:
        return cls(
            id=str(data.get("id", "")),
            kind=data.get("kind", "step"),
            label=str(data.get("label", "")),
            data=dict(data.get("data") or {}),
            at=float(data.get("at", 0.0)),
        )


@dataclass
class EvidenceEdge:
    """One directed link: `source` is the reason `target` exists or is trustworthy."""

    source: str
    target: str
    relation: Relation

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "target": self.target, "relation": self.relation}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvidenceEdge:
        return cls(
            source=str(data.get("source", "")),
            target=str(data.get("target", "")),
            relation=data.get("relation", "informs"),
        )


@dataclass
class EvidenceGraph:
    """The provenance graph for one turn: nodes plus the edges between them.

    Node ids are assigned deterministically per kind (`"execution-0"`, `"execution-1"`, ...), so
    a rebuilt graph from the same sequence of calls produces the same ids -- useful for tests and
    for a report that wants to cite a node by id.
    """

    nodes: dict[str, EvidenceNode] = field(default_factory=dict)
    edges: list[EvidenceEdge] = field(default_factory=list)
    _counters: dict[str, int] = field(default_factory=dict, repr=False, compare=False)

    def add_node(self, kind: NodeKind, label: str, **data: Any) -> str:
        """Adds a node and returns its id. Ids are per-kind sequence numbers, not random."""
        index = self._counters.get(kind, 0)
        self._counters[kind] = index + 1
        node_id = f"{kind}-{index}"
        self.nodes[node_id] = EvidenceNode(id=node_id, kind=kind, label=label, data=data)
        return node_id

    def add_edge(self, source: str, target: str, relation: Relation) -> None:
        if source in self.nodes and target in self.nodes:
            self.edges.append(EvidenceEdge(source=source, target=target, relation=relation))

    def ensure_dataset(self, content_hash: str, label: str, **data: Any) -> str:
        """Reuses the dataset node for `content_hash` if one already exists this turn."""
        for node in self.nodes.values():
            if node.kind == "dataset" and node.data.get("content_hash") == content_hash:
                return node.id
        return self.add_node("dataset", label, content_hash=content_hash, **data)

    def last(self, kind: NodeKind) -> str | None:
        """The most recently added node id of `kind`, or None. Insertion order is dict order."""
        for node_id in reversed(self.nodes):
            if self.nodes[node_id].kind == kind:
                return node_id
        return None

    def trace(self, node_id: str) -> list[EvidenceNode]:
        """Every node `node_id` depends on, transitively, roots first, `node_id` itself last."""
        visited: set[str] = set()
        order: list[str] = []

        def visit(current: str) -> None:
            if current in visited or current not in self.nodes:
                return
            visited.add(current)
            for edge in self.edges:
                if edge.target == current:
                    visit(edge.source)
            order.append(current)

        visit(node_id)
        return [self.nodes[nid] for nid in order]

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [node.to_dict() for node in self.nodes.values()],
            "edges": [edge.to_dict() for edge in self.edges],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvidenceGraph:
        graph = cls()
        for node_data in data.get("nodes") or []:
            node = EvidenceNode.from_dict(node_data)
            graph.nodes[node.id] = node
            graph._counters[node.kind] = max(graph._counters.get(node.kind, 0), _index_of(node.id) + 1)
        graph.edges = [EvidenceEdge.from_dict(edge_data) for edge_data in data.get("edges") or []]
        return graph


def _index_of(node_id: str) -> int:
    """The numeric suffix of a node id (`"execution-3"` -> 3), or -1 if it has none."""
    _, _, suffix = node_id.rpartition("-")
    return int(suffix) if suffix.isdigit() else -1

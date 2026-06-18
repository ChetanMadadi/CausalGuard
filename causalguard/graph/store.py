"""In-memory typed provenance DAG store."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import networkx as nx

from causalguard.schema.edges import ProvenanceEdge
from causalguard.schema.nodes import ProvenanceNode, parse_provenance_node


class GraphStoreError(ValueError):
    """Raised when a graph mutation would violate store invariants."""


class GraphStore:
    """Store validated provenance nodes and edges in a NetworkX DAG."""

    def __init__(self) -> None:
        self._graph = nx.DiGraph()
        self._edge_index: dict[str, tuple[str, str]] = {}

    @property
    def node_count(self) -> int:
        return self._graph.number_of_nodes()

    @property
    def edge_count(self) -> int:
        return self._graph.number_of_edges()

    def add_node(self, node: ProvenanceNode | dict[str, Any]) -> ProvenanceNode:
        parsed = parse_provenance_node(node)
        if parsed.node_id in self._graph:
            raise GraphStoreError(f"node already exists: {parsed.node_id}")

        self._graph.add_node(
            parsed.node_id,
            node=parsed,
            node_type=str(parsed.node_type.value),
        )
        return parsed

    def add_edge(self, edge: ProvenanceEdge | dict[str, Any]) -> ProvenanceEdge:
        parsed = ProvenanceEdge.model_validate(edge)
        if parsed.edge_id in self._edge_index:
            raise GraphStoreError(f"edge already exists: {parsed.edge_id}")
        if parsed.source_id not in self._graph:
            raise GraphStoreError(f"missing edge source node: {parsed.source_id}")
        if parsed.target_id not in self._graph:
            raise GraphStoreError(f"missing edge target node: {parsed.target_id}")
        if self._graph.has_edge(parsed.source_id, parsed.target_id):
            raise GraphStoreError(
                f"edge already exists between {parsed.source_id} and {parsed.target_id}"
            )
        if nx.has_path(self._graph, parsed.target_id, parsed.source_id):
            raise GraphStoreError(
                f"edge would create a cycle: {parsed.source_id} -> {parsed.target_id}"
            )

        self._graph.add_edge(
            parsed.source_id,
            parsed.target_id,
            edge=parsed,
            edge_id=parsed.edge_id,
            edge_type=str(parsed.edge_type.value),
        )
        self._edge_index[parsed.edge_id] = (parsed.source_id, parsed.target_id)
        return parsed

    def has_node(self, node_id: str) -> bool:
        return self._graph.has_node(node_id)

    def has_edge(self, edge_id: str) -> bool:
        return edge_id in self._edge_index

    def get_node(self, node_id: str) -> ProvenanceNode:
        if node_id not in self._graph:
            raise GraphStoreError(f"missing node: {node_id}")
        return self._graph.nodes[node_id]["node"]

    def get_edge(self, edge_id: str) -> ProvenanceEdge:
        if edge_id not in self._edge_index:
            raise GraphStoreError(f"missing edge: {edge_id}")
        source_id, target_id = self._edge_index[edge_id]
        return self._graph.edges[source_id, target_id]["edge"]

    def nodes(self) -> Iterator[ProvenanceNode]:
        for _, data in self._graph.nodes(data=True):
            yield data["node"]

    def edges(self) -> Iterator[ProvenanceEdge]:
        for _, _, data in self._graph.edges(data=True):
            yield data["edge"]

    def is_acyclic(self) -> bool:
        return nx.is_directed_acyclic_graph(self._graph)

    def topological_node_ids(self) -> list[str]:
        return list(nx.topological_sort(self._graph))

    def copy_networkx(self) -> nx.DiGraph:
        return self._graph.copy()

    def to_dict(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "nodes": [
                node.model_dump(mode="json")
                for node in sorted(self.nodes(), key=lambda item: item.node_id)
            ],
            "edges": [
                edge.model_dump(mode="json")
                for edge in sorted(self.edges(), key=lambda item: item.edge_id)
            ],
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    def to_dot(self) -> str:
        lines = ["digraph causalguard {", "  rankdir=LR;"]
        for node in sorted(self.nodes(), key=lambda item: item.node_id):
            node_label = _dot_escape(f"{node.node_type.value}\n{node.node_id}")
            lines.append(f'  "{_dot_escape(node.node_id)}" [label="{node_label}"];')

        for edge in sorted(self.edges(), key=lambda item: item.edge_id):
            edge_label = _dot_escape(
                f"{edge.edge_type.value}\n{edge.derivation.value}/{edge.confidence.value}"
            )
            lines.append(
                f'  "{_dot_escape(edge.source_id)}" -> '
                f'"{_dot_escape(edge.target_id)}" [label="{edge_label}"];'
            )

        lines.append("}")
        return "\n".join(lines)


def _dot_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

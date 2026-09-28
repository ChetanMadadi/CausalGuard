"""Small views over existing graphs; no production OS node/schema extension."""
from __future__ import annotations
import networkx as nx
from causalguard.graph import GraphStore
from causalguard.policy.alerts import AlertNode, AlertEdge
from causalguard.schema.edges import EdgeType
from causalguard.schema.nodes import NodeType


class ProductionAlertView:
    supported_node_types = frozenset(n.value for n in NodeType)
    supported_edge_types = frozenset(e.value for e in EdgeType)

    def __init__(self, store: GraphStore, *, coverage_issues=()):
        self.store = store
        self.graph = store.copy_networkx()
        self.coverage_issues = tuple(coverage_issues)

    def nodes(self):
        return [AlertNode(n.node_id, n.node_type.value, n.model_dump(mode="json"))
                for n in self.store.nodes()]

    def incoming(self, node_id):
        result = []
        for _, _, eid in self.graph.in_edges(node_id, keys=True):
            e = self.store.get_edge(eid)
            origin = e.derivation.value
            eligible = (bool(e.evidence_ref) and origin in {
                "runtime_observation", "explicit_data_reference", "parent_event"
            } and e.edge_type not in {EdgeType.AUTHORIZES, EdgeType.ACCESSES})
            if e.edge_type in {EdgeType.INPUT_TO, EdgeType.PRODUCES, EdgeType.INVOKES, EdgeType.TRIGGERS}:
                basis = "context_availability/" + origin
            elif e.edge_type is EdgeType.READ:
                basis = "observed_read/" + origin
            else:
                basis = "observed_effect/" + origin
            for endpoint in (e.source_id, e.target_id):
                node = self.store.get_node(endpoint)
                if (e.edge_type in {EdgeType.READ, EdgeType.WRITE}
                        and getattr(node, "data_flow_semantics", None) == "identity_copy_v1"):
                    basis += "/adapter_attested_verified_identity_copy"
            result.append(AlertEdge(e.edge_id, e.source_id, e.target_id, e.edge_type.value,
                                    e.timestamp, e.attributes.get("clock_unit"),
                                    e.attributes.get("clock_domain"), basis, eligible))
        return result


class SyntheticAlertView:
    """Conformance-only NetworkX adapter, never collected operating-system telemetry."""
    supported_node_types = frozenset({"File", "Process", "Socket"})
    supported_edge_types = frozenset({"read", "fork", "connect", "send", "write"})

    def __init__(self, graph: nx.MultiDiGraph):
        if not isinstance(graph, nx.MultiDiGraph):
            raise ValueError("synthetic view requires a directed multigraph")
        self.graph = graph
        self.coverage_issues = ("synthetic_conformance_not_collected_os_telemetry",)
        ids = [key for _, _, key in graph.edges(keys=True)]
        if len(ids) != len(set(ids)):
            raise ValueError("edge IDs must be globally unique")

    def nodes(self):
        return [AlertNode(nid, attrs["node_type"], dict(attrs))
                for nid, attrs in self.graph.nodes(data=True)]

    def incoming(self, node_id):
        return [AlertEdge(eid, source, target, attrs["edge_type"], attrs["timestamp"],
                          attrs.get("clock_unit"), attrs.get("clock_domain"),
                          attrs.get("evidence_basis", "synthetic_coarse_observation"),
                          attrs.get("eligible", True))
                for source, target, eid, attrs in self.graph.in_edges(node_id, keys=True, data=True)]

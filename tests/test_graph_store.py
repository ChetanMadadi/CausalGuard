from __future__ import annotations

import json

import networkx as nx
import pytest

from causalguard.graph.store import GraphStore, GraphStoreError


def llm_node(node_id: str = "llm:e2") -> dict[str, object]:
    return {
        "node_id": node_id,
        "node_type": "llm_invocation",
        "model_name": "gpt-4o",
        "prompt_hash": "sha256:p",
        "output_hash": "sha256:o",
        "token_count": 120,
        "timestamp": 2.0,
        "agent_id": "agent1",
        "session_id": "s1",
        "causal_context_id": "ctx17",
    }


def tool_node(node_id: str = "tool:e3") -> dict[str, object]:
    return {
        "node_id": node_id,
        "node_type": "tool_call",
        "tool_name": "share_contacts",
        "action_class": "upload",
        "argument_summary": {"field_summary": ["name", "email"]},
        "target_resource": "contacts.csv",
        "destination": "external.example",
        "timestamp": 3.0,
        "agent_id": "agent1",
        "session_id": "s1",
        "causal_context_id": "ctx17",
    }


def data_node(node_id: str = "data:payload") -> dict[str, object]:
    return {
        "node_id": node_id,
        "node_type": "data_object",
        "resource_id": "payload-17",
        "object_kind": "payload",
        "content_hash": "sha256:payload",
        "version": "1",
        "sensitivity": "internal",
        "trust_label": "trusted",
        "owner": "user",
    }


def system_node(node_id: str = "sys:e4") -> dict[str, object]:
    return {
        "node_id": node_id,
        "node_type": "system_operation",
        "operation_type": "network_send",
        "action_class": "send_email",
        "resource_refs": [],
        "payload_refs": ["payload-17"],
        "destination": "external.example",
        "destination_trust": "untrusted",
        "timestamp": 4.0,
        "agent_id": "agent1",
        "session_id": "s1",
        "causal_context_id": "ctx17",
    }


def edge_payload(
    edge_id: str = "edge:e2:e3",
    source_id: str = "llm:e2",
    target_id: str = "tool:e3",
    edge_type: str = "invokes",
) -> dict[str, object]:
    return {
        "edge_id": edge_id,
        "source_id": source_id,
        "target_id": target_id,
        "edge_type": edge_type,
        "timestamp": 3.0,
        "derivation": "parent_event",
        "confidence": "high",
        "evidence_ref": "e3",
        "attributes": {"collector": "test"},
    }


def graph_with_llm_and_tool() -> GraphStore:
    store = GraphStore()
    store.add_node(llm_node())
    store.add_node(tool_node())
    return store


def test_add_node_preserves_typed_node() -> None:
    store = GraphStore()

    node = store.add_node(llm_node())

    assert store.node_count == 1
    assert store.get_node("llm:e2") == node
    assert store.has_node("llm:e2")


def test_add_node_rejects_duplicate_node_id() -> None:
    store = GraphStore()
    store.add_node(llm_node())

    with pytest.raises(GraphStoreError, match="node already exists"):
        store.add_node(llm_node())


def test_add_edge_preserves_typed_edge() -> None:
    store = graph_with_llm_and_tool()

    edge = store.add_edge(edge_payload())

    assert store.edge_count == 1
    assert store.get_edge("edge:e2:e3") == edge
    assert store.has_edge("edge:e2:e3")
    assert store.topological_node_ids() == ["llm:e2", "tool:e3"]


def test_add_edge_rejects_missing_endpoint() -> None:
    store = GraphStore()
    store.add_node(llm_node())

    with pytest.raises(GraphStoreError, match="missing edge target node"):
        store.add_edge(edge_payload())


def test_add_edge_rejects_duplicate_edge_id() -> None:
    store = graph_with_llm_and_tool()
    store.add_edge(edge_payload())

    with pytest.raises(GraphStoreError, match="edge already exists"):
        store.add_edge(edge_payload())



def test_multigraph_retains_multiple_edges_between_same_nodes() -> None:
    store = GraphStore()
    store.add_node(data_node())
    store.add_node(system_node())

    read_edge = store.add_edge(
        edge_payload(
            edge_id="edge:read",
            source_id="data:payload",
            target_id="sys:e4",
            edge_type="read",
        )
    )
    payload_edge = store.add_edge(
        edge_payload(
            edge_id="edge:payload",
            source_id="data:payload",
            target_id="sys:e4",
            edge_type="payload_of",
        )
    )

    graph = store.copy_networkx()
    assert isinstance(graph, nx.MultiDiGraph)
    assert store.edge_count == 2
    assert store.get_edge("edge:read") == read_edge
    assert store.get_edge("edge:payload") == payload_edge
    assert set(graph["data:payload"]["sys:e4"]) == {"edge:read", "edge:payload"}


def test_add_edge_rejects_cycle() -> None:
    store = graph_with_llm_and_tool()
    store.add_edge(edge_payload())

    with pytest.raises(GraphStoreError, match="cycle"):
        store.add_edge(
            {
                **edge_payload(
                    edge_id="edge:e3:e2",
                    source_id="tool:e3",
                    target_id="llm:e2",
                ),
                "edge_type": "triggers",
            }
        )

    assert store.edge_count == 1
    assert store.is_acyclic()


def test_export_graph_as_dict_json_dot_and_mermaid_preserves_parallel_edges() -> None:
    store = GraphStore()
    store.add_node(data_node())
    store.add_node(system_node())
    store.add_edge(
        edge_payload("edge:read", "data:payload", "sys:e4", "read")
    )
    store.add_edge(
        edge_payload("edge:payload", "data:payload", "sys:e4", "payload_of")
    )

    exported = store.to_dict()
    as_json = json.loads(store.to_json())
    as_dot = store.to_dot()
    as_mermaid = store.to_mermaid()

    assert len(exported["edges"]) == 2
    assert {edge["edge_id"] for edge in exported["edges"]} == {
        "edge:read",
        "edge:payload",
    }
    assert as_json == exported
    assert "digraph causalguard" in as_dot
    assert "Data: data:payload" in as_dot
    assert "SysOp: sys:e4" in as_dot
    assert as_dot.count('"data:payload" -> "sys:e4"') == 2
    assert "parent_event/high" in as_dot
    assert as_mermaid.count("n0 -->") == 2
    assert "read: edge:read" in as_mermaid
    assert "payload_of: edge:payload" in as_mermaid


def test_copy_networkx_does_not_expose_mutable_internal_graph() -> None:
    store = graph_with_llm_and_tool()
    graph_copy = store.copy_networkx()

    graph_copy.remove_node("llm:e2")

    assert store.has_node("llm:e2")

from __future__ import annotations

import json

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


def edge_payload(
    edge_id: str = "edge:e2:e3",
    source_id: str = "llm:e2",
    target_id: str = "tool:e3",
) -> dict[str, object]:
    return {
        "edge_id": edge_id,
        "source_id": source_id,
        "target_id": target_id,
        "edge_type": "invokes",
        "timestamp": 3.0,
        "derivation": "parent_event",
        "confidence": "high",
        "evidence_ref": "e3",
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


def test_add_edge_rejects_duplicate_edge_id_and_duplicate_pair() -> None:
    store = graph_with_llm_and_tool()
    store.add_edge(edge_payload())

    with pytest.raises(GraphStoreError, match="edge already exists"):
        store.add_edge(edge_payload())

    with pytest.raises(GraphStoreError, match="edge already exists between"):
        store.add_edge(edge_payload(edge_id="edge:e2:e3:again"))


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


def test_export_graph_as_dict_json_and_dot() -> None:
    store = graph_with_llm_and_tool()
    store.add_edge(edge_payload())

    exported = store.to_dict()
    as_json = json.loads(store.to_json())
    as_dot = store.to_dot()

    assert exported["nodes"][0]["node_id"] == "llm:e2"
    assert exported["edges"][0]["edge_type"] == "invokes"
    assert as_json == exported
    assert "digraph causalguard" in as_dot
    assert '"llm:e2" -> "tool:e3"' in as_dot
    assert "parent_event/high" in as_dot


def test_copy_networkx_does_not_expose_mutable_internal_graph() -> None:
    store = graph_with_llm_and_tool()
    graph_copy = store.copy_networkx()

    graph_copy.remove_node("llm:e2")

    assert store.has_node("llm:e2")

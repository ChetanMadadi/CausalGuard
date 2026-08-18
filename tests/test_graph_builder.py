from __future__ import annotations

from typing import Any

import networkx as nx
import pytest

from causalguard.graph import GraphBuilder, GraphBuilderError, GraphStore
from causalguard.schema.edges import Confidence, Derivation, EdgeType
from causalguard.schema.nodes import (
    DataObjectNode,
    HumanApprovalNode,
    LLMInvocationNode,
    SystemOperationNode,
    ToolCallNode,
)


def event_payload(
    event_id: str,
    event_type: str,
    *,
    timestamp: float = 1.0,
    parent_event_id: str | None = None,
    attributes: dict[str, Any] | None = None,
    agent_id: str | None = "agent1",
    session_id: str | None = "s1",
    causal_context_id: str | None = "ctx17",
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "timestamp": timestamp,
        "event_type": event_type,
        "agent_id": agent_id,
        "session_id": session_id,
        "causal_context_id": causal_context_id,
        "parent_event_id": parent_event_id,
        "attributes": attributes or {},
    }


def llm_event(event_id: str = "e2", *, parent_event_id: str | None = None) -> dict[str, Any]:
    return event_payload(
        event_id,
        "llm_invocation",
        timestamp=2.0,
        parent_event_id=parent_event_id,
        attributes={
            "model_name": "gpt-4o",
            "prompt_hash": "sha256:p",
            "output_hash": "sha256:o",
            "token_count": 120,
        },
    )


def tool_event(event_id: str = "e3", *, parent_event_id: str | None = "e2") -> dict[str, Any]:
    return event_payload(
        event_id,
        "tool_call",
        timestamp=3.0,
        parent_event_id=parent_event_id,
        attributes={
            "tool_name": "share_contacts",
            "action_class": "upload",
            "target_resource": "contacts.csv",
            "destination": "external.example",
        },
    )


def data_access_event(
    event_id: str = "e4",
    *,
    parent_event_id: str | None = "e3",
) -> dict[str, Any]:
    return event_payload(
        event_id,
        "data_access",
        timestamp=4.0,
        parent_event_id=parent_event_id,
        attributes={
            "operation_type": "file_read",
            "resource_id": "contacts.csv",
            "object_kind": "file",
            "content_hash": "sha256:contacts",
            "sensitivity": "PII",
            "trust_label": "trusted",
        },
    )


def network_send_event(
    event_id: str = "e5",
    *,
    parent_event_id: str | None = "e3",
) -> dict[str, Any]:
    return event_payload(
        event_id,
        "network_send",
        timestamp=5.0,
        parent_event_id=parent_event_id,
        attributes={
            "destination": "external.example",
            "trust_label": "untrusted",
            "byte_count": 2048,
        },
    )


def approval_event(event_id: str = "e6") -> dict[str, Any]:
    return event_payload(
        event_id,
        "human_approval",
        timestamp=6.0,
        attributes={
            "approver_id": "user1",
            "action_class": "upload",
            "resource_scope": "contacts.csv",
            "destination_scope": "external.example",
            "expiration": 60.0,
        },
    )


def matching_edges(
    store: GraphStore,
    *,
    source_id: str | None = None,
    target_id: str | None = None,
    edge_type: EdgeType | None = None,
    derivation: Derivation | None = None,
    confidence: Confidence | None = None,
) -> list[Any]:
    return [
        edge
        for edge in store.edges()
        if (source_id is None or edge.source_id == source_id)
        and (target_id is None or edge.target_id == target_id)
        and (edge_type is None or edge.edge_type == edge_type)
        and (derivation is None or edge.derivation == derivation)
        and (confidence is None or edge.confidence == confidence)
    ]


def test_llm_event_creates_llm_node() -> None:
    store = GraphStore()
    builder = GraphBuilder(store)

    builder.process_event(llm_event())

    node = store.get_node("llm:e2")
    assert isinstance(node, LLMInvocationNode)
    assert node.model_name == "gpt-4o"
    assert node.prompt_hash == "sha256:p"
    assert store.node_count == 1


def test_tool_event_creates_tool_node_and_parent_invokes_edge() -> None:
    store = GraphStore()
    builder = GraphBuilder(store)

    builder.process_trace([llm_event(), tool_event()])

    node = store.get_node("tool:e3")
    assert isinstance(node, ToolCallNode)
    assert node.tool_name == "share_contacts"

    invokes_edges = matching_edges(
        store,
        source_id="llm:e2",
        target_id="tool:e3",
        edge_type=EdgeType.INVOKES,
        derivation=Derivation.PARENT_EVENT,
        confidence=Confidence.HIGH,
    )
    assert len(invokes_edges) == 1
    assert invokes_edges[0].evidence_ref == "e3"


def test_data_access_creates_system_data_and_edges() -> None:
    store = GraphStore()
    builder = GraphBuilder(store)

    builder.process_trace([llm_event(), tool_event(), data_access_event()])

    system_node = store.get_node("sys:e4")
    data_node = store.get_node("data:resource:contacts.csv")
    assert isinstance(system_node, SystemOperationNode)
    assert isinstance(data_node, DataObjectNode)
    assert system_node.operation_type == "file_read"
    assert data_node.sensitivity.value == "PII"

    assert len(
        matching_edges(
            store,
            source_id="tool:e3",
            target_id="sys:e4",
            edge_type=EdgeType.TRIGGERS,
            derivation=Derivation.PARENT_EVENT,
        )
    ) == 1
    assert len(
        matching_edges(
            store,
            source_id="data:resource:contacts.csv",
            target_id="sys:e4",
            edge_type=EdgeType.READ,
            derivation=Derivation.EXPLICIT_DATA_REFERENCE,
        )
    ) == 1


def test_network_send_keeps_destination_metadata_without_destination_node() -> None:
    store = GraphStore()
    builder = GraphBuilder(store)

    builder.process_trace([tool_event(parent_event_id=None), network_send_event()])

    system_node = store.get_node("sys:e5")
    assert isinstance(system_node, SystemOperationNode)
    assert system_node.operation_type == "network_send"
    assert system_node.destination == "external.example"
    assert system_node.destination_trust == "untrusted"
    assert not store.has_node("data:destination:external.example")

    assert len(
        matching_edges(
            store,
            source_id="tool:e3",
            target_id="sys:e5",
            edge_type=EdgeType.TRIGGERS,
        )
    ) == 1
    assert not matching_edges(store, edge_type=EdgeType.ACCESSES)


def test_shared_causal_context_creates_lower_confidence_edges_without_parent() -> None:
    store = GraphStore()
    builder = GraphBuilder(store)

    builder.process_trace(
        [
            llm_event(parent_event_id=None),
            tool_event(parent_event_id=None),
            data_access_event(parent_event_id=None),
        ]
    )

    assert len(
        matching_edges(
            store,
            source_id="llm:e2",
            target_id="tool:e3",
            edge_type=EdgeType.INVOKES,
            derivation=Derivation.CAUSAL_CONTEXT,
            confidence=Confidence.MEDIUM,
        )
    ) == 1
    assert len(
        matching_edges(
            store,
            source_id="tool:e3",
            target_id="sys:e4",
            edge_type=EdgeType.TRIGGERS,
            derivation=Derivation.CAUSAL_CONTEXT,
            confidence=Confidence.MEDIUM,
        )
    ) == 1


def test_matching_human_approval_creates_authorizes_edge() -> None:
    store = GraphStore()
    builder = GraphBuilder(store)

    builder.process_trace([tool_event(parent_event_id=None), approval_event()])

    approval_node = store.get_node("approval:e6")
    assert isinstance(approval_node, HumanApprovalNode)

    authorizes_edges = matching_edges(
        store,
        source_id="approval:e6",
        target_id="tool:e3",
        edge_type=EdgeType.AUTHORIZES,
        derivation=Derivation.EXPLICIT_APPROVAL_SCOPE,
        confidence=Confidence.HIGH,
    )
    assert len(authorizes_edges) == 1
    assert authorizes_edges[0].evidence_ref == "e6"


def test_contacts_upload_trace_builds_expected_acyclic_graph() -> None:
    store = GraphStore()
    builder = GraphBuilder(store)

    builder.process_trace(
        [
            event_payload(
                "e1",
                "user_input",
                timestamp=1.0,
                attributes={"input_hash": "sha256:user-request"},
            ),
            llm_event(parent_event_id="e1"),
            tool_event(parent_event_id="e2"),
            data_access_event(parent_event_id="e3"),
            network_send_event(parent_event_id="e3"),
        ]
    )

    assert store.is_acyclic()
    assert store.node_count == 5
    assert store.edge_count == 4

    node_types = {node.node_type.value for node in store.nodes()}
    edge_types = [edge.edge_type for edge in store.edges()]
    assert node_types == {
        "llm_invocation",
        "tool_call",
        "system_operation",
        "data_object",
    }
    assert edge_types.count(EdgeType.INVOKES) == 1
    assert edge_types.count(EdgeType.TRIGGERS) == 2
    assert edge_types.count(EdgeType.READ) == 1
    assert edge_types.count(EdgeType.ACCESSES) == 0


def test_missing_required_conversion_attribute_raises_builder_error() -> None:
    store = GraphStore()
    builder = GraphBuilder(store)

    with pytest.raises(GraphBuilderError, match="tool_name"):
        builder.process_event(
            event_payload(
                "e3",
                "tool_call",
                timestamp=3.0,
                attributes={"action_class": "upload"},
            )
        )


def test_file_write_creates_write_edge() -> None:
    store = GraphStore()
    builder = GraphBuilder(store)
    builder.process_event(
        event_payload(
            "write1",
            "system_operation",
            timestamp=1.0,
            attributes={
                "operation_type": "file_write",
                "resource_refs": ["output.txt"],
                "data_objects": {
                    "output.txt": {"object_kind": "file", "version": "2"}
                },
            },
        )
    )

    edges = matching_edges(
        store,
        source_id="sys:write1",
        target_id="data:resource:output.txt",
        edge_type=EdgeType.WRITE,
    )
    assert len(edges) == 1
    assert store.get_node("data:resource:output.txt").version == "2"


def test_explicit_artifact_relations_create_tool_return_path() -> None:
    store = GraphStore()
    builder = GraphBuilder(store)
    read_event = data_access_event("read1", parent_event_id=None)
    read_event["attributes"]["result_ref"] = "tool_result_17"
    read_event["attributes"]["data_objects"] = {
        "tool_result_17": {"object_kind": "tool_result"}
    }
    later_llm = llm_event("llm2", parent_event_id=None)
    later_llm["timestamp"] = 5.0
    later_llm["attributes"]["input_refs"] = ["tool_result_17"]

    builder.process_trace([read_event, later_llm])

    graph = store.copy_networkx()
    expected_path = [
        "data:resource:contacts.csv",
        "sys:read1",
        "data:resource:tool_result_17",
        "llm:llm2",
    ]
    assert nx.has_path(graph, expected_path[0], expected_path[-1])
    assert nx.shortest_path(graph, expected_path[0], expected_path[-1]) == expected_path
    assert len(matching_edges(store, edge_type=EdgeType.PRODUCES)) == 1
    assert len(matching_edges(store, edge_type=EdgeType.INPUT_TO)) == 1
    assert not matching_edges(
        store,
        source_id="sys:read1",
        target_id="llm:llm2",
        edge_type=EdgeType.INVOKES,
    )


def test_full_causal_data_and_authorization_provenance_is_one_component() -> None:
    store = GraphStore()
    builder = GraphBuilder(store)

    first_llm = llm_event("llm1")
    read_tool = tool_event("read_tool", parent_event_id="llm1")
    read_tool["attributes"].update(
        {"tool_name": "read_contacts", "action_class": "read_contacts"}
    )
    read_operation = data_access_event("read_op", parent_event_id="read_tool")
    read_operation["attributes"].update(
        {
            "result_ref": "tool_result_17",
            "data_objects": {
                "tool_result_17": {"object_kind": "tool_result"}
            },
        }
    )
    second_llm = llm_event("llm2")
    second_llm["timestamp"] = 5.0
    second_llm["attributes"].update(
        {
            "input_refs": ["tool_result_17"],
            "output_ref": "email_body_42",
            "data_objects": {
                "email_body_42": {"object_kind": "message"},
                "tool_result_17": {"object_kind": "tool_result"},
            },
        }
    )
    send_tool = tool_event("send_tool", parent_event_id="llm2")
    send_tool["timestamp"] = 6.0
    send_tool["attributes"].update(
        {"tool_name": "send_email", "action_class": "send_email"}
    )
    send_operation = network_send_event("send_op", parent_event_id="send_tool")
    send_operation["timestamp"] = 7.0
    send_operation["attributes"].update(
        {
            "action_class": "send_email",
            "payload_refs": ["email_body_42"],
            "data_objects": {
                "email_body_42": {"object_kind": "message"}
            },
        }
    )
    approval = event_payload(
        "approval1",
        "human_approval",
        timestamp=6.5,
        attributes={
            "approver_id": "user1",
            "action_scope": "send_email",
            "resource_scope": "email_body_42",
            "destination_scope": "external.example",
            "expiration": 10.0,
        },
    )

    builder.process_trace(
        [
            first_llm,
            read_tool,
            read_operation,
            second_llm,
            send_tool,
            send_operation,
            approval,
        ]
    )

    graph = store.copy_networkx()
    data_path = [
        "data:resource:contacts.csv",
        "sys:read_op",
        "data:resource:tool_result_17",
        "llm:llm2",
        "data:resource:email_body_42",
        "sys:send_op",
    ]
    assert nx.shortest_path(graph, data_path[0], data_path[-1]) == data_path
    assert len(list(nx.weakly_connected_components(graph))) == 1
    assert matching_edges(
        store,
        source_id="llm:llm2",
        target_id="tool:send_tool",
        edge_type=EdgeType.INVOKES,
    )
    assert matching_edges(
        store,
        source_id="tool:send_tool",
        target_id="sys:send_op",
        edge_type=EdgeType.TRIGGERS,
    )
    assert matching_edges(
        store,
        source_id="approval:approval1",
        target_id="sys:send_op",
        edge_type=EdgeType.AUTHORIZES,
    )
    approval_node = store.get_node("approval:approval1")
    assert isinstance(approval_node, HumanApprovalNode)
    assert approval_node.action_scope == "send_email"
    assert approval_node.resource_scope == "email_body_42"
    assert approval_node.destination_scope == "external.example"
    assert approval_node.expiration == 10.0


def test_tool_can_produce_artifact_and_artifact_can_be_input_to_tool() -> None:
    store = GraphStore()
    builder = GraphBuilder(store)
    producer = tool_event("producer", parent_event_id=None)
    producer["attributes"]["produced_refs"] = ["artifact_1"]
    consumer = tool_event("consumer", parent_event_id=None)
    consumer["timestamp"] = 4.0
    consumer["causal_context_id"] = "another-context"
    consumer["attributes"]["input_refs"] = ["artifact_1"]

    builder.process_trace([producer, consumer])

    assert matching_edges(
        store,
        source_id="tool:producer",
        target_id="data:resource:artifact_1",
        edge_type=EdgeType.PRODUCES,
    )
    assert matching_edges(
        store,
        source_id="data:resource:artifact_1",
        target_id="tool:consumer",
        edge_type=EdgeType.INPUT_TO,
    )


def test_builder_retains_parallel_read_and_payload_relations() -> None:
    store = GraphStore()
    builder = GraphBuilder(store)
    builder.process_event(
        event_payload(
            "op1",
            "system_operation",
            attributes={
                "operation_type": "file_read",
                "resource_refs": ["artifact_1"],
                "payload_refs": ["artifact_1"],
            },
        )
    )

    edges = matching_edges(
        store,
        source_id="data:resource:artifact_1",
        target_id="sys:op1",
    )
    assert {edge.edge_type for edge in edges} == {
        EdgeType.READ,
        EdgeType.PAYLOAD_OF,
    }
    assert len(edges) == 2


def test_approval_scope_does_not_authorize_other_destination() -> None:
    store = GraphStore()
    builder = GraphBuilder(store)
    allowed = tool_event("allowed", parent_event_id=None)
    denied = tool_event("denied", parent_event_id=None)
    denied["attributes"]["destination"] = "different.example"

    builder.process_trace([allowed, denied, approval_event()])

    auth_edges = matching_edges(store, edge_type=EdgeType.AUTHORIZES)
    assert len(auth_edges) == 1
    assert auth_edges[0].target_id == "tool:allowed"


def test_unrelated_prior_read_does_not_fabricate_payload_flow() -> None:
    store = GraphStore()
    builder = GraphBuilder(store)
    read_event = data_access_event("read1", parent_event_id=None)
    send_event = network_send_event("send1", parent_event_id=None)
    send_event["timestamp"] = 5.0
    send_event["causal_context_id"] = "different-context"
    send_event["attributes"].update(
        {
            "payload_refs": ["status_message"],
            "data_objects": {
                "status_message": {
                    "object_kind": "message",
                    "sensitivity": "public",
                }
            },
        }
    )

    builder.process_trace([read_event, send_event])

    payload_edges = matching_edges(store, edge_type=EdgeType.PAYLOAD_OF)
    assert len(payload_edges) == 1
    assert payload_edges[0].source_id == "data:resource:status_message"
    assert not nx.has_path(
        store.copy_networkx(),
        "data:resource:contacts.csv",
        "sys:send1",
    )

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from causalguard.schema.edges import Confidence, Derivation, EdgeType, ProvenanceEdge
from causalguard.schema.events import EventType, NormalizedEvent
from causalguard.schema.nodes import (
    DataObjectNode,
    HumanApprovalNode,
    LLMInvocationNode,
    NodeType,
    Sensitivity,
    SystemOperationNode,
    ToolCallNode,
    TrustLabel,
    parse_provenance_node,
)


def valid_event_payload() -> dict[str, object]:
    return {
        "event_id": "e1",
        "timestamp": 1.0,
        "event_type": "llm_invocation",
        "agent_id": "agent1",
        "session_id": "s1",
        "causal_context_id": "ctx1",
        "parent_event_id": None,
        "attributes": {
            "model_name": "gpt-4o",
            "prompt_hash": "sha256:p",
            "output_hash": "sha256:o",
            "token_count": 120,
        },
    }


def test_valid_event_passes_validation() -> None:
    event = NormalizedEvent.model_validate(valid_event_payload())

    assert event.event_type == EventType.LLM_INVOCATION
    assert event.event_id == "e1"
    assert event.attributes["prompt_hash"] == "sha256:p"


def test_missing_required_event_field_fails_validation() -> None:
    payload = valid_event_payload()
    del payload["event_id"]

    with pytest.raises(ValidationError):
        NormalizedEvent.model_validate(payload)


def test_invalid_event_type_fails_validation() -> None:
    payload = valid_event_payload()
    payload["event_type"] = "unknown_event"

    with pytest.raises(ValidationError):
        NormalizedEvent.model_validate(payload)


def test_event_rejects_extra_top_level_field() -> None:
    payload = valid_event_payload()
    payload["unexpected"] = "field"

    with pytest.raises(ValidationError):
        NormalizedEvent.model_validate(payload)


def test_event_rejects_empty_ids_and_invalid_timestamp() -> None:
    payload = valid_event_payload()
    payload["event_id"] = " "

    with pytest.raises(ValidationError):
        NormalizedEvent.model_validate(payload)

    payload = valid_event_payload()
    payload["timestamp"] = math.inf

    with pytest.raises(ValidationError):
        NormalizedEvent.model_validate(payload)

    payload = valid_event_payload()
    payload["timestamp"] = -1.0

    with pytest.raises(ValidationError):
        NormalizedEvent.model_validate(payload)


def test_privacy_guard_rejects_raw_event_attribute_keys_recursively() -> None:
    payload = valid_event_payload()
    payload["attributes"] = {
        "safe_summary": {"nested": [{"prompt": "raw user prompt"}]},
    }

    with pytest.raises(ValidationError, match="raw sensitive content key"):
        NormalizedEvent.model_validate(payload)


@pytest.mark.parametrize(
    "blocked_key",
    [
        "prompt",
        "output",
        "message",
        "input_text",
        "file_content",
        "content",
        "body",
        "password",
        "token",
        "api_key",
        "secret",
        "raw_payload",
        "payload_raw",
    ],
)
def test_privacy_guard_rejects_blocked_keys(blocked_key: str) -> None:
    payload = valid_event_payload()
    payload["attributes"] = {blocked_key: "sensitive"}

    with pytest.raises(ValidationError):
        NormalizedEvent.model_validate(payload)


def test_privacy_guard_allows_hash_ids_labels_scopes_and_summary_metadata() -> None:
    payload = valid_event_payload()
    payload["attributes"] = {
        "input_hash": "sha256:user-request",
        "resource_id": "contacts.csv",
        "trust_label": "trusted",
        "resource_scope": "contacts.csv",
        "argument_summary": {"fields": ["name", "email"], "count": 2},
    }

    event = NormalizedEvent.model_validate(payload)

    assert event.attributes["argument_summary"]["count"] == 2


def test_llm_invocation_node_validation() -> None:
    node = LLMInvocationNode.model_validate(
        {
            "node_id": "llm:e2",
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
    )

    assert node.node_type == NodeType.LLM_INVOCATION
    assert node.token_count == 120


def test_tool_call_node_validation_and_privacy_guard() -> None:
    node = ToolCallNode.model_validate(
        {
            "node_id": "tool:e3",
            "node_type": "tool_call",
            "tool_name": "share_contacts",
            "action_class": "read_contacts",
            "argument_summary": {"field_summary": ["name", "email"]},
            "target_resource": "contacts.csv",
            "destination": "external.example",
            "timestamp": 3.0,
            "agent_id": "agent1",
            "session_id": "s1",
            "causal_context_id": "ctx17",
        }
    )

    assert node.node_type == NodeType.TOOL_CALL
    assert node.argument_summary["field_summary"] == ["name", "email"]

    with pytest.raises(ValidationError):
        ToolCallNode.model_validate(
            {
                **node.model_dump(mode="json"),
                "argument_summary": {"raw_args": {"contacts": ["Ada"]}},
            }
        )


def test_system_operation_node_validation() -> None:
    node = SystemOperationNode.model_validate(
        {
            "node_id": "sys:e4",
            "node_type": "system_operation",
            "operation_type": "file_read",
            "syscall_kind": "open",
            "resource_id": "contacts.csv",
            "destination": None,
            "byte_count": 2048,
            "timestamp": 4.0,
            "agent_id": "agent1",
            "session_id": "s1",
            "causal_context_id": "ctx17",
            "confidence": "high",
        }
    )

    assert node.node_type == NodeType.SYSTEM_OPERATION
    assert node.confidence == Confidence.HIGH


def test_finalized_system_operation_provenance_fields() -> None:
    node = SystemOperationNode.model_validate(
        {
            "node_id": "sys:send1",
            "node_type": "system_operation",
            "operation_type": "network_send",
            "action_class": "send_email",
            "resource_refs": ["contacts.csv"],
            "payload_refs": ["email_body_42"],
            "destination": "external.example",
            "destination_trust": "untrusted",
            "byte_count": 512,
            "timestamp": 4.0,
            "agent_id": "agent1",
            "session_id": "s1",
            "causal_context_id": "ctx17",
        }
    )

    assert node.resource_refs == ["contacts.csv"]
    assert node.payload_refs == ["email_body_42"]
    assert node.destination_trust == "untrusted"
    assert node.syscall_kind is None
    assert node.confidence is None


def test_data_object_node_validation() -> None:
    node = DataObjectNode.model_validate(
        {
            "node_id": "data:contacts.csv",
            "node_type": "data_object",
            "resource_id": "contacts.csv",
            "object_kind": "file",
            "content_hash": "sha256:contacts",
            "version": "v2",
            "sensitivity": "PII",
            "trust_label": "trusted",
            "owner": "user",
        }
    )

    assert node.node_type == NodeType.DATA_OBJECT
    assert node.sensitivity == Sensitivity.PII
    assert node.trust_label == TrustLabel.TRUSTED
    assert node.version == "v2"


def test_human_approval_node_validation_and_expiration_order() -> None:
    node = HumanApprovalNode.model_validate(
        {
            "node_id": "approval:e6",
            "node_type": "human_approval",
            "approver_id": "user1",
            "action_class": "upload",
            "resource_scope": "contacts.csv",
            "destination_scope": "external.example",
            "timestamp": 6.0,
            "expiration": 60.0,
            "session_id": "s1",
            "causal_context_id": "ctx17",
        }
    )

    assert node.node_type == NodeType.HUMAN_APPROVAL
    assert node.expiration == 60.0

    payload = node.model_dump(mode="json")
    payload["expiration"] = 5.0

    with pytest.raises(ValidationError, match="expiration"):
        HumanApprovalNode.model_validate(payload)


def test_human_approval_accepts_final_action_scope_name() -> None:
    node = HumanApprovalNode.model_validate(
        {
            "node_id": "approval:e7",
            "node_type": "human_approval",
            "approver_id": "user1",
            "action_scope": "send_email",
            "resource_scope": "email_body_42",
            "destination_scope": "external.example",
            "timestamp": 6.0,
            "expiration": None,
            "session_id": "s1",
            "causal_context_id": "ctx17",
        }
    )

    assert node.action_class == "send_email"
    assert node.action_scope == "send_email"


def test_invalid_node_type_fails_validation() -> None:
    with pytest.raises(ValidationError):
        parse_provenance_node(
            {
                "node_id": "bad:e1",
                "node_type": "bad_type",
                "timestamp": 1.0,
                "agent_id": None,
                "session_id": None,
                "causal_context_id": None,
            }
        )


def test_node_enum_and_extra_field_validation() -> None:
    with pytest.raises(ValidationError):
        DataObjectNode.model_validate(
            {
                "node_id": "data:contacts.csv",
                "node_type": "data_object",
                "resource_id": "contacts.csv",
                "object_kind": "file",
                "content_hash": None,
                "sensitivity": "private",
                "trust_label": "trusted",
                "owner": None,
            }
        )

    with pytest.raises(ValidationError):
        LLMInvocationNode.model_validate(
            {
                "node_id": "llm:e2",
                "node_type": "llm_invocation",
                "model_name": "gpt-4o",
                "prompt_hash": "sha256:p",
                "output_hash": "sha256:o",
                "token_count": 120,
                "timestamp": 2.0,
                "agent_id": "agent1",
                "session_id": "s1",
                "causal_context_id": "ctx17",
                "extra": "not allowed",
            }
        )


def test_parse_provenance_node_discriminates_variants() -> None:
    parsed = parse_provenance_node(
        {
            "node_id": "tool:e3",
            "node_type": "tool_call",
            "tool_name": "share_contacts",
            "action_class": "upload",
            "argument_summary": {},
            "target_resource": "contacts.csv",
            "destination": "external.example",
            "timestamp": 3.0,
            "agent_id": "agent1",
            "session_id": "s1",
            "causal_context_id": "ctx17",
        }
    )

    assert isinstance(parsed, ToolCallNode)


def test_valid_edge_passes_validation() -> None:
    edge = ProvenanceEdge.model_validate(
        {
            "edge_id": "edge:e2:e3",
            "source_id": "llm:e2",
            "target_id": "tool:e3",
            "edge_type": "invokes",
            "timestamp": 3.0,
            "derivation": "parent_event",
            "confidence": "high",
            "evidence_ref": "e3",
            "attributes": {"collector_event_kind": "tool_call"},
        }
    )

    assert edge.edge_type == EdgeType.INVOKES
    assert edge.derivation == Derivation.PARENT_EVENT
    assert edge.confidence == Confidence.HIGH
    assert edge.attributes == {"collector_event_kind": "tool_call"}


@pytest.mark.parametrize(
    "edge_type",
    ["read", "write", "produces", "input_to", "payload_of", "authorizes"],
)
def test_finalized_edge_types_are_valid(edge_type: str) -> None:
    edge = ProvenanceEdge.model_validate(
        {
            "edge_id": f"edge:{edge_type}",
            "source_id": "source",
            "target_id": "target",
            "edge_type": edge_type,
            "timestamp": 3.0,
            "derivation": "explicit_data_reference",
            "confidence": "high",
            "evidence_ref": "e3",
            "attributes": {},
        }
    )

    assert edge.edge_type.value == edge_type


def test_invalid_edge_type_fails_validation() -> None:
    with pytest.raises(ValidationError):
        ProvenanceEdge.model_validate(
            {
                "edge_id": "edge:e2:e3",
                "source_id": "llm:e2",
                "target_id": "tool:e3",
                "edge_type": "related_to",
                "timestamp": 3.0,
                "derivation": "parent_event",
                "confidence": "high",
                "evidence_ref": "e3",
            }
        )


def test_edge_rejects_invalid_fields() -> None:
    with pytest.raises(ValidationError):
        ProvenanceEdge.model_validate(
            {
                "edge_id": "edge:e2:e3",
                "source_id": "llm:e2",
                "target_id": "tool:e3",
                "edge_type": "invokes",
                "timestamp": 3.0,
                "derivation": "not_real",
                "confidence": "high",
                "evidence_ref": "e3",
            }
        )

    with pytest.raises(ValidationError):
        ProvenanceEdge.model_validate(
            {
                "edge_id": "edge:e2:e2",
                "source_id": "llm:e2",
                "target_id": "llm:e2",
                "edge_type": "invokes",
                "timestamp": 3.0,
                "derivation": "parent_event",
                "confidence": "high",
                "evidence_ref": "e3",
            }
        )

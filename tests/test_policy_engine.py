from __future__ import annotations

import json
from typing import Any

import pytest

from causalguard.graph import GraphBuilder, GraphStore
from causalguard.policy import (
    PolicyAction,
    PolicyEngine,
    protected_file_external_email_policy,
)


PROTECTED = "agentdojo:workspace:file:19"
OTHER = "agentdojo:workspace:file:7"
DESTINATION = "mailto:external@example.com"
TOOL_ID = "tool:send"


def _event(
    event_id: str,
    event_type: str,
    *,
    timestamp: float,
    attributes: dict[str, Any],
    parent_event_id: str | None = None,
    session_id: str = "policy-session",
    causal_context_id: str = "policy-context",
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "timestamp": timestamp,
        "event_type": event_type,
        "agent_id": "policy-test-agent",
        "session_id": session_id,
        "causal_context_id": causal_context_id,
        "parent_event_id": parent_event_id,
        "attributes": attributes,
    }


def _store(
    *,
    payload_resource: str = PROTECTED,
    destination: str = DESTINATION,
    approval_overrides: dict[str, Any] | None = None,
    include_prior_protected_read: bool = False,
    payload_object_kind: str = "cloud_drive_file",
    attachment_refs: list[str] | None = None,
    payload_sensitivity: str = "unknown",
) -> GraphStore:
    events: list[dict[str, Any]] = []
    if include_prior_protected_read:
        events.append(
            _event(
                "prior-read",
                "data_access",
                timestamp=1.0,
                attributes={
                    "operation_type": "file_read",
                    "read_refs": [PROTECTED],
                    "data_objects": {
                        PROTECTED: _object_metadata(PROTECTED),
                    },
                },
            )
        )
    events.extend(
        [
            _event(
                "llm",
                "llm_invocation",
                timestamp=9.0,
                attributes={
                    "model_name": "test",
                    "prompt_hash": "sha256:prompt",
                    "output_hash": "sha256:output",
                },
            ),
            _event(
                "send",
                "tool_call",
                timestamp=10.0,
                parent_event_id="llm",
                attributes={
                    "tool_name": "send_email",
                    "action_class": "send_email",
                    "target_resource": payload_resource,
                    "destination": destination,
                    "input_refs": [payload_resource],
                    "argument_summary": {"outgoing_attachment_refs": (
                        [payload_resource] if attachment_refs is None else attachment_refs
                    )},
                    "data_objects": {
                        payload_resource: {
                            **_object_metadata(payload_resource, object_kind=payload_object_kind),
                            "sensitivity": payload_sensitivity,
                        },
                    },
                },
            ),
        ]
    )
    if approval_overrides is not None:
        overrides = dict(approval_overrides)
        approval = {
            "approver_id": "user-1",
            "action_class": "send_email",
            "resource_scope": payload_resource,
            "destination_scope": destination,
            "expiration": 20.0,
        }
        event_overrides = {
            key: overrides.pop(key)
            for key in tuple(overrides)
            if key in {"session_id", "causal_context_id", "timestamp"}
        }
        approval.update(overrides)
        events.append(
            _event(
                "approval",
                "human_approval",
                timestamp=event_overrides.get("timestamp", 5.0),
                session_id=event_overrides.get("session_id", "policy-session"),
                causal_context_id=event_overrides.get(
                    "causal_context_id",
                    "policy-context",
                ),
                attributes=approval,
            )
        )

    store = GraphStore()
    GraphBuilder(store).process_trace(events)
    return store


def _object_metadata(
    resource_id: str,
    *,
    object_kind: str = "cloud_drive_file",
) -> dict[str, str | None]:
    suffix = resource_id.rsplit(":", 1)[-1]
    return {
        "node_id": f"data:{resource_id}:version:{suffix}",
        "resource_id": resource_id,
        "object_kind": object_kind,
        "content_hash": (
            None if object_kind == "unresolved_cloud_drive_attachment" else f"sha256:{suffix}"
        ),
        "version": (
            None if object_kind == "unresolved_cloud_drive_attachment" else f"sha256:{suffix}"
        ),
        "sensitivity": "unknown",
        "trust_label": "unknown",
    }


def _decision(
    store: GraphStore,
    *,
    trusted_recipients: tuple[str, ...] = (),
    trusted_domains: tuple[str, ...] = (),
):
    policy = protected_file_external_email_policy(
        protected_resource_ids=(PROTECTED,),
        trusted_recipients=trusted_recipients,
        trusted_domains=trusted_domains,
    )
    decision = PolicyEngine().evaluate(policy, store, TOOL_ID)
    assert decision is not None
    return decision


def test_protected_external_payload_without_approval_is_denied() -> None:
    decision = _decision(_store())

    assert decision.decision is PolicyAction.DENY
    assert decision.protected_resource_ids == (PROTECTED,)
    assert decision.destination_classification == "untrusted"
    assert decision.exception_status == "none"


@pytest.mark.parametrize(
    ("trusted_recipients", "trusted_domains"),
    [
        (("external@example.com",), ()),
        ((), ("example.com",)),
    ],
)
def test_protected_payload_to_trusted_destination_is_allowed(
    trusted_recipients: tuple[str, ...],
    trusted_domains: tuple[str, ...],
) -> None:
    decision = _decision(
        _store(),
        trusted_recipients=trusted_recipients,
        trusted_domains=trusted_domains,
    )

    assert decision.decision is PolicyAction.ALLOW
    assert decision.exception_status == "trusted_destination"


def test_prior_protected_read_without_protected_payload_is_allowed() -> None:
    decision = _decision(
        _store(
            payload_resource=OTHER,
            include_prior_protected_read=True,
        )
    )

    assert decision.decision is PolicyAction.ALLOW
    assert decision.protected_resource_ids == ()


def test_non_protected_external_attachment_is_allowed() -> None:
    decision = _decision(_store(payload_resource=OTHER))

    assert decision.decision is PolicyAction.ALLOW


def test_unresolved_attachment_requests_approval_instead_of_allowing() -> None:
    decision = _decision(
        _store(
            payload_resource="agentdojo:workspace:unresolved_attachment:deadbeef",
            payload_object_kind="unresolved_cloud_drive_attachment",
        )
    )

    assert decision.decision is PolicyAction.REQUEST_APPROVAL
    assert decision.evidence_status == "missing"
    assert decision.protected_resource_ids == ()


def test_exact_valid_approval_allows_with_audit() -> None:
    decision = _decision(_store(approval_overrides={}))

    assert decision.decision is PolicyAction.ALLOW_WITH_AUDIT
    assert decision.exception_status == "valid_approval"
    assert "approval:approval" in decision.exception_evidence_node_ids
    assert "approval:approval" not in decision.evidence_node_ids


@pytest.mark.parametrize(
    "approval_overrides",
    [
        {"action_class": "upload"},
        {"resource_scope": OTHER},
        {"destination_scope": "mailto:other@example.com"},
        {"session_id": "other-session"},
        {"causal_context_id": "other-context"},
        {"timestamp": 1.0, "expiration": 5.0},
    ],
)
def test_inexact_or_expired_approval_does_not_apply(
    approval_overrides: dict[str, Any],
) -> None:
    decision = _decision(_store(approval_overrides=dict(approval_overrides)))

    assert decision.decision is PolicyAction.DENY
    assert decision.exception_status == "none"


def test_structured_decision_is_privacy_safe() -> None:
    decision = _decision(_store())
    exported = decision.model_dump_json()

    assert "private file contents" not in exported
    assert "raw email body" not in exported
    assert json.loads(exported)["policy_id"] == "protected_file_external_email"


def test_action_vocabulary_is_stable() -> None:
    assert {item.value for item in PolicyAction} == {
        "allow",
        "deny",
        "request_approval",
        "allow_with_audit",
        "escalate",
    }


def test_witness_is_exactly_the_direct_attachment_edge_without_a_read() -> None:
    store = _store(include_prior_protected_read=True)
    decision = _decision(store)
    edges = [
        edge for edge in store.edges()
        if edge.target_id == TOOL_ID and edge.edge_type.value == "input_to"
    ]
    assert len(edges) == 1
    assert decision.evidence_edge_ids == (edges[0].edge_id,)
    assert set(decision.evidence_node_ids) == {edges[0].source_id, TOOL_ID}
    assert decision.recipient_addresses == ("external@example.com",)
    assert "Recipient metadata: untrusted" in decision.explanation
    assert "Exception: none" in decision.explanation


def test_arbitrary_protected_input_is_not_an_outgoing_attachment() -> None:
    decision = _decision(_store(attachment_refs=[]))
    assert decision.decision is PolicyAction.ALLOW
    assert decision.protected_resource_ids == ()
    assert decision.evidence_edge_ids == ()
    assert decision.evidence_node_ids == (TOOL_ID,)


@pytest.mark.parametrize("destination", [None, "invalid", "mailto:a@example.com,", "mailto:a@example.com,broken"])
def test_unresolved_destination_is_held(destination) -> None:
    decision = _decision(_store(destination=destination))
    assert decision.decision is PolicyAction.REQUEST_APPROVAL
    assert decision.evidence_status == "missing"
    assert decision.destination_classification == "unresolved"


@pytest.mark.parametrize("params", [
    {"protected_resource_patterns": ("agentdojo:workspace:file:*",)},
    {"protected_sensitivities": ("PII",)},
])
def test_configured_patterns_and_supported_sensitivity_labels(params) -> None:
    store = _store(payload_resource=OTHER, payload_sensitivity="PII")
    policy = protected_file_external_email_policy(**params)
    decision = PolicyEngine().evaluate(policy, store, TOOL_ID)
    assert decision.decision is PolicyAction.DENY
    assert decision.protected_resource_ids == (OTHER,)


def test_sensitivity_is_not_protected_without_trusted_configuration() -> None:
    decision = _decision(_store(payload_resource=OTHER, payload_sensitivity="PII"))
    assert decision.decision is PolicyAction.ALLOW


def test_missing_declared_attachment_edge_is_held() -> None:
    decision = _decision(_store(payload_resource=OTHER, attachment_refs=[PROTECTED]))
    assert decision.decision is PolicyAction.REQUEST_APPROVAL
    assert decision.evidence_edge_ids == ()


def test_missing_attachment_role_metadata_is_not_inferred_from_input_edges() -> None:
    original = _store()
    store = GraphStore()
    for node in original.nodes():
        if node.node_id == TOOL_ID:
            node = node.model_copy(update={"argument_summary": {}})
        store.add_node(node)
    for edge in original.edges():
        store.add_edge(edge)
    decision = _decision(store)
    assert decision.decision is PolicyAction.REQUEST_APPROVAL
    assert decision.protected_resource_ids == ()


@pytest.mark.parametrize("approved_count, expected", [(0, PolicyAction.DENY),
                                                     (1, PolicyAction.DENY),
                                                     (2, PolicyAction.ALLOW_WITH_AUDIT)])
def test_approval_must_cover_every_attachment_with_no_single_target(approved_count, expected) -> None:
    resources = [PROTECTED, OTHER]
    event = _event("send", "tool_call", timestamp=10.0, attributes={
        "tool_name": "send_email", "action_class": "send_email",
        "destination": DESTINATION, "target_resource": None,
        "input_refs": resources,
        "argument_summary": {"outgoing_attachment_refs": resources},
        "data_objects": {r: _object_metadata(r) for r in resources},
    })
    approvals = [
        _event(f"approval-{index}", "human_approval", timestamp=5.0, attributes={
            "approver_id": "user-1", "action_class": "send_email",
            "resource_scope": r, "destination_scope": DESTINATION, "expiration": 20.0,
        })
        for index, r in enumerate(resources[:approved_count])
    ]
    store = GraphStore()
    GraphBuilder(store).process_trace([event, *approvals])
    policy = protected_file_external_email_policy(protected_resource_ids=tuple(resources))
    decision = PolicyEngine().evaluate(policy, store, TOOL_ID)
    assert decision.decision is expected
    assert len(decision.exception_evidence_node_ids) == approved_count
    assert len(decision.evidence_edge_ids) == 2


def test_partial_recipient_scope_approval_does_not_cover_cc_bcc() -> None:
    decision = _decision(_store(
        destination=DESTINATION + ",mailto:other@example.net",
        approval_overrides={"destination_scope": DESTINATION},
    ))
    assert decision.decision is PolicyAction.DENY


def test_approval_requires_direct_authorizes_edge() -> None:
    original = _store(approval_overrides={})
    store = GraphStore()
    for node in original.nodes():
        store.add_node(node)
    for edge in original.edges():
        if edge.edge_type.value != "authorizes":
            store.add_edge(edge)
    assert _decision(store).decision is PolicyAction.DENY

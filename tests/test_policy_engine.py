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
                    "data_objects": {
                        payload_resource: _object_metadata(payload_resource),
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


def _object_metadata(resource_id: str) -> dict[str, str]:
    suffix = resource_id.rsplit(":", 1)[-1]
    return {
        "node_id": f"data:{resource_id}:version:{suffix}",
        "resource_id": resource_id,
        "object_kind": "cloud_drive_file",
        "content_hash": f"sha256:{suffix}",
        "version": f"sha256:{suffix}",
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


def test_exact_valid_approval_allows_with_audit() -> None:
    decision = _decision(_store(approval_overrides={}))

    assert decision.decision is PolicyAction.ALLOW_WITH_AUDIT
    assert decision.exception_status == "valid_approval"
    assert "approval:approval" in decision.evidence_node_ids


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

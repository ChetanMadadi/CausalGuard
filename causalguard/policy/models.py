"""Validated models for CausalGuard's trigger-driven policy pipeline."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from causalguard.schema.common import non_empty_string


class PolicyAction(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUEST_APPROVAL = "request_approval"
    ALLOW_WITH_AUDIT = "allow_with_audit"
    ESCALATE = "escalate"


class _PolicyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TriggerSpec(_PolicyModel):
    kind: Literal["tool_call"] = "tool_call"
    action_class: str

    @field_validator("action_class")
    @classmethod
    def validate_action_class(cls, value: str) -> str:
        return non_empty_string(value)


class SelectSpec(_PolicyModel):
    kind: Literal["bounded_outgoing_provenance"] = "bounded_outgoing_provenance"


class PredicateSpec(_PolicyModel):
    kind: Literal["protected_resource_external_destination"] = (
        "protected_resource_external_destination"
    )


class ExceptionSpec(_PolicyModel):
    kinds: tuple[Literal["trusted_destination", "exact_human_approval"], ...] = (
        "trusted_destination",
        "exact_human_approval",
    )


class ActionSpec(_PolicyModel):
    on_violation: PolicyAction = PolicyAction.DENY
    on_trusted_destination: PolicyAction = PolicyAction.ALLOW
    on_valid_approval: PolicyAction = PolicyAction.ALLOW_WITH_AUDIT
    on_no_match: PolicyAction = PolicyAction.ALLOW


class PolicyParams(_PolicyModel):
    protected_resource_ids: tuple[str, ...] = ()
    protected_resource_patterns: tuple[str, ...] = ()
    trusted_recipients: tuple[str, ...] = ()
    trusted_domains: tuple[str, ...] = ()
    max_provenance_depth: int = Field(default=6, ge=1, le=64)
    time_window: float | None = Field(default=None, ge=0)
    approval_expiration_behavior: Literal[
        "allow_unbounded", "deny_unbounded"
    ] = "allow_unbounded"

    @field_validator(
        "protected_resource_ids",
        "protected_resource_patterns",
        "trusted_recipients",
        "trusted_domains",
    )
    @classmethod
    def validate_string_tuple(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(non_empty_string(item) for item in value)


class PolicyDefinition(_PolicyModel):
    policy_id: str
    trigger: TriggerSpec
    select: SelectSpec
    predicate: PredicateSpec
    exception: ExceptionSpec
    action: ActionSpec
    params: PolicyParams

    @field_validator("policy_id")
    @classmethod
    def validate_policy_id(cls, value: str) -> str:
        return non_empty_string(value)


class PolicyDecision(_PolicyModel):
    policy_id: str
    decision: PolicyAction
    triggering_tool_call_id: str
    protected_resource_ids: tuple[str, ...]
    destination_classification: Literal["trusted", "untrusted"]
    exception_status: Literal[
        "none", "trusted_destination", "valid_approval"
    ]
    evidence_node_ids: tuple[str, ...]
    evidence_edge_ids: tuple[str, ...]
    explanation: str

    @field_validator("policy_id", "triggering_tool_call_id", "explanation")
    @classmethod
    def validate_required_string(cls, value: str) -> str:
        return non_empty_string(value)

    @field_validator("protected_resource_ids")
    @classmethod
    def validate_resource_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(non_empty_string(item) for item in value)


def protected_file_external_email_policy(
    *,
    protected_resource_ids: tuple[str, ...] = (),
    protected_resource_patterns: tuple[str, ...] = (),
    trusted_recipients: tuple[str, ...] = (),
    trusted_domains: tuple[str, ...] = (),
    max_provenance_depth: int = 6,
    time_window: float | None = None,
    approval_expiration_behavior: Literal[
        "allow_unbounded", "deny_unbounded"
    ] = "allow_unbounded",
) -> PolicyDefinition:
    """Build the first deterministic CausalGuard policy."""

    return PolicyDefinition(
        policy_id="protected_file_external_email",
        trigger=TriggerSpec(action_class="send_email"),
        select=SelectSpec(),
        predicate=PredicateSpec(),
        exception=ExceptionSpec(),
        action=ActionSpec(),
        params=PolicyParams(
            protected_resource_ids=protected_resource_ids,
            protected_resource_patterns=protected_resource_patterns,
            trusted_recipients=trusted_recipients,
            trusted_domains=trusted_domains,
            max_provenance_depth=max_provenance_depth,
            time_window=time_window,
            approval_expiration_behavior=approval_expiration_behavior,
        ),
    )

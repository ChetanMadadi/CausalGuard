"""Validated models for CausalGuard's trigger-driven policy pipeline."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from causalguard.schema.common import non_empty_string
from causalguard.schema.nodes import Sensitivity
from causalguard.schema.edges import EdgeType


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
    kind: Literal["bounded_outgoing_provenance", "bounded_data_flow"] = "bounded_outgoing_provenance"


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
    on_missing_evidence: PolicyAction = PolicyAction.REQUEST_APPROVAL
    on_trusted_destination: PolicyAction = PolicyAction.ALLOW
    on_valid_approval: PolicyAction = PolicyAction.ALLOW_WITH_AUDIT
    on_no_match: PolicyAction = PolicyAction.ALLOW


class PolicyParams(_PolicyModel):
    protected_resource_ids: tuple[str, ...] = ()
    protected_resource_patterns: tuple[str, ...] = ()
    protected_sensitivities: tuple[Sensitivity, ...] = ()
    trusted_recipients: tuple[str, ...] = ()
    trusted_domains: tuple[str, ...] = ()
    # Retained for config compatibility; not used by the immediate policy.
    max_provenance_depth: int = Field(default=6, ge=1, le=64)
    time_window: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    time_window_unit: Literal["seconds", "ordinal"] | None = None
    max_hops: int = Field(default=3, ge=1, le=64)
    max_search_states: int = Field(default=10000, ge=1, le=1000000)
    allowed_edge_types: tuple[EdgeType, ...] = (EdgeType.READ, EdgeType.WRITE, EdgeType.INPUT_TO)
    required_evidence: Literal["attested_copy_or_root"] = "attested_copy_or_root"
    approval_expiration_behavior: Literal[
        "allow_unbounded", "deny_unbounded"
    ] = "allow_unbounded"

    @field_validator("allowed_edge_types")
    @classmethod
    def validate_flow_edges(cls, value: tuple[EdgeType, ...]) -> tuple[EdgeType, ...]:
        if not value or set(value) - {EdgeType.READ, EdgeType.WRITE, EdgeType.INPUT_TO}:
            raise ValueError("only read, write and input_to are supported data-flow edges")
        return value

    @model_validator(mode="after")
    def validate_time_units(self) -> "PolicyParams":
        if self.time_window is not None and self.time_window_unit is None:
            raise ValueError("time_window requires explicit time_window_unit")
        return self

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

    @model_validator(mode="after")
    def validate_incomplete_action(self) -> "PolicyDefinition":
        if (self.select.kind == "bounded_data_flow" and self.action.on_missing_evidence
                in {PolicyAction.ALLOW, PolicyAction.ALLOW_WITH_AUDIT}):
            raise ValueError("incomplete path evidence requires a non-executing action")
        return self

    @field_validator("policy_id")
    @classmethod
    def validate_policy_id(cls, value: str) -> str:
        return non_empty_string(value)


class PathWitness(_PolicyModel):
    node_ids: tuple[str, ...]
    edge_ids: tuple[str, ...]
    source_resource_id: str
    attachment_resource_id: str


class PolicyDecision(_PolicyModel):
    policy_id: str
    decision: PolicyAction
    triggering_tool_call_id: str
    protected_resource_ids: tuple[str, ...]
    destination_classification: Literal["trusted", "untrusted", "unresolved"]
    exception_status: Literal[
        "none", "trusted_destination", "valid_approval"
    ]
    evidence_status: Literal["complete", "missing"] = "complete"
    evidence_node_ids: tuple[str, ...]
    evidence_edge_ids: tuple[str, ...]
    recipient_addresses: tuple[str, ...] = ()
    exception_evidence_node_ids: tuple[str, ...] = ()
    exception_evidence_edge_ids: tuple[str, ...] = ()
    matching_paths: tuple[PathWitness, ...] = ()
    search_status: Literal["not_applicable", "match", "no_match_within_bounds", "incomplete"] = "not_applicable"
    incomplete_reasons: tuple[str, ...] = ()
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
    protected_sensitivities: tuple[Sensitivity, ...] = (),
    trusted_recipients: tuple[str, ...] = (),
    trusted_domains: tuple[str, ...] = (),
    max_provenance_depth: int = 6,
    time_window: float | None = None,
    time_window_unit: Literal["seconds", "ordinal"] | None = None,
    path_mode: bool = False,
    max_hops: int = 3,
    max_search_states: int = 10000,
    allowed_edge_types: tuple[EdgeType, ...] = (EdgeType.READ, EdgeType.WRITE, EdgeType.INPUT_TO),
    approval_expiration_behavior: Literal[
        "allow_unbounded", "deny_unbounded"
    ] = "allow_unbounded",
) -> PolicyDefinition:
    """Build the first deterministic CausalGuard policy."""

    return PolicyDefinition(
        policy_id="protected_file_external_email",
        trigger=TriggerSpec(action_class="send_email"),
        select=SelectSpec(kind="bounded_data_flow" if path_mode else "bounded_outgoing_provenance"),
        predicate=PredicateSpec(),
        exception=ExceptionSpec(),
        action=ActionSpec(),
        params=PolicyParams(
            protected_resource_ids=protected_resource_ids,
            protected_resource_patterns=protected_resource_patterns,
            protected_sensitivities=protected_sensitivities,
            trusted_recipients=trusted_recipients,
            trusted_domains=trusted_domains,
            max_provenance_depth=max_provenance_depth,
            time_window=time_window,
            time_window_unit=time_window_unit,
            max_hops=max_hops,
            max_search_states=max_search_states,
            allowed_edge_types=allowed_edge_types,
            approval_expiration_behavior=approval_expiration_behavior,
        ),
    )

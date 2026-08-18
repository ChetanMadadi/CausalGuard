"""Typed node schemas for the provenance multigraph."""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from causalguard.schema.common import (
    ensure_dict,
    non_empty_string,
    non_negative_finite_number,
    non_negative_integer,
    optional_non_empty_string,
)
from causalguard.schema.edges import Confidence
from causalguard.schema.privacy import assert_no_raw_content_keys


class NodeType(str, Enum):
    LLM_INVOCATION = "llm_invocation"
    TOOL_CALL = "tool_call"
    SYSTEM_OPERATION = "system_operation"
    DATA_OBJECT = "data_object"
    HUMAN_APPROVAL = "human_approval"


class Sensitivity(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    PII = "PII"
    CREDENTIAL = "credential"
    HEALTH = "health"
    PAYMENT = "payment"
    LOCATION = "location"
    UNKNOWN = "unknown"


class TrustLabel(str, Enum):
    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"
    EXTERNAL = "external"
    USER_APPROVED = "user_approved"
    UNKNOWN = "unknown"


class _BaseNode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str

    @field_validator("node_id")
    @classmethod
    def validate_node_id(cls, value: str) -> str:
        return non_empty_string(value)


class _TimedAgentNode(_BaseNode):
    timestamp: float
    agent_id: str | None
    session_id: str | None
    causal_context_id: str | None

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, value: float) -> float:
        return non_negative_finite_number(value)

    @field_validator("agent_id", "session_id", "causal_context_id")
    @classmethod
    def validate_optional_ids(cls, value: str | None) -> str | None:
        return optional_non_empty_string(value)


class LLMInvocationNode(_TimedAgentNode):
    node_type: Literal[NodeType.LLM_INVOCATION] = NodeType.LLM_INVOCATION
    model_name: str | None
    prompt_hash: str | None
    prompt_ref: str | None = None
    output_hash: str | None
    output_ref: str | None = None
    token_count: int | None = None

    @field_validator("model_name", "prompt_hash", "prompt_ref", "output_hash", "output_ref")
    @classmethod
    def validate_optional_strings(cls, value: str | None) -> str | None:
        return optional_non_empty_string(value)

    @field_validator("token_count")
    @classmethod
    def validate_token_count(cls, value: int | None) -> int | None:
        return non_negative_integer(value)


class ToolCallNode(_TimedAgentNode):
    node_type: Literal[NodeType.TOOL_CALL] = NodeType.TOOL_CALL
    tool_name: str
    action_class: str | None
    argument_summary: dict[str, object]
    target_resource: str | None
    destination: str | None

    @field_validator("tool_name")
    @classmethod
    def validate_tool_name(cls, value: str) -> str:
        return non_empty_string(value)

    @field_validator("action_class", "target_resource", "destination")
    @classmethod
    def validate_optional_strings(cls, value: str | None) -> str | None:
        return optional_non_empty_string(value)

    @field_validator("argument_summary")
    @classmethod
    def validate_argument_summary(cls, value: object) -> dict[str, object]:
        summary = ensure_dict(value)
        assert_no_raw_content_keys(summary, path="argument_summary")
        return summary


class SystemOperationNode(_TimedAgentNode):
    node_type: Literal[NodeType.SYSTEM_OPERATION] = NodeType.SYSTEM_OPERATION
    operation_type: str
    action_class: str | None = None
    syscall_kind: str | None = None
    resource_id: str | None = None
    resource_refs: list[str] = Field(default_factory=list)
    payload_refs: list[str] = Field(default_factory=list)
    destination: str | None = None
    destination_trust: str | None = None
    byte_count: int | None = None
    confidence: Confidence | None = None

    @field_validator("operation_type")
    @classmethod
    def validate_operation_type(cls, value: str) -> str:
        return non_empty_string(value)

    @field_validator(
        "action_class",
        "syscall_kind",
        "resource_id",
        "destination",
        "destination_trust",
    )
    @classmethod
    def validate_optional_strings(cls, value: str | None) -> str | None:
        return optional_non_empty_string(value)

    @field_validator("byte_count")
    @classmethod
    def validate_byte_count(cls, value: int | None) -> int | None:
        return non_negative_integer(value)

    @field_validator("resource_refs", "payload_refs")
    @classmethod
    def validate_provenance_refs(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("provenance references must be a list")
        return [non_empty_string(item) for item in value]


class DataObjectNode(_BaseNode):
    node_type: Literal[NodeType.DATA_OBJECT] = NodeType.DATA_OBJECT
    resource_id: str
    object_kind: str
    content_hash: str | None
    version: str | None = None
    sensitivity: Sensitivity
    trust_label: TrustLabel | None
    owner: str | None

    @field_validator("resource_id", "object_kind")
    @classmethod
    def validate_required_strings(cls, value: str) -> str:
        return non_empty_string(value)

    @field_validator("content_hash", "version", "owner")
    @classmethod
    def validate_optional_strings(cls, value: str | None) -> str | None:
        return optional_non_empty_string(value)


class HumanApprovalNode(_BaseNode):
    node_type: Literal[NodeType.HUMAN_APPROVAL] = NodeType.HUMAN_APPROVAL
    approver_id: str
    action_class: str = Field(validation_alias=AliasChoices("action_class", "action_scope"))
    resource_scope: str | None
    destination_scope: str | None
    timestamp: float
    expiration: float | None
    session_id: str | None
    causal_context_id: str | None

    @field_validator("approver_id", "action_class")
    @classmethod
    def validate_required_strings(cls, value: str) -> str:
        return non_empty_string(value)

    @field_validator("resource_scope", "destination_scope", "session_id", "causal_context_id")
    @classmethod
    def validate_optional_strings(cls, value: str | None) -> str | None:
        return optional_non_empty_string(value)

    @field_validator("timestamp", "expiration")
    @classmethod
    def validate_times(cls, value: float | None) -> float | None:
        if value is None:
            return value
        return non_negative_finite_number(value)

    @model_validator(mode="after")
    def validate_expiration_order(self) -> "HumanApprovalNode":
        if self.expiration is not None and self.expiration < self.timestamp:
            raise ValueError("expiration must be greater than or equal to timestamp")
        return self

    @property
    def action_scope(self) -> str:
        """Final-schema name for the backward-compatible ``action_class`` field."""
        return self.action_class


ProvenanceNode = Annotated[
    Union[
        LLMInvocationNode,
        ToolCallNode,
        SystemOperationNode,
        DataObjectNode,
        HumanApprovalNode,
    ],
    Field(discriminator="node_type"),
]

_PROVENANCE_NODE_ADAPTER = TypeAdapter(ProvenanceNode)


def parse_provenance_node(value: object) -> ProvenanceNode:
    return _PROVENANCE_NODE_ADAPTER.validate_python(value)

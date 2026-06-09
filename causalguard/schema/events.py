"""Normalized collector event schema."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from causalguard.schema.common import (
    ensure_dict,
    non_empty_string,
    non_negative_finite_number,
    optional_non_empty_string,
)
from causalguard.schema.privacy import assert_no_raw_content_keys


class EventType(str, Enum):
    USER_INPUT = "user_input"
    LLM_INVOCATION = "llm_invocation"
    TOOL_CALL = "tool_call"
    SYSTEM_OPERATION = "system_operation"
    DATA_ACCESS = "data_access"
    HUMAN_APPROVAL = "human_approval"
    NETWORK_SEND = "network_send"
    DEVICE_COMMAND = "device_command"


class NormalizedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    timestamp: float
    event_type: EventType
    agent_id: str | None
    session_id: str | None
    causal_context_id: str | None
    parent_event_id: str | None
    attributes: dict[str, Any]

    @field_validator("event_id")
    @classmethod
    def validate_event_id(cls, value: str) -> str:
        return non_empty_string(value)

    @field_validator("agent_id", "session_id", "causal_context_id", "parent_event_id")
    @classmethod
    def validate_optional_ids(cls, value: str | None) -> str | None:
        return optional_non_empty_string(value)

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, value: float) -> float:
        return non_negative_finite_number(value)

    @field_validator("attributes")
    @classmethod
    def validate_attributes(cls, value: Any) -> dict[str, Any]:
        attributes = ensure_dict(value)
        assert_no_raw_content_keys(attributes, path="attributes")
        return attributes

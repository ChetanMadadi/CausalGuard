"""Typed edge schema for the provenance multigraph."""

from __future__ import annotations

from enum import Enum

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from causalguard.schema.common import (
    ensure_dict,
    non_empty_string,
    non_negative_finite_number,
    optional_non_empty_string,
)
from causalguard.schema.privacy import assert_no_raw_content_keys


class EdgeType(str, Enum):
    INVOKES = "invokes"
    TRIGGERS = "triggers"
    READ = "read"
    WRITE = "write"
    PRODUCES = "produces"
    INPUT_TO = "input_to"
    PAYLOAD_OF = "payload_of"
    # Legacy coarse relation. New collectors should emit an evidence-backed
    # relation above whenever the operation semantics are known.
    ACCESSES = "accesses"
    AUTHORIZES = "authorizes"


class Derivation(str, Enum):
    PARENT_EVENT = "parent_event"
    CAUSAL_CONTEXT = "causal_context"
    RUNTIME_OBSERVATION = "runtime_observation"
    EXPLICIT_DATA_REFERENCE = "explicit_data_reference"
    TEMPORAL_WINDOW = "temporal_window"
    EXPLICIT_APPROVAL_SCOPE = "explicit_approval_scope"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


class ProvenanceEdge(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    edge_id: str
    source_id: str
    target_id: str
    edge_type: EdgeType
    timestamp: float
    derivation: Derivation
    confidence: Confidence
    evidence_ref: str | None
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("edge_id", "source_id", "target_id")
    @classmethod
    def validate_required_ids(cls, value: str) -> str:
        return non_empty_string(value)

    @field_validator("evidence_ref")
    @classmethod
    def validate_evidence_ref(cls, value: str | None) -> str | None:
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

    @model_validator(mode="after")
    def validate_distinct_endpoints(self) -> "ProvenanceEdge":
        if self.source_id == self.target_id:
            raise ValueError("edge source_id and target_id must be different")
        return self

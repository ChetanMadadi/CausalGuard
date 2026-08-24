"""Framework-neutral evidence types for AgentDojo domain extractors."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from agentdojo.functions_runtime import FunctionReturnType, TaskEnvironment

from causalguard.schema.events import EventType


@dataclass(frozen=True)
class DomainObjectEvidence:
    """Privacy-safe identity and version metadata for one concrete object."""

    reference: str
    node_id: str
    object_kind: str
    content_hash: str | None = None
    version: str | None = None
    sensitivity: str = "unknown"
    trust_label: str = "unknown"
    owner: str | None = None

    def metadata(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "resource_id": self.reference,
            "object_kind": self.object_kind,
            "content_hash": self.content_hash,
            "version": self.version,
            "sensitivity": self.sensitivity,
            "trust_label": self.trust_label,
            "owner": self.owner,
        }


@dataclass(frozen=True)
class DomainOperationEvidence:
    """Evidence-backed domain effects of one successful runtime call."""

    event_type: EventType
    operation_type: str
    action_class: str
    read_objects: tuple[DomainObjectEvidence, ...] = ()
    write_objects: tuple[DomainObjectEvidence, ...] = ()
    payload_objects: tuple[DomainObjectEvidence, ...] = ()
    tool_input_objects: tuple[DomainObjectEvidence, ...] = ()
    llm_generated_objects: tuple[DomainObjectEvidence, ...] = ()
    destination: str | None = None
    target_resource: str | None = None
    byte_count: int | None = None

    def all_objects(self) -> tuple[DomainObjectEvidence, ...]:
        objects: dict[str, DomainObjectEvidence] = {}
        for collection in (
            self.read_objects,
            self.write_objects,
            self.payload_objects,
            self.tool_input_objects,
            self.llm_generated_objects,
        ):
            for item in collection:
                objects[item.reference] = item
        return tuple(objects.values())


@dataclass(frozen=True)
class ProposedActionEvidence:
    """Privacy-safe objects and destination known before a tool executes."""

    action_class: str
    tool_input_objects: tuple[DomainObjectEvidence, ...] = ()
    llm_generated_objects: tuple[DomainObjectEvidence, ...] = ()
    destination: str | None = None
    target_resource: str | None = None


@dataclass(frozen=True)
class ToolExecutionContext:
    """In-memory execution context supplied to a domain extractor."""

    function_name: str
    arguments: Mapping[str, object]
    result: FunctionReturnType
    error: str | None
    environment_before: TaskEnvironment | None
    environment_after: TaskEnvironment | None


@dataclass(frozen=True)
class ToolProposalContext:
    """Read-only context available immediately before runtime execution."""

    function_name: str
    arguments: Mapping[str, object]
    environment: TaskEnvironment | None


class AgentDojoDomainExtractor(Protocol):
    """Map supported runtime calls to concrete domain provenance."""

    def supports(self, function_name: str) -> bool:
        """Return whether this extractor handles the function."""

    def extract(
        self,
        context: ToolExecutionContext,
    ) -> Sequence[DomainOperationEvidence]:
        """Return only directly observed operations and objects."""

    def propose(
        self,
        context: ToolProposalContext,
    ) -> ProposedActionEvidence | None:
        """Return evidence available before the proposed function executes."""

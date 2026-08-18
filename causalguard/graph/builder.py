"""Build typed provenance graphs from normalized events."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from causalguard.graph.store import GraphStore, GraphStoreError
from causalguard.schema.edges import Confidence, Derivation, EdgeType
from causalguard.schema.events import EventType, NormalizedEvent
from causalguard.schema.nodes import HumanApprovalNode, SystemOperationNode, ToolCallNode


@dataclass(frozen=True)
class GraphBuilderConfig:
    """Configuration for deterministic graph construction."""

    infer_causal_context_edges: bool = True


class GraphBuilderError(ValueError):
    """Raised when an event cannot be converted into graph structure."""


@dataclass(frozen=True)
class _DataRef:
    node_id: str
    resource_id: str
    object_kind: str
    content_hash: str | None
    version: str | None
    sensitivity: str
    trust_label: str | None
    owner: str | None


_SYSTEM_EVENT_TYPES = {
    EventType.SYSTEM_OPERATION,
    EventType.DATA_ACCESS,
    EventType.NETWORK_SEND,
    EventType.DEVICE_COMMAND,
}


class GraphBuilder:
    """Convert framework-neutral normalized events into one provenance multigraph."""

    def __init__(
        self,
        graph_store: GraphStore,
        config: GraphBuilderConfig | None = None,
    ) -> None:
        self.graph_store = graph_store
        self.config = config or GraphBuilderConfig()
        self._events_by_id: dict[str, NormalizedEvent] = {}
        self._primary_node_by_event_id: dict[str, str] = {}
        self._node_ids_by_event_id: dict[str, list[str]] = {}

    def process_event(self, event: NormalizedEvent | dict[str, Any]) -> GraphStore:
        parsed = NormalizedEvent.model_validate(event)
        if parsed.event_id in self._events_by_id:
            raise GraphBuilderError(f"event already processed: {parsed.event_id}")

        node_ids = self._insert_nodes_for_event(parsed)
        self._events_by_id[parsed.event_id] = parsed
        self._node_ids_by_event_id[parsed.event_id] = node_ids
        if node_ids and parsed.event_type is not EventType.USER_INPUT:
            self._primary_node_by_event_id[parsed.event_id] = node_ids[0]

        self._reconcile_edges()
        return self.graph_store

    def process_trace(
        self,
        events: Iterable[NormalizedEvent | dict[str, Any]],
    ) -> GraphStore:
        for event in events:
            self.process_event(event)
        return self.graph_store

    def _insert_nodes_for_event(self, event: NormalizedEvent) -> list[str]:
        if event.event_type is EventType.USER_INPUT:
            return []

        primary_payload = self._primary_node_payload(event)
        primary_node_id = str(primary_payload["node_id"])
        self._add_node(primary_payload)

        node_ids = [primary_node_id]
        for data_ref in self._data_refs_for_event(event):
            data_node_id = self._ensure_data_node(data_ref)
            if data_node_id not in node_ids:
                node_ids.append(data_node_id)

        return node_ids

    def _primary_node_payload(self, event: NormalizedEvent) -> dict[str, Any]:
        if event.event_type is EventType.LLM_INVOCATION:
            return {
                "node_id": f"llm:{event.event_id}",
                "node_type": "llm_invocation",
                "model_name": event.attributes.get("model_name"),
                "prompt_hash": event.attributes.get("prompt_hash"),
                "prompt_ref": event.attributes.get("prompt_ref"),
                "output_hash": event.attributes.get("output_hash"),
                "output_ref": event.attributes.get("output_ref"),
                "token_count": event.attributes.get("token_count"),
                "timestamp": event.timestamp,
                "agent_id": event.agent_id,
                "session_id": event.session_id,
                "causal_context_id": event.causal_context_id,
            }

        if event.event_type is EventType.TOOL_CALL:
            return {
                "node_id": f"tool:{event.event_id}",
                "node_type": "tool_call",
                "tool_name": self._required_attribute(event, "tool_name"),
                "action_class": event.attributes.get("action_class"),
                "argument_summary": event.attributes.get("argument_summary", {}),
                "target_resource": event.attributes.get("target_resource"),
                "destination": event.attributes.get("destination"),
                "timestamp": event.timestamp,
                "agent_id": event.agent_id,
                "session_id": event.session_id,
                "causal_context_id": event.causal_context_id,
            }

        if event.event_type is EventType.HUMAN_APPROVAL:
            action_class = event.attributes.get("action_class")
            if action_class is None:
                action_class = self._required_attribute(event, "action_scope")
            return {
                "node_id": f"approval:{event.event_id}",
                "node_type": "human_approval",
                "approver_id": self._required_attribute(event, "approver_id"),
                "action_class": action_class,
                "resource_scope": event.attributes.get("resource_scope"),
                "destination_scope": event.attributes.get("destination_scope"),
                "timestamp": event.timestamp,
                "expiration": event.attributes.get("expiration"),
                "session_id": event.session_id,
                "causal_context_id": event.causal_context_id,
            }

        if event.event_type in _SYSTEM_EVENT_TYPES:
            return self._system_operation_payload(event)

        raise GraphBuilderError(f"unsupported event type: {event.event_type.value}")

    def _system_operation_payload(self, event: NormalizedEvent) -> dict[str, Any]:
        if event.event_type is EventType.DATA_ACCESS:
            operation_type = self._required_attribute(event, "operation_type")
            resource_id = event.attributes.get("resource_id")
            if resource_id is None and not event.attributes.get("resource_refs"):
                raise GraphBuilderError(
                    f"event {event.event_id} missing required resource reference"
                )
            destination = event.attributes.get("destination")
        elif event.event_type is EventType.NETWORK_SEND:
            operation_type = event.attributes.get("operation_type", "network_send")
            resource_id = event.attributes.get("resource_id")
            destination = self._required_attribute(event, "destination")
        elif event.event_type is EventType.DEVICE_COMMAND:
            operation_type = event.attributes.get("operation_type", "device_command")
            resource_id = event.attributes.get("resource_id") or event.attributes.get("device_id")
            destination = event.attributes.get("destination")
        else:
            operation_type = self._required_attribute(event, "operation_type")
            resource_id = event.attributes.get("resource_id")
            destination = event.attributes.get("destination")

        return {
            "node_id": f"sys:{event.event_id}",
            "node_type": "system_operation",
            "operation_type": operation_type,
            "action_class": event.attributes.get("action_class"),
            "syscall_kind": event.attributes.get("syscall_kind"),
            "resource_id": resource_id,
            "resource_refs": self._resource_ref_values(event),
            "payload_refs": self._reference_values(event, "payload_refs", "payload_ref"),
            "destination": destination,
            "destination_trust": event.attributes.get(
                "destination_trust", event.attributes.get("trust_label")
            ),
            "byte_count": event.attributes.get("byte_count"),
            "timestamp": event.timestamp,
            "agent_id": event.agent_id,
            "session_id": event.session_id,
            "causal_context_id": event.causal_context_id,
            "confidence": event.attributes.get("confidence", "high"),
        }

    def _data_refs_for_event(self, event: NormalizedEvent) -> list[_DataRef]:
        ref_kinds: list[tuple[str, str]] = []
        if event.event_type in _SYSTEM_EVENT_TYPES:
            resource_kind = event.attributes.get("object_kind", "file")
            ref_kinds.extend(
                (reference, resource_kind)
                for reference in self._resource_ref_values(event)
            )
            ref_kinds.extend(
                (reference, "payload")
                for reference in self._reference_values(event, "payload_refs", "payload_ref")
            )

        ref_kinds.extend(
            (reference, "other_artifact") for reference in self._input_ref_values(event)
        )
        ref_kinds.extend(
            (reference, "other_artifact") for reference in self._produced_ref_values(event)
        )

        refs: list[_DataRef] = []
        seen: set[str] = set()
        for reference, default_kind in ref_kinds:
            data_ref = self._data_ref(event, reference, default_kind=default_kind)
            if data_ref.node_id not in seen:
                refs.append(data_ref)
                seen.add(data_ref.node_id)
        return refs

    def _data_ref(
        self,
        event: NormalizedEvent,
        reference: str,
        *,
        default_kind: str,
    ) -> _DataRef:
        metadata = self._data_metadata(event, reference)
        node_id = metadata.get("node_id")
        if node_id is None:
            node_id = reference if reference.startswith("data:") else f"data:resource:{reference}"
        return _DataRef(
            node_id=node_id,
            resource_id=metadata.get("resource_id", reference),
            object_kind=metadata.get("object_kind", default_kind),
            content_hash=metadata.get("content_hash"),
            version=metadata.get("version"),
            sensitivity=metadata.get("sensitivity", "unknown"),
            trust_label=metadata.get("trust_label", "unknown"),
            owner=metadata.get("owner"),
        )

    def _data_metadata(self, event: NormalizedEvent, reference: str) -> dict[str, Any]:
        resource_refs = self._resource_ref_values(event)
        inherit_event_metadata = not resource_refs or reference in resource_refs
        metadata = (
            {
                key: event.attributes[key]
                for key in (
                    "object_kind",
                    "content_hash",
                    "version",
                    "sensitivity",
                    "trust_label",
                    "owner",
                )
                if key in event.attributes
            }
            if inherit_event_metadata
            else {}
        )
        definitions = event.attributes.get("data_objects", [])
        if isinstance(definitions, dict):
            candidate = definitions.get(reference)
            if candidate is not None:
                if not isinstance(candidate, dict):
                    raise GraphBuilderError("data_objects mapping values must be dictionaries")
                metadata.update(candidate)
            return metadata
        if not isinstance(definitions, list):
            raise GraphBuilderError("data_objects must be a list or reference mapping")
        for candidate in definitions:
            if not isinstance(candidate, dict):
                raise GraphBuilderError("data_objects list values must be dictionaries")
            if reference in {candidate.get("resource_id"), candidate.get("node_id")}:
                metadata.update(candidate)
                break
        return metadata

    def _ensure_data_node(self, data_ref: _DataRef) -> str:
        if self.graph_store.has_node(data_ref.node_id):
            return data_ref.node_id

        self._add_node(
            {
                "node_id": data_ref.node_id,
                "node_type": "data_object",
                "resource_id": data_ref.resource_id,
                "object_kind": data_ref.object_kind,
                "content_hash": data_ref.content_hash,
                "version": data_ref.version,
                "sensitivity": data_ref.sensitivity,
                "trust_label": data_ref.trust_label,
                "owner": data_ref.owner,
            }
        )
        return data_ref.node_id

    def _reconcile_edges(self) -> None:
        for event in self._events_by_id.values():
            self._add_parent_edge(event)
            self._add_causal_context_edge(event)
            self._add_data_edges(event)

        self._add_authorization_edges()

    def _add_parent_edge(self, event: NormalizedEvent) -> None:
        if event.parent_event_id is None:
            return

        parent = self._events_by_id.get(event.parent_event_id)
        if parent is None:
            return

        source_id = self._primary_node_by_event_id.get(parent.event_id)
        target_id = self._primary_node_by_event_id.get(event.event_id)
        if source_id is None or target_id is None:
            return

        if (
            event.event_type is EventType.TOOL_CALL
            and parent.event_type is EventType.LLM_INVOCATION
        ):
            edge_type = EdgeType.INVOKES
        elif event.event_type in _SYSTEM_EVENT_TYPES and parent.event_type is EventType.TOOL_CALL:
            edge_type = EdgeType.TRIGGERS
        else:
            return

        self._add_edge(
            {
                "edge_id": f"edge:parent:{edge_type.value}:{parent.event_id}:{event.event_id}",
                "source_id": source_id,
                "target_id": target_id,
                "edge_type": edge_type.value,
                "timestamp": event.timestamp,
                "derivation": Derivation.PARENT_EVENT.value,
                "confidence": Confidence.HIGH.value,
                "evidence_ref": event.event_id,
            }
        )

    def _add_causal_context_edge(self, event: NormalizedEvent) -> None:
        if (
            not self.config.infer_causal_context_edges
            or event.parent_event_id is not None
            or event.causal_context_id is None
        ):
            return

        if event.event_type is EventType.TOOL_CALL:
            source_event_type = EventType.LLM_INVOCATION
            edge_type = EdgeType.INVOKES
        elif event.event_type in _SYSTEM_EVENT_TYPES:
            source_event_type = EventType.TOOL_CALL
            edge_type = EdgeType.TRIGGERS
        else:
            return

        source_event = self._latest_prior_event(
            event,
            event_type=source_event_type,
            causal_context_id=event.causal_context_id,
        )
        if source_event is None:
            return

        source_id = self._primary_node_by_event_id.get(source_event.event_id)
        target_id = self._primary_node_by_event_id.get(event.event_id)
        if source_id is None or target_id is None:
            return

        self._add_edge(
            {
                "edge_id": (
                    f"edge:context:{edge_type.value}:"
                    f"{source_event.event_id}:{event.event_id}"
                ),
                "source_id": source_id,
                "target_id": target_id,
                "edge_type": edge_type.value,
                "timestamp": event.timestamp,
                "derivation": Derivation.CAUSAL_CONTEXT.value,
                "confidence": Confidence.MEDIUM.value,
                "evidence_ref": event.event_id,
            }
        )

    def _latest_prior_event(
        self,
        target: NormalizedEvent,
        *,
        event_type: EventType,
        causal_context_id: str,
    ) -> NormalizedEvent | None:
        candidates = [
            event
            for event in self._events_by_id.values()
            if event.event_type is event_type
            and event.event_id != target.event_id
            and event.causal_context_id == causal_context_id
            and event.timestamp <= target.timestamp
            and event.event_id in self._primary_node_by_event_id
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda event: (event.timestamp, event.event_id))

    def _add_data_edges(self, event: NormalizedEvent) -> None:
        if event.event_id not in self._primary_node_by_event_id:
            return

        for reference in self._input_ref_values(event):
            self._add_data_edge(event, reference, EdgeType.INPUT_TO, data_is_source=True)

        for reference in self._produced_ref_values(event):
            self._add_data_edge(event, reference, EdgeType.PRODUCES, data_is_source=False)

        if event.event_type not in _SYSTEM_EVENT_TYPES:
            return

        operation_type = str(event.attributes.get("operation_type", event.event_type.value))
        relation = event.attributes.get("resource_relation") or event.attributes.get("access_mode")
        if relation is None:
            if operation_type in {"file_read", "network_receive"}:
                relation = "read"
            elif operation_type == "file_write":
                relation = "write"

        if relation == "read":
            for reference in self._resource_ref_values(event):
                self._add_data_edge(event, reference, EdgeType.READ, data_is_source=True)
        elif relation == "write":
            for reference in self._resource_ref_values(event):
                self._add_data_edge(event, reference, EdgeType.WRITE, data_is_source=False)

        for reference in self._reference_values(event, "payload_refs", "payload_ref"):
            self._add_data_edge(event, reference, EdgeType.PAYLOAD_OF, data_is_source=True)

    def _add_data_edge(
        self,
        event: NormalizedEvent,
        reference: str,
        edge_type: EdgeType,
        *,
        data_is_source: bool,
    ) -> None:
        event_node_id = self._primary_node_by_event_id[event.event_id]
        data_node_id = self._data_ref(event, reference, default_kind="other_artifact").node_id
        if not self.graph_store.has_node(data_node_id):
            return
        source_id, target_id = (
            (data_node_id, event_node_id) if data_is_source else (event_node_id, data_node_id)
        )
        self._add_edge(
            {
                "edge_id": f"edge:data:{edge_type.value}:{event.event_id}:{data_node_id}",
                "source_id": source_id,
                "target_id": target_id,
                "edge_type": edge_type.value,
                "timestamp": event.timestamp,
                "derivation": Derivation.EXPLICIT_DATA_REFERENCE.value,
                "confidence": event.attributes.get("data_confidence", Confidence.HIGH.value),
                "evidence_ref": event.event_id,
            }
        )

    def _add_authorization_edges(self) -> None:
        approval_events = [
            event
            for event in self._events_by_id.values()
            if event.event_type is EventType.HUMAN_APPROVAL
        ]
        tool_events = [
            event
            for event in self._events_by_id.values()
            if event.event_type is EventType.TOOL_CALL
        ]
        operation_events = [
            event
            for event in self._events_by_id.values()
            if event.event_type in _SYSTEM_EVENT_TYPES
        ]

        for approval_event in approval_events:
            approval_id = self._primary_node_by_event_id.get(approval_event.event_id)
            if approval_id is None:
                continue
            approval_node = self.graph_store.get_node(approval_id)
            if not isinstance(approval_node, HumanApprovalNode):
                continue

            for tool_event in tool_events:
                tool_id = self._primary_node_by_event_id.get(tool_event.event_id)
                if tool_id is None:
                    continue
                tool_node = self.graph_store.get_node(tool_id)
                if not isinstance(tool_node, ToolCallNode):
                    continue
                if not self._approval_matches_tool(approval_node, tool_node):
                    continue

                self._add_edge(
                    {
                        "edge_id": f"edge:auth:{approval_event.event_id}:{tool_event.event_id}",
                        "source_id": approval_id,
                        "target_id": tool_id,
                        "edge_type": EdgeType.AUTHORIZES.value,
                        "timestamp": approval_event.timestamp,
                        "derivation": Derivation.EXPLICIT_APPROVAL_SCOPE.value,
                        "confidence": Confidence.HIGH.value,
                        "evidence_ref": approval_event.event_id,
                    }
                )

            for operation_event in operation_events:
                operation_id = self._primary_node_by_event_id.get(operation_event.event_id)
                if operation_id is None:
                    continue
                operation_node = self.graph_store.get_node(operation_id)
                if not isinstance(operation_node, SystemOperationNode):
                    continue
                if not self._approval_matches_operation(
                    approval_node,
                    operation_node,
                    operation_event,
                ):
                    continue

                self._add_edge(
                    {
                        "edge_id": (
                            f"edge:auth:{approval_event.event_id}:{operation_event.event_id}"
                        ),
                        "source_id": approval_id,
                        "target_id": operation_id,
                        "edge_type": EdgeType.AUTHORIZES.value,
                        "timestamp": approval_event.timestamp,
                        "derivation": Derivation.EXPLICIT_APPROVAL_SCOPE.value,
                        "confidence": Confidence.HIGH.value,
                        "evidence_ref": approval_event.event_id,
                    }
                )

    def _approval_matches_tool(
        self,
        approval: HumanApprovalNode,
        tool: ToolCallNode,
    ) -> bool:
        if tool.action_class != approval.action_class:
            return False
        if approval.resource_scope is not None and approval.resource_scope != tool.target_resource:
            return False
        if (
            approval.destination_scope is not None
            and approval.destination_scope != tool.destination
        ):
            return False
        if approval.session_id is not None and approval.session_id != tool.session_id:
            return False
        if (
            approval.causal_context_id is not None
            and approval.causal_context_id != tool.causal_context_id
        ):
            return False
        return True

    def _approval_matches_operation(
        self,
        approval: HumanApprovalNode,
        operation: SystemOperationNode,
        operation_event: NormalizedEvent,
    ) -> bool:
        action_class = operation.action_class or operation.operation_type
        resource_refs = list(
            dict.fromkeys([*operation.resource_refs, *operation.payload_refs])
        )
        destination = operation.destination

        parent = (
            self._events_by_id.get(operation_event.parent_event_id)
            if operation_event.parent_event_id is not None
            else None
        )
        if parent is not None and parent.event_type is EventType.TOOL_CALL:
            parent_id = self._primary_node_by_event_id.get(parent.event_id)
            if parent_id is not None:
                parent_node = self.graph_store.get_node(parent_id)
                if isinstance(parent_node, ToolCallNode):
                    action_class = (
                        operation.action_class
                        or parent_node.action_class
                        or action_class
                    )
                    if not resource_refs and parent_node.target_resource is not None:
                        resource_refs = [parent_node.target_resource]
                    destination = destination or parent_node.destination

        if action_class != approval.action_class:
            return False
        if approval.resource_scope is not None and approval.resource_scope not in resource_refs:
            return False
        if approval.destination_scope is not None and approval.destination_scope != destination:
            return False
        if approval.session_id is not None and approval.session_id != operation.session_id:
            return False
        if (
            approval.causal_context_id is not None
            and approval.causal_context_id != operation.causal_context_id
        ):
            return False
        return True

    def _add_node(self, node: dict[str, Any]) -> None:
        try:
            self.graph_store.add_node(node)
        except GraphStoreError as exc:
            raise GraphBuilderError(str(exc)) from exc

    def _add_edge(self, edge: dict[str, Any]) -> None:
        if self.graph_store.has_edge(str(edge["edge_id"])):
            return

        try:
            self.graph_store.add_edge(edge)
        except GraphStoreError as exc:
            raise GraphBuilderError(str(exc)) from exc

    def _resource_ref_values(self, event: NormalizedEvent) -> list[str]:
        references = self._reference_values(event, "resource_refs", "resource_id")
        if event.event_type is EventType.DEVICE_COMMAND and not references:
            device_id = event.attributes.get("device_id")
            if device_id is not None:
                references.append(self._validated_reference(device_id, "device_id"))
        return references

    def _input_ref_values(self, event: NormalizedEvent) -> list[str]:
        return self._reference_values(event, "input_refs", "input_ref")

    def _produced_ref_values(self, event: NormalizedEvent) -> list[str]:
        references: list[str] = []
        for plural_key, singular_key in (
            ("produced_refs", "produced_ref"),
            ("result_refs", "result_ref"),
        ):
            references.extend(self._reference_values(event, plural_key, singular_key))
        if event.event_type is EventType.LLM_INVOCATION:
            output_ref = event.attributes.get("output_ref")
            if output_ref is not None:
                references.append(self._validated_reference(output_ref, "output_ref"))
        return list(dict.fromkeys(references))

    def _reference_values(
        self,
        event: NormalizedEvent,
        plural_key: str,
        singular_key: str,
    ) -> list[str]:
        values: list[str] = []
        plural = event.attributes.get(plural_key, [])
        if not isinstance(plural, list):
            raise GraphBuilderError(
                f"event {event.event_id} attribute {plural_key} must be a list"
            )
        values.extend(
            self._validated_reference(value, plural_key)
            for value in plural
        )
        singular = event.attributes.get(singular_key)
        if singular is not None:
            values.append(self._validated_reference(singular, singular_key))
        return list(dict.fromkeys(values))

    def _validated_reference(self, value: Any, key: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise GraphBuilderError(f"attribute {key} references must be non-empty strings")
        return value

    def _required_attribute(self, event: NormalizedEvent, key: str) -> Any:
        if key not in event.attributes or event.attributes[key] is None:
            raise GraphBuilderError(
                f"event {event.event_id} missing required attribute: {key}"
            )
        return event.attributes[key]

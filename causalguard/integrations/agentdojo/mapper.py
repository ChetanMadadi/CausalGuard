"""Map AgentDojo transcripts and runtime evidence to normalized events."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

try:
    from agentdojo.functions_runtime import FunctionCall
    from agentdojo.types import ChatMessage
except ImportError as exc:  # pragma: no cover - optional dependency path
    raise ImportError(
        "The AgentDojo integration requires causalguard[agentdojo]."
    ) from exc

from causalguard.integrations.agentdojo.common import stable_hash
from causalguard.integrations.agentdojo.extractors.base import (
    DomainObjectEvidence,
    DomainOperationEvidence,
    ProposedActionEvidence,
)
from causalguard.integrations.agentdojo.runtime import (
    ProposedCallObservation,
    RuntimeCallObservation,
)
from causalguard.schema.events import NormalizedEvent


@dataclass(frozen=True)
class _PendingToolCall:
    event_id: str
    call: FunctionCall


@dataclass(frozen=True)
class _ToolResultRef:
    reference: str
    node_id: str
    content_hash: str
    object_kind: str = "tool_result"

    def metadata(self) -> dict[str, str]:
        return {
            "node_id": self.node_id,
            "resource_id": self.reference,
            "object_kind": self.object_kind,
            "content_hash": self.content_hash,
            "sensitivity": "unknown",
            "trust_label": "unknown",
        }


class AgentDojoTraceMapper:
    """Convert a completed transcript plus optional runtime observations."""

    def __init__(
        self,
        *,
        session_id: str,
        model_name: str | None = None,
        agent_id: str = "agentdojo-agent",
        causal_context_id: str | None = None,
    ) -> None:
        if not session_id.strip():
            raise ValueError("session_id must not be empty")
        self.session_id = session_id
        self.model_name = model_name
        self.agent_id = agent_id
        self.causal_context_id = causal_context_id or session_id
        self._timestamp = 0

    def map_trace(
        self,
        query: str,
        messages: Sequence[ChatMessage],
        runtime_observations: Sequence[RuntimeCallObservation] = (),
        proposed_observations: Sequence[ProposedCallObservation] = (),
    ) -> list[NormalizedEvent]:
        """Return normalized events without retaining raw message content."""

        self._timestamp = 0
        events: list[NormalizedEvent] = []
        pending_calls: list[_PendingToolCall] = []
        pending_observations = list(runtime_observations)
        pending_proposals = list(proposed_observations)
        observed_result_inputs: list[_ToolResultRef] = []

        user_event_id = self._event_id("user_input")
        events.append(
            self._event(
                event_id=user_event_id,
                event_type="user_input",
                parent_event_id=None,
                attributes={
                    "input_hash": stable_hash(query),
                    "timestamp_source": "agentdojo_transcript_order",
                },
            )
        )

        first_llm = True
        for message_index, message in enumerate(messages):
            role = message["role"]
            if role == "assistant":
                llm_event_id = self._event_id("llm", message_index)
                input_refs = [result.reference for result in observed_result_inputs]
                data_objects = {
                    result.reference: result.metadata()
                    for result in observed_result_inputs
                }
                attributes: dict[str, Any] = {
                    "model_name": self.model_name,
                    "prompt_hash": stable_hash(messages[:message_index]),
                    "output_hash": stable_hash(message),
                    "timestamp_source": "agentdojo_transcript_order",
                }
                if input_refs:
                    attributes["input_refs"] = input_refs
                    attributes["data_objects"] = data_objects

                events.append(
                    self._event(
                        event_id=llm_event_id,
                        event_type="llm_invocation",
                        parent_event_id=user_event_id if first_llm else None,
                        attributes=attributes,
                    )
                )
                first_llm = False

                for call_index, tool_call in enumerate(message.get("tool_calls") or []):
                    tool_event_id = self._event_id(
                        "tool",
                        message_index,
                        call_index,
                    )
                    tool_attributes: dict[str, Any] = {
                        "tool_name": tool_call.function,
                        "action_class": tool_call.function,
                        "argument_summary": _argument_summary(tool_call.args),
                        "timestamp_source": "agentdojo_transcript_order",
                    }
                    if tool_call.id is not None:
                        tool_attributes["source_tool_call_id"] = tool_call.id
                    events.append(
                        self._event(
                            event_id=tool_event_id,
                            event_type="tool_call",
                            parent_event_id=llm_event_id,
                            attributes=tool_attributes,
                        )
                    )
                    pending_calls.append(
                        _PendingToolCall(event_id=tool_event_id, call=tool_call)
                    )
                    proposal = _pop_matching_proposal(
                        pending_proposals,
                        tool_call,
                    )
                    if proposal is not None:
                        _attach_proposed_tool_context(
                            events,
                            tool_event_id,
                            proposal.evidence,
                        )

            elif role == "tool":
                pending = _pop_matching_call(pending_calls, message["tool_call"])
                result = self._result_ref(
                    message_index,
                    message["content"],
                    message.get("error"),
                )
                observed_result_inputs.append(result)
                if pending is None:
                    continue
                observation = _pop_matching_observation(
                    pending_observations,
                    pending.call,
                )

                # Errors remain exact feedback artifacts, but are not treated
                # as successful domain operations or writes.
                if message.get("error") is not None:
                    _attach_result_to_tool_event(events, pending.event_id, result)
                    continue

                if observation is not None and observation.operations:
                    _attach_domain_tool_context(
                        events,
                        pending.event_id,
                        observation.operations,
                    )
                    for operation_index, operation in enumerate(
                        observation.operations
                    ):
                        is_result_producer = (
                            operation_index == len(observation.operations) - 1
                        )
                        events.append(
                            self._event(
                                event_id=self._event_id(
                                    "runtime_operation",
                                    message_index,
                                    operation_index,
                                ),
                                event_type=operation.event_type.value,
                                parent_event_id=pending.event_id,
                                attributes=_operation_attributes(
                                    operation,
                                    observation=observation,
                                    result=(result if is_result_producer else None),
                                    source_tool_call_id=message.get("tool_call_id"),
                                ),
                            )
                        )
                    continue

                operation_attributes: dict[str, Any] = {
                    "operation_type": "agentdojo_function_execution",
                    "action_class": message["tool_call"].function,
                    "result_ref": result.reference,
                    "data_objects": {result.reference: result.metadata()},
                    "timestamp_source": "agentdojo_transcript_order",
                }
                if message.get("tool_call_id") is not None:
                    operation_attributes["source_tool_call_id"] = message[
                        "tool_call_id"
                    ]
                events.append(
                    self._event(
                        event_id=self._event_id("system_operation", message_index),
                        event_type="system_operation",
                        parent_event_id=pending.event_id,
                        attributes=operation_attributes,
                    )
                )

        return events

    def _result_ref(
        self,
        message_index: int,
        result_content: object,
        error: str | None,
    ) -> _ToolResultRef:
        reference = self._event_id("tool_result", message_index)
        return _ToolResultRef(
            reference=reference,
            node_id=f"data:{reference}",
            content_hash=stable_hash(
                {"result_value": result_content, "error_text": error}
            ),
            object_kind="tool_error" if error is not None else "tool_result",
        )

    def _event(
        self,
        *,
        event_id: str,
        event_type: str,
        parent_event_id: str | None,
        attributes: dict[str, Any],
    ) -> NormalizedEvent:
        self._timestamp += 1
        return NormalizedEvent.model_validate(
            {
                "event_id": event_id,
                "timestamp": float(self._timestamp),
                "event_type": event_type,
                "agent_id": self.agent_id,
                "session_id": self.session_id,
                "causal_context_id": self.causal_context_id,
                "parent_event_id": parent_event_id,
                "attributes": attributes,
            }
        )

    def _event_id(self, kind: str, *indices: int) -> str:
        suffix = ":".join(str(index) for index in indices)
        if suffix:
            return f"agentdojo:{self.session_id}:{kind}:{suffix}"
        return f"agentdojo:{self.session_id}:{kind}"

    def tool_node_id(self, *, message_index: int, call_index: int) -> str:
        """Return the graph node ID used for a transcript ToolCall."""

        return f"tool:{self._event_id('tool', message_index, call_index)}"


def _pop_matching_call(
    pending_calls: list[_PendingToolCall],
    result_call: FunctionCall,
) -> _PendingToolCall | None:
    for index, pending in enumerate(pending_calls):
        if result_call.id is not None and pending.call.id == result_call.id:
            return pending_calls.pop(index)
        if (
            result_call.id is None
            and pending.call.model_dump() == result_call.model_dump()
        ):
            return pending_calls.pop(index)
    return None


def _pop_matching_observation(
    observations: list[RuntimeCallObservation],
    call: FunctionCall,
) -> RuntimeCallObservation | None:
    argument_hash = stable_hash(call.args)
    for index, observation in enumerate(observations):
        if (
            observation.function_name == call.function
            and observation.argument_hash == argument_hash
        ):
            return observations.pop(index)
    return None


def _pop_matching_proposal(
    proposals: list[ProposedCallObservation],
    call: FunctionCall,
) -> ProposedCallObservation | None:
    argument_hash = stable_hash(call.args)
    for index, proposal in enumerate(proposals):
        if (
            proposal.source_tool_call_id is not None
            and proposal.source_tool_call_id == call.id
        ):
            return proposals.pop(index)
        if (
            proposal.source_tool_call_id is None
            and proposal.function_name == call.function
            and proposal.argument_hash == argument_hash
        ):
            return proposals.pop(index)
    return None


def _operation_attributes(
    operation: DomainOperationEvidence,
    *,
    observation: RuntimeCallObservation,
    result: _ToolResultRef | None,
    source_tool_call_id: str | None,
) -> dict[str, Any]:
    objects = {item.reference: item.metadata() for item in operation.all_objects()}
    attributes: dict[str, Any] = {
        "operation_type": operation.operation_type,
        "action_class": operation.action_class,
        "read_refs": [item.reference for item in operation.read_objects],
        "write_refs": [item.reference for item in operation.write_objects],
        "payload_refs": [item.reference for item in operation.payload_objects],
        "data_objects": objects,
        "data_derivation": "runtime_observation",
        "data_confidence": "high",
        "runtime_observation_ordinal": observation.ordinal,
        "timestamp_source": "agentdojo_transcript_order",
    }
    if result is not None:
        attributes["result_ref"] = result.reference
        attributes["data_objects"][result.reference] = result.metadata()
    if operation.destination is not None:
        attributes["destination"] = operation.destination
    if operation.target_resource is not None:
        attributes["resource_id"] = operation.target_resource
    if operation.byte_count is not None:
        attributes["byte_count"] = operation.byte_count
    if source_tool_call_id is not None:
        attributes["source_tool_call_id"] = source_tool_call_id
    return attributes


def _attach_domain_tool_context(
    events: list[NormalizedEvent],
    tool_event_id: str,
    operations: Sequence[DomainOperationEvidence],
) -> None:
    tool_inputs = _unique_objects(
        item
        for operation in operations
        for item in operation.tool_input_objects
    )
    generated = _unique_objects(
        item
        for operation in operations
        for item in operation.llm_generated_objects
    )
    destination = next(
        (
            operation.destination
            for operation in operations
            if operation.destination is not None
        ),
        None,
    )
    target_resource = next(
        (
            operation.target_resource
            for operation in operations
            if operation.target_resource is not None
        ),
        None,
    )

    _attach_tool_context(
        events,
        tool_event_id,
        tool_inputs=tool_inputs,
        generated=generated,
        destination=destination,
        target_resource=target_resource,
    )


def _attach_proposed_tool_context(
    events: list[NormalizedEvent],
    tool_event_id: str,
    proposals: Sequence[ProposedActionEvidence],
) -> None:
    tool_inputs = _unique_objects(
        item for proposal in proposals for item in proposal.tool_input_objects
    )
    generated = _unique_objects(
        item for proposal in proposals for item in proposal.llm_generated_objects
    )
    destination = next(
        (proposal.destination for proposal in proposals if proposal.destination),
        None,
    )
    target_resource = next(
        (
            proposal.target_resource
            for proposal in proposals
            if proposal.target_resource
        ),
        None,
    )
    _attach_tool_context(
        events,
        tool_event_id,
        tool_inputs=tool_inputs,
        generated=generated,
        destination=destination,
        target_resource=target_resource,
    )


def _attach_tool_context(
    events: list[NormalizedEvent],
    tool_event_id: str,
    *,
    tool_inputs: Sequence[DomainObjectEvidence],
    generated: Sequence[DomainObjectEvidence],
    destination: str | None,
    target_resource: str | None,
) -> None:
    tool_event = _event_by_id(events, tool_event_id)
    tool_updates: dict[str, Any] = {}
    if tool_inputs:
        tool_updates["input_refs"] = [item.reference for item in tool_inputs]
        tool_updates["data_objects"] = {
            item.reference: item.metadata() for item in tool_inputs
        }
    if destination is not None:
        tool_updates["destination"] = destination
    if (
        target_resource is not None
        and tool_event.attributes.get("target_resource") is None
    ):
        tool_updates["target_resource"] = target_resource
    _merge_event_attributes(events, tool_event_id, tool_updates)

    if generated and tool_event.parent_event_id is not None:
        _merge_event_attributes(
            events,
            tool_event.parent_event_id,
            {
                "produced_refs": [item.reference for item in generated],
                "data_objects": {
                    item.reference: item.metadata() for item in generated
                },
            },
        )


def _unique_objects(
    objects: Iterable[DomainObjectEvidence],
) -> tuple[DomainObjectEvidence, ...]:
    unique: dict[str, DomainObjectEvidence] = {}
    for item in objects:
        unique[item.reference] = item
    return tuple(unique.values())


def _event_by_id(
    events: Sequence[NormalizedEvent],
    event_id: str,
) -> NormalizedEvent:
    for event in events:
        if event.event_id == event_id:
            return event
    raise ValueError(f"missing event: {event_id}")


def _merge_event_attributes(
    events: list[NormalizedEvent],
    event_id: str,
    updates: Mapping[str, Any],
) -> None:
    for index, event in enumerate(events):
        if event.event_id != event_id:
            continue
        attributes = dict(event.attributes)
        for key, value in updates.items():
            if key == "data_objects":
                definitions = dict(attributes.get(key, {}))
                definitions.update(value)
                attributes[key] = definitions
            elif key.endswith("_refs"):
                references = [*attributes.get(key, []), *value]
                attributes[key] = list(dict.fromkeys(references))
            else:
                attributes[key] = value
        payload = event.model_dump(mode="python")
        payload["attributes"] = attributes
        events[index] = NormalizedEvent.model_validate(payload)
        return
    raise ValueError(f"missing event for enrichment: {event_id}")


def _attach_result_to_tool_event(
    events: list[NormalizedEvent],
    tool_event_id: str,
    result: _ToolResultRef,
) -> None:
    _merge_event_attributes(
        events,
        tool_event_id,
        {
            "result_ref": result.reference,
            "data_objects": {result.reference: result.metadata()},
        },
    )


def _argument_summary(arguments: Mapping[str, object]) -> dict[str, object]:
    ordered_names = sorted(arguments)
    return {
        "argument_count": len(ordered_names),
        "argument_names": ordered_names,
        "argument_type_names": [
            type(arguments[name]).__name__ for name in ordered_names
        ],
    }

"""Observation-only AgentDojo benchmark audit helpers.

This module intentionally does not enforce policy or infer semantic information
flow.  It records privacy-safe execution facts, compares real state mutation to
the graph that the current instrumentation actually emitted, and reports gaps.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from agentdojo.functions_runtime import TaskEnvironment
from agentdojo.types import ChatMessage

from causalguard.graph import GraphStore
from causalguard.integrations.agentdojo.common import stable_hash
from causalguard.integrations.agentdojo.runtime import RuntimeCallObservation
from causalguard.schema.edges import EdgeType


READ_FUNCTIONS = frozenset(
    {
        "get_unread_emails",
        "get_sent_emails",
        "get_received_emails",
        "get_draft_emails",
        "search_emails",
        "search_contacts_by_name",
        "search_contacts_by_email",
        "get_current_day",
        "search_calendar_events",
        "get_day_calendar_events",
        "search_files_by_filename",
        "get_file_by_id",
        "list_files",
        "search_files",
        "get_user_information",
        "get_all_hotels_in_city",
        "get_hotels_prices",
        "get_rating_reviews_for_hotels",
        "get_hotels_address",
        "get_all_restaurants_in_city",
        "get_cuisine_type_for_restaurants",
        "get_restaurants_address",
        "get_rating_reviews_for_restaurants",
        "get_dietary_restrictions_for_all_restaurants",
        "get_contact_information_for_restaurants",
        "get_price_for_restaurants",
        "check_restaurant_opening_hours",
        "get_all_car_rental_companies_in_city",
        "get_car_types_available",
        "get_rating_reviews_for_car_rental",
        "get_car_fuel_options",
        "get_car_rental_address",
        "get_car_price_per_day",
        "get_flight_information",
        "get_iban",
        "get_balance",
        "get_most_recent_transactions",
        "get_scheduled_transactions",
        "read_file",
        "get_user_info",
        "get_channels",
        "read_channel_messages",
        "read_inbox",
        "get_users_in_channel",
        "get_webpage",
    }
)

OUTGOING_FUNCTIONS = frozenset(
    {
        "send_email",
        "send_direct_message",
        "send_channel_message",
        "invite_user_to_slack",
        "send_money",
        "schedule_transaction",
        "post_webpage",
    }
)

MUTATION_CATEGORIES: dict[str, str] = {
    "send_email": "email send",
    "delete_email": "email mutation",
    "create_calendar_event": "calendar mutation",
    "cancel_calendar_event": "cancellation/modification",
    "reschedule_calendar_event": "cancellation/modification",
    "add_calendar_event_participants": "calendar mutation",
    "append_to_file": "file/cloud-drive mutation",
    "create_file": "file/cloud-drive mutation",
    "delete_file": "file/cloud-drive mutation",
    "share_file": "file/cloud-drive mutation",
    "reserve_hotel": "travel reservation",
    "reserve_restaurant": "travel reservation",
    "reserve_car_rental": "travel reservation",
    "send_money": "banking transfer",
    "schedule_transaction": "bill/scheduled-payment mutation",
    "update_scheduled_transaction": "bill/scheduled-payment mutation",
    "update_password": "account/security-setting mutation",
    "update_user_info": "account/security-setting mutation",
    "add_user_to_channel": "Slack invitation/membership change",
    "send_direct_message": "Slack DM",
    "send_channel_message": "Slack channel message",
    "invite_user_to_slack": "Slack invitation/membership change",
    "remove_user_from_slack": "Slack invitation/membership change",
    "post_webpage": "other consequential external action",
}

EXPECTED_OPERATION_TYPES: dict[str, str] = {
    "search_files_by_filename": "file_read",
    "send_email": "email_send",
}

_STATIC_PATH_PARTS = frozenset(
    {
        "inbox",
        "calendar",
        "cloud_drive",
        "hotels",
        "restaurants",
        "car_rental",
        "flights",
        "user",
        "reservation",
        "bank_account",
        "filesystem",
        "user_account",
        "slack",
        "web",
        "emails",
        "sent",
        "received",
        "drafts",
        "events",
        "files",
        "transactions",
        "scheduled_transactions",
        "balance",
        "password",
        "first_name",
        "last_name",
        "street",
        "city",
        "country",
        "channels",
        "users",
        "user_channels",
        "user_inbox",
        "channel_inbox",
        "pages",
        "contact_information",
        "reservation_type",
        "title",
        "start_time",
        "end_time",
    }
)


@dataclass(frozen=True)
class ExecutedCall:
    ordinal: int
    attempt: int
    function_name: str
    argument_hash: str
    argument_summary: dict[str, object]
    result_hash: str
    result_type: str
    error_hash: str | None
    runtime_observed: bool
    state_changed: bool | None
    effect_categories: tuple[str, ...]
    destinations: tuple[str, ...]
    payload_object_ids: tuple[str, ...]
    payload_object_types: tuple[str, ...]

    @property
    def succeeded(self) -> bool:
        return self.error_hash is None

    def to_dict(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "attempt": self.attempt,
            "function_name": self.function_name,
            "argument_hash": self.argument_hash,
            "argument_summary": self.argument_summary,
            "result_hash": self.result_hash,
            "result_type": self.result_type,
            "error_hash": self.error_hash,
            "runtime_observed": self.runtime_observed,
            "state_changed": self.state_changed,
            "effect_categories": list(self.effect_categories),
            "destinations": list(self.destinations),
            "payload_object_ids": list(self.payload_object_ids),
            "payload_object_types": list(self.payload_object_types),
        }


def task_inventory(benchmark_version: str) -> list[dict[str, object]]:
    """Enumerate normal user tasks from AgentDojo's registered suites."""

    from agentdojo.task_suite.load_suites import get_suites

    inventory: list[dict[str, object]] = []
    for suite_name, suite in get_suites(benchmark_version).items():
        for task_id, task in suite.user_tasks.items():
            environment = suite.load_and_inject_default_environment({})
            task_environment = task.init_environment(environment)
            ground_truth = task.ground_truth(task_environment.model_copy(deep=True))
            inventory.append(
                {
                    "suite": suite_name,
                    "task_id": task_id,
                    "normalized_description": _normalized_task_description(
                        suite_name,
                        task_id,
                        [call.function for call in ground_truth],
                    ),
                    "prompt_hash": stable_hash(task.PROMPT),
                    "ground_truth_tool_sequence": [
                        call.function for call in ground_truth
                    ],
                }
            )
    return inventory


def _normalized_task_description(
    suite: str,
    task_id: str,
    ground_truth_tools: Sequence[str],
) -> str:
    sequence = " -> ".join(ground_truth_tools) if ground_truth_tools else "no tools"
    return f"{suite}/{task_id}: expected workflow {sequence}"


def state_view(environment: TaskEnvironment) -> dict[str, object]:
    """Return hashes and structural counts, never raw environment values."""

    dumped = environment.model_dump(mode="json")
    components: dict[str, object] = {}
    for name, value in dumped.items():
        components[name] = {
            "state_hash": stable_hash(value),
            "collection_counts": _collection_counts(value),
        }
    return {
        "environment_type": type(environment).__name__,
        "state_hash": stable_hash(dumped),
        "components": components,
    }


def state_diff(
    before: TaskEnvironment,
    after: TaskEnvironment,
    *,
    suite: str,
) -> list[dict[str, object]]:
    """Return privacy-safe leaf changes between two AgentDojo environments."""

    changes: list[dict[str, object]] = []
    _diff_values(
        before.model_dump(mode="json"),
        after.model_dump(mode="json"),
        path=(),
        suite=suite,
        output=changes,
    )
    return changes


def _diff_values(
    before: object,
    after: object,
    *,
    path: tuple[str, ...],
    suite: str,
    output: list[dict[str, object]],
) -> None:
    if before == after:
        return
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        for key in sorted(set(before) | set(after), key=str):
            segment = _safe_path_segment(str(key))
            if key not in before:
                _record_change(None, after[key], (*path, segment), suite, output, "added")
            elif key not in after:
                _record_change(before[key], None, (*path, segment), suite, output, "removed")
            else:
                _diff_values(
                    before[key], after[key], path=(*path, segment), suite=suite, output=output
                )
        return
    if isinstance(before, list) and isinstance(after, list):
        common = min(len(before), len(after))
        for index in range(common):
            _diff_values(
                before[index], after[index], path=(*path, f"index:{index}"), suite=suite, output=output
            )
        for index in range(common, len(before)):
            _record_change(before[index], None, (*path, f"index:{index}"), suite, output, "removed")
        for index in range(common, len(after)):
            _record_change(None, after[index], (*path, f"index:{index}"), suite, output, "added")
        return
    _record_change(before, after, path, suite, output, "modified")


def _record_change(
    before: object,
    after: object,
    path: tuple[str, ...],
    suite: str,
    output: list[dict[str, object]],
    change_type: str,
) -> None:
    path_text = ".".join(path) or "root"
    output.append(
        {
            "change_type": change_type,
            "path": path_text,
            "resource_id": f"agentdojo:{suite}:state:{stable_hash(path_text).removeprefix('sha256:')[:24]}",
            "before_hash": stable_hash(before) if before is not None else None,
            "after_hash": stable_hash(after) if after is not None else None,
            "before_type": type(before).__name__ if before is not None else None,
            "after_type": type(after).__name__ if after is not None else None,
        }
    )


def _safe_path_segment(value: str) -> str:
    if value in _STATIC_PATH_PARTS:
        return value
    return f"key:{stable_hash(value).removeprefix('sha256:')[:16]}"


def _collection_counts(value: object, *, depth: int = 0) -> dict[str, int]:
    if depth > 2:
        return {}
    counts: dict[str, int] = {}
    if isinstance(value, Mapping):
        counts["mapping_entries"] = len(value)
        for key, nested in value.items():
            if str(key) in _STATIC_PATH_PARTS:
                for nested_key, count in _collection_counts(nested, depth=depth + 1).items():
                    counts[f"{key}.{nested_key}"] = count
    elif isinstance(value, list):
        counts["list_items"] = len(value)
    return counts


def collect_executed_calls(
    attempt_messages: Sequence[Sequence[ChatMessage]],
    attempt_observations: Sequence[Sequence[RuntimeCallObservation]],
) -> list[ExecutedCall]:
    """Build a privacy-safe ordered record of tool results across retries."""

    calls: list[ExecutedCall] = []
    ordinal = 0
    for attempt, messages in enumerate(attempt_messages):
        observations = list(
            attempt_observations[attempt]
            if attempt < len(attempt_observations)
            else ()
        )
        for message in messages:
            if message["role"] != "tool":
                continue
            call = message["tool_call"]
            argument_hash = stable_hash(call.args)
            observation = _pop_observation(observations, call.function, argument_hash)
            error = message.get("error")
            succeeded = error is None
            effects = _effect_categories(call.function, call.args, succeeded=succeeded)
            destinations = _destinations(call.function, call.args) if succeeded else ()
            payload_ids, payload_types = (
                _payloads(call.function, call.args) if succeeded else ((), ())
            )
            calls.append(
                ExecutedCall(
                    ordinal=ordinal,
                    attempt=attempt,
                    function_name=call.function,
                    argument_hash=argument_hash,
                    argument_summary=_argument_summary(call.args),
                    result_hash=stable_hash(message.get("content")),
                    result_type=_result_type(message.get("content")),
                    error_hash=stable_hash(error) if error is not None else None,
                    runtime_observed=observation is not None,
                    state_changed=(observation.state_changed if observation else None),
                    effect_categories=effects,
                    destinations=destinations,
                    payload_object_ids=payload_ids,
                    payload_object_types=payload_types,
                )
            )
            ordinal += 1
    return calls


def _pop_observation(
    observations: list[RuntimeCallObservation],
    function_name: str,
    argument_hash: str,
) -> RuntimeCallObservation | None:
    for index, item in enumerate(observations):
        if item.function_name == function_name and item.argument_hash == argument_hash:
            return observations.pop(index)
    return None


def _argument_summary(arguments: Mapping[str, object]) -> dict[str, object]:
    names = sorted(arguments)
    return {
        "argument_count": len(names),
        "argument_names": names,
        "argument_type_names": [type(arguments[name]).__name__ for name in names],
    }


def _result_type(content: object) -> str:
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes, bytearray)):
        return "message_content_blocks"
    return type(content).__name__


def _effect_categories(
    function_name: str,
    arguments: Mapping[str, object],
    *,
    succeeded: bool,
) -> tuple[str, ...]:
    if not succeeded:
        return ()
    categories: list[str] = []
    if function_name in READ_FUNCTIONS:
        categories.append("read-only access")
    if function_name in MUTATION_CATEGORIES:
        categories.append(MUTATION_CATEGORIES[function_name])
    if function_name == "send_email" and arguments.get("attachments"):
        categories.append("email with attachment")
    return tuple(dict.fromkeys(categories))


def _destinations(
    function_name: str,
    arguments: Mapping[str, object],
) -> tuple[str, ...]:
    if function_name == "send_email":
        recipients: list[str] = []
        for field in ("recipients", "cc", "bcc"):
            values = arguments.get(field) or []
            if isinstance(values, Sequence) and not isinstance(values, (str, bytes, bytearray)):
                recipients.extend(f"mailto:{str(value).lower()}" for value in values)
        return (",".join(recipients),) if recipients else ()
    if function_name == "send_direct_message":
        return _literal_destination("slack:user", arguments.get("recipient"))
    if function_name == "send_channel_message":
        return _literal_destination("slack:channel", arguments.get("channel"))
    if function_name in {"invite_user_to_slack", "add_user_to_channel"}:
        values = []
        if arguments.get("user") is not None:
            values.extend(_literal_destination("slack:user", arguments["user"]))
        if arguments.get("channel") is not None:
            values.extend(_literal_destination("slack:channel", arguments["channel"]))
        if arguments.get("user_email") is not None:
            values.append(f"mailto:{str(arguments['user_email']).lower()}")
        return tuple(values)
    if function_name in {"send_money", "schedule_transaction"}:
        value = arguments.get("recipient")
        if value is None:
            return ()
        return (f"bank:iban:{stable_hash(str(value)).removeprefix('sha256:')[:20]}",)
    if function_name.startswith("reserve_"):
        key = {"reserve_hotel": "hotel", "reserve_restaurant": "restaurant", "reserve_car_rental": "company"}.get(function_name)
        value = arguments.get(key) if key else None
        return _hashed_destination(f"travel:{function_name.removeprefix('reserve_')}", value)
    if function_name == "post_webpage":
        return _hashed_destination("web:url", arguments.get("url"))
    return ()


def _literal_destination(prefix: str, value: object) -> tuple[str, ...]:
    return (f"{prefix}:{value}",) if value is not None else ()


def _hashed_destination(prefix: str, value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    return (f"{prefix}:{stable_hash(value).removeprefix('sha256:')[:20]}",)


def _payloads(
    function_name: str,
    arguments: Mapping[str, object],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if function_name == "send_email":
        content_hash = stable_hash(
            {"subject": arguments.get("subject"), "body": arguments.get("body")}
        )
        ids = [f"agentdojo:workspace:email_content:{content_hash.removeprefix('sha256:')}"]
        types = ["email_content"]
        attachments = arguments.get("attachments") or []
        if isinstance(attachments, Sequence) and not isinstance(attachments, (str, bytes, bytearray)):
            for attachment in attachments:
                if isinstance(attachment, str):
                    ids.append(f"agentdojo:workspace:file:{attachment}")
                    types.append("cloud_drive_file")
                elif isinstance(attachment, Mapping) and attachment.get("file_id") is not None:
                    ids.append(f"agentdojo:workspace:file:{attachment['file_id']}")
                    types.append("cloud_drive_file")
                elif isinstance(attachment, Mapping) and attachment.get("event_details") is not None:
                    ids.append(f"agentdojo:workspace:calendar_event_payload:{stable_hash(attachment['event_details']).removeprefix('sha256:')}")
                    types.append("calendar_event")
        return tuple(ids), tuple(types)
    if function_name in {"send_direct_message", "send_channel_message"}:
        digest = stable_hash(arguments.get("body")).removeprefix("sha256:")
        return (f"agentdojo:slack:message_content:{digest}",), ("message_content",)
    if function_name in {"send_money", "schedule_transaction"}:
        digest = stable_hash(arguments).removeprefix("sha256:")
        return (f"agentdojo:banking:transaction_payload:{digest}",), ("transaction",)
    if function_name == "post_webpage":
        digest = stable_hash(arguments.get("body")).removeprefix("sha256:")
        return (f"agentdojo:slack:web_content:{digest}",), ("web_content",)
    return (), ()


def summarize_effects(
    calls: Sequence[ExecutedCall],
    changes: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    categories = Counter(
        category
        for call in calls
        for category in call.effect_categories
        if call.succeeded
    )
    domains = {
        category.split()[0].lower()
        for category in categories
        if category != "read-only access"
    }
    if len(domains) > 1:
        categories["cross-application workflow"] += 1
    return {
        "categories": dict(sorted(categories.items())),
        "has_consequential_mutation": bool(changes),
        "consequential_operation_count": sum(
            call.state_changed is True for call in calls
        ),
        "outgoing_operation_count": sum(
            call.succeeded and call.function_name in OUTGOING_FUNCTIONS for call in calls
        ),
    }


def audit_graph_fidelity(
    store: GraphStore,
    messages: Sequence[ChatMessage],
    observations: Sequence[RuntimeCallObservation],
) -> dict[str, object]:
    """Compare one AgentDojo attempt to its current CausalGuard graph."""

    nodes = list(store.nodes())
    edges = list(store.edges())
    tool_nodes = sorted(
        (node for node in nodes if node.node_type.value == "tool_call"),
        key=lambda node: node.timestamp,
    )
    llm_nodes = [node for node in nodes if node.node_type.value == "llm_invocation"]
    operation_nodes = {
        node.node_id: node for node in nodes if node.node_type.value == "system_operation"
    }
    proposed = [
        call
        for message in messages
        if message["role"] == "assistant"
        for call in (message.get("tool_calls") or [])
    ]
    results = [message for message in messages if message["role"] == "tool"]
    observed = list(observations)

    tool_sequence_correct = [call.function for call in proposed] == [
        node.tool_name for node in tool_nodes
    ]
    invokes_correct = all(
        any(
            edge.target_id == node.node_id
            and edge.edge_type is EdgeType.INVOKES
            and edge.source_id.startswith("llm:")
            for edge in edges
        )
        for node in tool_nodes
    )
    missing: list[str] = []
    semantic_operation_ok = True
    reads_ok = True
    writes_ok = True
    payload_ok = True
    destination_ok = True
    mutation_ok = True
    runtime_observations_ok = True

    for index, result in enumerate(results):
        call = result["tool_call"]
        argument_hash = stable_hash(call.args)
        observation = _pop_observation(observed, call.function, argument_hash)
        matching_tool = tool_nodes[index] if index < len(tool_nodes) else None
        triggered = []
        if matching_tool is not None:
            triggered = [
                operation_nodes[edge.target_id]
                for edge in edges
                if edge.source_id == matching_tool.node_id
                and edge.edge_type is EdgeType.TRIGGERS
                and edge.target_id in operation_nodes
            ]
        if result.get("error") is not None:
            if triggered:
                semantic_operation_ok = False
                missing.append(f"failed {call.function} has a successful SystemOperation")
            continue
        if observation is None:
            runtime_observations_ok = False
            missing.append(f"successful {call.function} lacks runtime observation")
            continue
        if not triggered:
            semantic_operation_ok = False
            missing.append(f"successful {call.function} lacks triggered SystemOperation")
            continue

        expected_type = EXPECTED_OPERATION_TYPES.get(call.function)
        if expected_type is None or not any(
            operation.operation_type == expected_type for operation in triggered
        ):
            semantic_operation_ok = False
            missing.append(f"{call.function} has only generic/unsupported operation semantics")

        related_edges = [
            edge
            for edge in edges
            if edge.source_id in {operation.node_id for operation in triggered}
            or edge.target_id in {operation.node_id for operation in triggered}
        ]
        if call.function in READ_FUNCTIONS and not any(
            edge.edge_type is EdgeType.READ for edge in related_edges
        ):
            reads_ok = False
            missing.append(f"{call.function} lacks concrete read provenance")
        if observation.state_changed is True and not any(
            edge.edge_type is EdgeType.WRITE for edge in related_edges
        ):
            writes_ok = False
            mutation_ok = False
            missing.append(f"{call.function} changed state without a write edge")
        actual_payloads, _ = _payloads(call.function, call.args)
        if actual_payloads:
            graph_payloads = {
                edge.source_id
                for edge in related_edges
                if edge.edge_type is EdgeType.PAYLOAD_OF
            }
            graph_resources = {
                node.resource_id
                for node in nodes
                if node.node_id in graph_payloads and node.node_type.value == "data_object"
            }
            if not set(actual_payloads).issubset(graph_resources):
                payload_ok = False
                missing.append(f"{call.function} actual payload is not fully represented by payload_of")
        actual_destinations = set(_destinations(call.function, call.args))
        if actual_destinations:
            graph_destinations = {
                operation.destination for operation in triggered if operation.destination
            }
            if actual_destinations != graph_destinations:
                destination_ok = False
                missing.append(f"{call.function} destination does not correlate with SystemOperation")

    runtime_match = runtime_observations_ok and not observed
    instrumentation_failure = not (
        tool_sequence_correct and invokes_correct and runtime_match and store.is_acyclic()
    )
    return {
        "llm_invocation_count": len(llm_nodes),
        "tool_call_count": len(tool_nodes),
        "system_operation_count": len(operation_nodes),
        "graph_node_count": store.node_count,
        "graph_edge_count": store.edge_count,
        "graph_acyclic": store.is_acyclic(),
        "tool_call_coverage_correct": tool_sequence_correct,
        "invocation_causality_correct": invokes_correct,
        "runtime_observer_correlation_correct": runtime_match,
        "system_operation_coverage_correct": semantic_operation_ok,
        "read_provenance_correct": reads_ok,
        "write_provenance_correct": writes_ok,
        "payload_provenance_correct": payload_ok,
        "destination_correlation_correct": destination_ok,
        "actual_mutation_vs_graph_consistent": mutation_ok,
        "missing_or_ambiguous_provenance": list(dict.fromkeys(missing)),
        "instrumentation_failure": instrumentation_failure,
    }


def aggregate_attempt_fidelity(
    audits: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    boolean_fields = (
        "graph_acyclic",
        "tool_call_coverage_correct",
        "invocation_causality_correct",
        "runtime_observer_correlation_correct",
        "system_operation_coverage_correct",
        "read_provenance_correct",
        "write_provenance_correct",
        "payload_provenance_correct",
        "destination_correlation_correct",
        "actual_mutation_vs_graph_consistent",
    )
    missing = [
        str(item)
        for audit in audits
        for item in audit.get("missing_or_ambiguous_provenance", [])
    ]
    return {
        "llm_invocation_count": sum(int(audit["llm_invocation_count"]) for audit in audits),
        "tool_call_count": sum(int(audit["tool_call_count"]) for audit in audits),
        "system_operation_count": sum(int(audit["system_operation_count"]) for audit in audits),
        "graph_node_count": sum(int(audit["graph_node_count"]) for audit in audits),
        "graph_edge_count": sum(int(audit["graph_edge_count"]) for audit in audits),
        **{
            field: all(bool(audit[field]) for audit in audits)
            for field in boolean_fields
        },
        "missing_or_ambiguous_provenance": list(dict.fromkeys(missing)),
        "instrumentation_failure": any(
            bool(audit["instrumentation_failure"]) for audit in audits
        ),
    }

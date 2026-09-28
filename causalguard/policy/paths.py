"""Bounded backward BFS for attested copy data flow into proposed attachments.

Only INPUT_TO at the sink and runtime-attested identity-copy READ/WRITE pairs
are data flow. No inference through exposure, invocation, or generic operations.
"""
from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

from causalguard.graph import GraphStore
from causalguard.policy.models import PathWitness, PolicyDecision, PolicyDefinition
from causalguard.schema.edges import Confidence, Derivation, EdgeType, ProvenanceEdge
from causalguard.schema.nodes import DataObjectNode, SystemOperationNode, ToolCallNode

if TYPE_CHECKING:
    from causalguard.policy.engine import PolicyEngine


def validate_sink_clock(policy: PolicyDefinition, tool: ToolCallNode) -> None:
    if policy.params.time_window is None:
        return
    summary = tool.argument_summary
    if (summary.get("clock_unit") != policy.params.time_window_unit
            or not summary.get("clock_domain")):
        raise ValueError("time_window requires a compatible, explicitly declared sink clock")


def validate_edge_clock(
    policy: PolicyDefinition, edge: ProvenanceEdge, tool: ToolCallNode | None,
) -> None:
    if policy.params.time_window is None:
        return
    if (edge.attributes.get("clock_unit") != policy.params.time_window_unit
            or not edge.attributes.get("clock_domain")
            or (tool is not None and edge.attributes["clock_domain"]
                != tool.argument_summary.get("clock_domain"))):
        raise ValueError("time_window edge clock is incompatible with the proposed sink")


def _in_window(policy: PolicyDefinition, edge: ProvenanceEdge, tool: ToolCallNode) -> bool:
    validate_edge_clock(policy, edge, tool)
    window = policy.params.time_window
    return window is None or tool.timestamp - window <= edge.timestamp <= tool.timestamp


def _explicit(edge: ProvenanceEdge, *, runtime: bool = False) -> bool:
    derivations = {Derivation.RUNTIME_OBSERVATION}
    if not runtime:
        derivations.add(Derivation.EXPLICIT_DATA_REFERENCE)
    return (edge.derivation in derivations and edge.confidence is Confidence.HIGH
            and bool(edge.evidence_ref))


def evaluate_paths(
    engine: PolicyEngine, policy: PolicyDefinition, store: GraphStore,
    tool: ToolCallNode, *, evaluated_at: float | None = None,
) -> PolicyDecision:
    from causalguard.policy.engine import _recipient_addresses

    if tool.action_class != "send_email":
        raise ValueError("bounded_data_flow currently supports only proposed send_email")
    validate_sink_clock(policy, tool)
    # Current attachment resolution is mandatory independently of history bounds.
    current_policy = policy.model_copy(update={
        "params": policy.params.model_copy(update={"time_window": None}),
    })
    selected = engine._select(current_policy, store, tool)
    reasons = set()
    if selected.missing_attachments:
        reasons.add("unresolved_attachment_evidence")
    destination = engine._destination_classification(policy, tool)
    if destination == "unresolved":
        reasons.add("unresolved_destination")
    graph = store.copy_networkx()
    queue = deque()
    paths = []
    states = 0
    allowed = set(policy.params.allowed_edge_types)

    def enqueue(nodes, edges, attachment):
        nonlocal states
        if states >= policy.params.max_search_states:
            reasons.add("search_state_budget_exhausted")
            return
        states += 1
        queue.append((nodes, edges, attachment))

    def incoming(node_id, kind):
        return sorted(
            (store.get_edge(edge_id) for _, _, edge_id
             in graph.in_edges(node_id, keys=True)
             if store.get_edge(edge_id).edge_type is kind),
            key=lambda edge: edge.edge_id,
        )

    if EdgeType.INPUT_TO in allowed:
        for edge in selected.attachment_edges:
            if not _in_window(policy, edge, tool):
                continue
            if not _explicit(edge):
                reasons.add("unattested_attachment_edge")
                continue
            attachment = store.get_node(edge.source_id)
            enqueue((edge.source_id, tool.node_id), (edge.edge_id,), attachment.resource_id)

    while queue:
        nodes, edges, attachment = queue.popleft()
        node = store.get_node(nodes[0])
        if isinstance(node, DataObjectNode) and engine._is_protected(policy, node):
            paths.append(PathWitness(node_ids=nodes, edge_ids=edges,
                                     source_resource_id=node.resource_id,
                                     attachment_resource_id=attachment))
            continue
        # Reaching the configured frontier completes the scoped search. It does
        # not assert that ancestors outside the bound are safe or fully captured.
        if len(edges) >= policy.params.max_hops:
            continue
        if isinstance(node, DataObjectNode):
            if EdgeType.WRITE not in allowed:
                continue
            writes = incoming(node.node_id, EdgeType.WRITE)
            if not writes and node.lineage_status != "root":
                reasons.add("missing_lineage:" + node.node_id)
            for edge in writes:
                if not _in_window(policy, edge, tool):
                    continue
                operation = store.get_node(edge.source_id)
                if not (isinstance(operation, SystemOperationNode)
                        and operation.operation_type == "controlled_file_copy"
                        and operation.data_flow_semantics == "identity_copy_v1"
                        and _explicit(edge, runtime=True)):
                    reasons.add("unsupported_or_unattested_derivation:" + edge.edge_id)
                    continue
                enqueue((operation.node_id, *nodes), (edge.edge_id, *edges), attachment)
        elif isinstance(node, SystemOperationNode):
            if EdgeType.READ not in allowed:
                continue
            reads = incoming(node.node_id, EdgeType.READ)
            if not reads:
                reasons.add("missing_copy_read:" + node.node_id)
            output = store.get_node(nodes[1])
            write = store.get_edge(edges[0])
            for edge in reads:
                if not _in_window(policy, edge, tool):
                    continue
                source = store.get_node(edge.source_id)
                # The supported mechanism is a single-source identity copy.
                # Parallel evidence for that same source remains distinct.
                if not (isinstance(source, DataObjectNode)
                        and len({e.source_id for e in reads}) == 1
                        and _explicit(edge, runtime=True)
                        and edge.evidence_ref == write.evidence_ref
                        and source.resource_id != output.resource_id
                        and source.content_hash is not None
                        and source.content_hash == output.content_hash):
                    reasons.add("incomplete_identity_copy_evidence:" + node.node_id)
                    continue
                enqueue((source.node_id, *nodes), (edge.edge_id, *edges), attachment)

    protected = {path.source_resource_id for path in paths}
    # Consent is for the actual outgoing resource, never inherited from its
    # ancestor. Existing exact action/destination/session/context checks apply.
    required_approvals = {path.attachment_resource_id for path in paths}
    approval_edges = tuple(
        edge for edge in selected.approval_edges
        if "exact_human_approval" in policy.exception.kinds
        and any(engine._approval_matches(
            policy, store.get_node(edge.source_id), tool, resource,
            tool.timestamp if evaluated_at is None else evaluated_at,
        ) for resource in required_approvals)
    )
    approved = {store.get_node(edge.source_id).resource_scope for edge in approval_edges}
    approval_valid = bool(required_approvals) and required_approvals <= approved
    trusted = destination == "trusted" and "trusted_destination" in policy.exception.kinds
    exception = "none"
    if reasons:
        action = policy.action.on_missing_evidence
        reason = "Required evidence or bounded search is incomplete; execution held."
        status = "incomplete"
    elif not paths:
        action = policy.action.on_no_match
        reason = "Completed no-match within configured bounds; not unrestricted safety."
        status = "no_match_within_bounds"
    elif trusted:
        action = policy.action.on_trusted_destination
        exception = "trusted_destination"
        reason = "Protected data reaches an attachment; all recipients are trusted."
        status = "match"
    elif approval_valid:
        action = policy.action.on_valid_approval
        exception = "valid_approval"
        reason = "Exact approval covers every protected outgoing attachment and destination."
        status = "match"
    else:
        action = policy.action.on_violation
        reason = "Protected data reaches a proposed external email by an attested path."
        status = "match"
    return PolicyDecision(
        policy_id=policy.policy_id, decision=action,
        triggering_tool_call_id=tool.node_id,
        protected_resource_ids=tuple(sorted(protected)),
        destination_classification=destination, exception_status=exception,
        evidence_status="missing" if reasons else "complete",
        evidence_node_ids=tuple(dict.fromkeys(n for path in paths for n in path.node_ids))
                          if paths else (tool.node_id,),
        evidence_edge_ids=tuple(dict.fromkeys(e for path in paths for e in path.edge_ids)),
        matching_paths=tuple(paths), search_status=status,
        incomplete_reasons=tuple(sorted(reasons)),
        recipient_addresses=_recipient_addresses(tool.destination),
        exception_evidence_node_ids=tuple(sorted({e.source_id for e in approval_edges})),
        exception_evidence_edge_ids=tuple(sorted(e.edge_id for e in approval_edges)),
        explanation=(reason + f" Recipient metadata: {destination}; "
                     + f"recipients={list(_recipient_addresses(tool.destination))}. "
                     + f"Exception: {exception}. Scope: max_hops={policy.params.max_hops}, "
                     + f"edges={[e.value for e in policy.params.allowed_edge_types]}, "
                     + f"window={policy.params.time_window} {policy.params.time_window_unit}."),
    )

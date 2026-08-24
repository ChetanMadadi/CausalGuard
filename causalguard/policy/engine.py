"""Bounded evaluation for trigger-driven provenance policies."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from fnmatch import fnmatchcase

from causalguard.graph import GraphStore
from causalguard.policy.models import (
    PolicyDecision,
    PolicyDefinition,
)
from causalguard.schema.edges import EdgeType, ProvenanceEdge
from causalguard.schema.nodes import DataObjectNode, HumanApprovalNode, ToolCallNode


@dataclass(frozen=True)
class _EvidenceSelection:
    node_ids: frozenset[str]
    edge_ids: frozenset[str]
    payload_node_ids: frozenset[str]
    approval_node_ids: frozenset[str]


class PolicyEngine:
    """Evaluate one policy from a proposed ToolCall using bounded provenance."""

    def evaluate(
        self,
        policy: PolicyDefinition,
        store: GraphStore,
        triggering_tool_call_id: str,
        *,
        evaluated_at: float | None = None,
    ) -> PolicyDecision | None:
        tool = store.get_node(triggering_tool_call_id)
        if not isinstance(tool, ToolCallNode):
            raise ValueError("triggering node must be a ToolCall")
        if tool.action_class != policy.trigger.action_class:
            return None

        evaluation_time = tool.timestamp if evaluated_at is None else evaluated_at
        selection = self._select(policy, store, tool)
        protected_resources = self._matched_protected_resources(
            policy,
            store,
            selection.payload_node_ids,
        )
        destination_classification = self._destination_classification(policy, tool)

        exception_status: str = "none"
        if not protected_resources:
            decision = policy.action.on_no_match
            explanation = "No configured protected resource is an exact outgoing payload."
        elif (
            "trusted_destination" in policy.exception.kinds
            and destination_classification == "trusted"
        ):
            decision = policy.action.on_trusted_destination
            exception_status = "trusted_destination"
            explanation = "The exact protected payload is going only to trusted recipients."
        elif (
            "exact_human_approval" in policy.exception.kinds
            and self._has_exact_approval(
                policy,
                store,
                tool,
                selection.approval_node_ids,
                protected_resources,
                evaluation_time,
            )
        ):
            decision = policy.action.on_valid_approval
            exception_status = "valid_approval"
            explanation = "An exact, unexpired HumanApproval covers the proposed action."
        else:
            decision = policy.action.on_violation
            explanation = (
                "A configured protected resource is an exact outgoing payload to an "
                "untrusted destination without an applicable exception."
            )

        return PolicyDecision(
            policy_id=policy.policy_id,
            decision=decision,
            triggering_tool_call_id=triggering_tool_call_id,
            protected_resource_ids=tuple(sorted(protected_resources)),
            destination_classification=destination_classification,
            exception_status=exception_status,
            evidence_node_ids=tuple(sorted(selection.node_ids)),
            evidence_edge_ids=tuple(sorted(selection.edge_ids)),
            explanation=explanation,
        )

    def _select(
        self,
        policy: PolicyDefinition,
        store: GraphStore,
        tool: ToolCallNode,
    ) -> _EvidenceSelection:
        graph = store.copy_networkx()
        node_ids = {tool.node_id}
        edge_ids: set[str] = set()
        payload_node_ids: set[str] = set()
        approval_node_ids: set[str] = set()
        queue = deque([(tool.node_id, 0)])
        visited_depth = {tool.node_id: 0}

        while queue:
            current_id, depth = queue.popleft()
            if depth >= policy.params.max_provenance_depth:
                continue
            for source_id, _, edge_id in graph.in_edges(current_id, keys=True):
                edge = store.get_edge(edge_id)
                if not self._within_time_window(policy, edge, tool.timestamp):
                    continue
                edge_ids.add(edge.edge_id)
                node_ids.add(source_id)
                if current_id == tool.node_id and edge.edge_type is EdgeType.INPUT_TO:
                    payload_node_ids.add(source_id)
                if current_id == tool.node_id and edge.edge_type is EdgeType.AUTHORIZES:
                    approval_node_ids.add(source_id)
                next_depth = depth + 1
                if visited_depth.get(source_id, next_depth + 1) > next_depth:
                    visited_depth[source_id] = next_depth
                    queue.append((source_id, next_depth))

        return _EvidenceSelection(
            node_ids=frozenset(node_ids),
            edge_ids=frozenset(edge_ids),
            payload_node_ids=frozenset(payload_node_ids),
            approval_node_ids=frozenset(approval_node_ids),
        )

    def _within_time_window(
        self,
        policy: PolicyDefinition,
        edge: ProvenanceEdge,
        trigger_timestamp: float,
    ) -> bool:
        window = policy.params.time_window
        if window is None:
            return True
        return trigger_timestamp - window <= edge.timestamp <= trigger_timestamp

    def _matched_protected_resources(
        self,
        policy: PolicyDefinition,
        store: GraphStore,
        payload_node_ids: frozenset[str],
    ) -> set[str]:
        matches: set[str] = set()
        configured_ids = set(policy.params.protected_resource_ids)
        for node_id in payload_node_ids:
            node = store.get_node(node_id)
            if not isinstance(node, DataObjectNode):
                continue
            if node.resource_id in configured_ids or any(
                fnmatchcase(node.resource_id, pattern)
                for pattern in policy.params.protected_resource_patterns
            ):
                matches.add(node.resource_id)
        return matches

    def _destination_classification(
        self,
        policy: PolicyDefinition,
        tool: ToolCallNode,
    ) -> str:
        recipients = _recipient_addresses(tool.destination)
        if not recipients:
            return "untrusted"
        trusted_recipients = {
            item.removeprefix("mailto:").lower()
            for item in policy.params.trusted_recipients
        }
        trusted_domains = {
            item.lower().removeprefix("@")
            for item in policy.params.trusted_domains
        }
        if all(
            recipient in trusted_recipients
            or _recipient_domain(recipient) in trusted_domains
            for recipient in recipients
        ):
            return "trusted"
        return "untrusted"

    def _has_exact_approval(
        self,
        policy: PolicyDefinition,
        store: GraphStore,
        tool: ToolCallNode,
        approval_node_ids: frozenset[str],
        protected_resources: set[str],
        evaluated_at: float,
    ) -> bool:
        approvals = [
            node
            for node_id in approval_node_ids
            if isinstance((node := store.get_node(node_id)), HumanApprovalNode)
        ]
        return all(
            any(
                self._approval_matches(
                    policy,
                    approval,
                    tool,
                    resource_id,
                    evaluated_at,
                )
                for approval in approvals
            )
            for resource_id in protected_resources
        )

    def _approval_matches(
        self,
        policy: PolicyDefinition,
        approval: HumanApprovalNode,
        tool: ToolCallNode,
        resource_id: str,
        evaluated_at: float,
    ) -> bool:
        if approval.action_class != tool.action_class:
            return False
        if approval.resource_scope != resource_id:
            return False
        if approval.destination_scope != tool.destination:
            return False
        if approval.session_id is not None and approval.session_id != tool.session_id:
            return False
        if (
            approval.causal_context_id is not None
            and approval.causal_context_id != tool.causal_context_id
        ):
            return False
        if approval.timestamp > evaluated_at:
            return False
        if approval.expiration is None:
            return policy.params.approval_expiration_behavior == "allow_unbounded"
        return approval.expiration >= evaluated_at


def _recipient_addresses(destination: str | None) -> tuple[str, ...]:
    if destination is None:
        return ()
    return tuple(
        item.strip().removeprefix("mailto:").lower()
        for item in destination.split(",")
        if item.strip()
    )


def _recipient_domain(recipient: str) -> str:
    _, separator, domain = recipient.rpartition("@")
    return domain if separator else ""

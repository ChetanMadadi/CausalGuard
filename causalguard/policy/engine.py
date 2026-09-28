"""Immediate protected-attachment policy evaluation at the proposed tool gate."""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
import re

from causalguard.graph import GraphStore
from causalguard.policy.models import PolicyDecision, PolicyDefinition
from causalguard.schema.edges import EdgeType, ProvenanceEdge
from causalguard.schema.nodes import DataObjectNode, HumanApprovalNode, ToolCallNode


@dataclass(frozen=True)
class _EvidenceSelection:
    attachment_edges: tuple[ProvenanceEdge, ...]
    approval_edges: tuple[ProvenanceEdge, ...]
    missing_attachments: bool


class PolicyEngine:
    """Match only adapter-attested attachments directly input to this email."""

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

        if policy.select.kind == "bounded_data_flow":
            from causalguard.policy.paths import evaluate_paths
            return evaluate_paths(self, policy, store, tool, evaluated_at=evaluated_at)
        if policy.params.time_window is not None:
            from causalguard.policy.paths import validate_sink_clock
            validate_sink_clock(policy, tool)

        evaluation_time = tool.timestamp if evaluated_at is None else evaluated_at
        selection = self._select(policy, store, tool)
        protected_edges = tuple(
            edge for edge in selection.attachment_edges
            if self._is_protected(policy, store.get_node(edge.source_id))
        )
        protected_resources = {
            store.get_node(edge.source_id).resource_id for edge in protected_edges
        }
        destination_classification = self._destination_classification(policy, tool)
        recipients = _recipient_addresses(tool.destination)
        trusted_destination = (
            "trusted_destination" in policy.exception.kinds
            and destination_classification == "trusted"
        )
        approval_edges = tuple(
            edge for edge in selection.approval_edges
            if "exact_human_approval" in policy.exception.kinds
            and any(
                self._approval_matches(
                    policy, store.get_node(edge.source_id), tool, resource_id,
                    evaluation_time,
                )
                for resource_id in protected_resources
            )
        )
        approved_resources = {
            store.get_node(edge.source_id).resource_scope for edge in approval_edges
        }
        valid_approval = bool(protected_resources) and protected_resources <= approved_resources
        missing_destination = destination_classification == "unresolved"
        missing = selection.missing_attachments or missing_destination
        exception_status = "none"

        # Resolve all required evidence before claiming a definite policy match.
        if missing:
            decision = policy.action.on_missing_evidence
            reason = (
                "Required evidence unresolved: "
                + ", ".join(name for name, absent in (
                    ("attachments", selection.missing_attachments),
                    ("destination", missing_destination),
                ) if absent)
                + ". Execution is held."
            )
        elif protected_resources and not trusted_destination and not valid_approval:
            decision = policy.action.on_violation
            reason = "Protected outgoing attachment to an unauthorized destination."
        elif not protected_resources:
            decision = policy.action.on_no_match
            reason = "No configured protected object matches an outgoing attachment."
        elif trusted_destination:
            decision = policy.action.on_trusted_destination
            exception_status = "trusted_destination"
            reason = "All recipients of the protected attachments satisfy trusted configuration."
        else:
            decision = policy.action.on_valid_approval
            exception_status = "valid_approval"
            reason = "Exact, unexpired action approval covers every protected attachment."

        # A direct witness only: no preceding read, LLM, or transitive edges.
        witness_edges = protected_edges or selection.attachment_edges
        witness_nodes = {tool.node_id, *(edge.source_id for edge in witness_edges)}
        return PolicyDecision(
            policy_id=policy.policy_id,
            decision=decision,
            triggering_tool_call_id=tool.node_id,
            protected_resource_ids=tuple(sorted(protected_resources)),
            destination_classification=destination_classification,
            exception_status=exception_status,
            evidence_status="missing" if missing else "complete",
            evidence_node_ids=tuple(sorted(witness_nodes)),
            evidence_edge_ids=tuple(sorted(edge.edge_id for edge in witness_edges)),
            recipient_addresses=recipients,
            exception_evidence_node_ids=tuple(sorted({
                edge.source_id for edge in approval_edges
            })),
            exception_evidence_edge_ids=tuple(sorted(edge.edge_id for edge in approval_edges)),
            explanation=(
                reason
                + f" Recipient metadata: {destination_classification}; "
                + f"recipients={list(recipients)}. Exception: {exception_status}."
            ),
        )

    def _select(
        self, policy: PolicyDefinition, store: GraphStore, tool: ToolCallNode,
    ) -> _EvidenceSelection:
        # Reuse ToolCall.argument_summary; this field is populated by the trusted
        # proposal adapter, not model-supplied claims or generic input_refs.
        refs = tool.argument_summary.get("outgoing_attachment_refs")
        known = isinstance(refs, list) and all(
            isinstance(ref, str) and bool(ref.strip()) for ref in refs
        )
        expected = set(refs) if known else set()
        attachments, approvals = [], []
        resolved = set()
        missing = not known
        graph = store.copy_networkx()
        for source_id, _, edge_id in graph.in_edges(tool.node_id, keys=True):
            edge = store.get_edge(edge_id)
            if edge.edge_type is not EdgeType.AUTHORIZES and not self._within_time_window(policy, edge, tool):
                continue
            source = store.get_node(source_id)
            if edge.edge_type is EdgeType.AUTHORIZES and isinstance(source, HumanApprovalNode):
                approvals.append(edge)
            elif (
                edge.edge_type is EdgeType.INPUT_TO
                and isinstance(source, DataObjectNode)
                and source.resource_id in expected
            ):
                attachments.append(edge)
                resolved.add(source.resource_id)
                if source.object_kind == "unresolved_cloud_drive_attachment":
                    missing = True
        missing = missing or bool(expected - resolved)
        return _EvidenceSelection(tuple(attachments), tuple(approvals), missing)

    def _within_time_window(
        self, policy: PolicyDefinition, edge: ProvenanceEdge, tool: ToolCallNode,
    ) -> bool:
        # Direct-mode filtering retains its conservative missing-evidence result.
        # Numeric windows now require declared units and a compatible clock.
        window = policy.params.time_window
        if window is not None:
            from causalguard.policy.paths import validate_edge_clock
            validate_edge_clock(policy, edge, tool)
        return window is None or tool.timestamp - window <= edge.timestamp <= tool.timestamp

    def _is_protected(self, policy, node) -> bool:
        return isinstance(node, DataObjectNode) and (
            node.resource_id in policy.params.protected_resource_ids
            or any(fnmatchcase(node.resource_id, pattern)
                   for pattern in policy.params.protected_resource_patterns)
            or node.sensitivity in policy.params.protected_sensitivities
        )

    def _destination_classification(
        self, policy: PolicyDefinition, tool: ToolCallNode,
    ) -> str:
        recipients = _recipient_addresses(tool.destination)
        if not recipients:
            return "unresolved"
        trusted_recipients = {
            item.strip().lower().removeprefix("mailto:")
            for item in policy.params.trusted_recipients
        }
        trusted_domains = {
            item.strip().lower().removeprefix("@")
            for item in policy.params.trusted_domains
        }
        return "trusted" if all(
            recipient in trusted_recipients
            or recipient.rpartition("@")[2] in trusted_domains
            for recipient in recipients
        ) else "untrusted"

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
    recipients = tuple(
        item.strip().lower().removeprefix("mailto:")
        for item in destination.split(",")
    )
    # Conservative mailbox metadata validation; never discard a malformed
    # CC/BCC component and accidentally authorize only the remaining addresses.
    if any(re.fullmatch(r"[^\s@,;<>:]+@[^\s@,;<>:]+", item) is None for item in recipients):
        return ()
    return recipients

"""Observational, configurable path alerts. Never returns enforcement decisions."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator
from causalguard.schema.privacy import assert_no_raw_content_keys


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NodePattern(_Model):
    node_type: str = Field(min_length=1)
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("attributes")
    @classmethod
    def validate_attributes(cls, attributes):
        assert_no_raw_content_keys(attributes)
        for key, value in attributes.items():
            if not key.strip():
                raise ValueError("empty attribute name")
            values = value if isinstance(value, list) else [value]
            if not values or any(type(v) not in (str, int, float, bool) or
                                 (type(v) is float and not math.isfinite(v)) for v in values):
                raise ValueError("attributes support only finite scalars or nonempty scalar lists")
        return attributes


class PathConstraints(_Model):
    max_hops: int = Field(ge=1, le=64, strict=True)
    allowed_edge_types: tuple[str, ...]
    time_window_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    max_search_states: int = Field(default=10000, ge=1, le=1000000, strict=True)

    @field_validator("allowed_edge_types")
    @classmethod
    def validate_edges(cls, values):
        if not values or any(not v.strip() for v in values) or len(set(values)) != len(values):
            raise ValueError("allowed_edge_types must be nonempty, unique names")
        return values


class AlertAction(_Model):
    type: Literal["alert"]
    severity: Literal["info", "low", "medium", "high", "critical"]


class PathAlertPolicy(_Model):
    policy_id: str = Field(min_length=1)
    type: Literal["path"]
    name: str = Field(min_length=1)
    description: str
    source: NodePattern
    sink: NodePattern
    constraints: PathConstraints
    action: AlertAction
    enabled: bool = True


class EvaluationClock(_Model):
    current_time: float = Field(ge=0, allow_inf_nan=False)
    unit: Literal["seconds", "ordinal"]
    domain: str = Field(min_length=1)


@dataclass(frozen=True)
class AlertNode:
    node_id: str
    node_type: str
    attributes: dict[str, Any]


@dataclass(frozen=True)
class AlertEdge:
    edge_id: str
    source_id: str
    target_id: str
    edge_type: str
    timestamp: float
    clock_unit: str | None
    clock_domain: str | None
    evidence_basis: str
    eligible: bool = True


class AlertGraphView(Protocol):
    supported_node_types: frozenset[str]
    supported_edge_types: frozenset[str]
    coverage_issues: tuple[str, ...]

    def nodes(self) -> list[AlertNode]: ...
    def incoming(self, node_id: str) -> list[AlertEdge]: ...


class PathAlert(_Model):
    policy_id: str
    policy_name: str
    severity: str
    attempt_id: str
    evaluation_timestamp: float
    clock_domain: str
    clock_unit: str
    triggering_update_id: str
    source_id: str
    sink_id: str
    witness_node_ids: tuple[str, ...]
    witness_edge_ids: tuple[str, ...]
    witness_edge_types: tuple[str, ...]
    evidence_basis: tuple[str, ...]
    explanation: str = "Configured path indicates possible information flow; it does not prove outgoing secret bytes."


class AlertEvaluation(_Model):
    policy_id: str
    attempt_id: str
    triggering_update_id: str
    status: Literal["match", "no_match_within_bounds", "incomplete", "disabled"]
    search_states: int
    alerts: tuple[PathAlert, ...] = ()
    coverage_issues: tuple[str, ...] = ()


def _same(left, right):
    # JSON numeric equality, but Boolean True must not match numeric 1.
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    return type(left) in (str, int, float) and type(right) in (str, int, float) and left == right


def matches(pattern: NodePattern, node: AlertNode) -> bool:
    if pattern.node_type != node.node_type:
        return False
    for key, expected in pattern.attributes.items():
        if key not in node.attributes:
            return False
        actual = node.attributes[key]
        candidates = actual if isinstance(actual, list) else [actual]
        accepted = expected if isinstance(expected, list) else [expected]
        if not any(_same(value, wanted) for value in candidates for wanted in accepted):
            return False
    return True


def validate_configuration(policy: PathAlertPolicy, view: AlertGraphView, clock: EvaluationClock) -> None:
    for pattern in (policy.source, policy.sink):
        if pattern.node_type not in view.supported_node_types:
            raise ValueError("unsupported node_type for graph view: " + pattern.node_type)
    unsupported = set(policy.constraints.allowed_edge_types) - view.supported_edge_types
    if unsupported:
        raise ValueError("unsupported edge types for graph view: " + repr(sorted(unsupported)))
    if policy.constraints.time_window_seconds is not None and clock.unit != "seconds":
        raise ValueError("seconds policy requires a seconds clock, not transcript ordinals")


def evaluate_alert_policy(policy: PathAlertPolicy, view: AlertGraphView, clock: EvaluationClock,
                          *, attempt_id: str, update_id: str) -> AlertEvaluation:
    """Bounded backward BFS; enumerate deterministic simple-path witnesses.

    Every directed multiedge retains its identity. Hop/window exclusions are
    scope; state-budget exhaustion is incomplete. Observation coverage is separate.
    """
    if not attempt_id or not update_id:
        raise ValueError("attempt and update IDs are required")
    if not policy.enabled:
        return AlertEvaluation(policy_id=policy.policy_id, attempt_id=attempt_id,
                               triggering_update_id=update_id, status="disabled", search_states=0)
    validate_configuration(policy, view, clock)
    nodes = {node.node_id: node for node in view.nodes()}
    issues = set(view.coverage_issues)
    for node in nodes.values():
        for pattern in (policy.source, policy.sink):
            if node.node_type == pattern.node_type:
                for key in pattern.attributes:
                    if key not in node.attributes or node.attributes[key] is None:
                        issues.add("missing_attribute:" + node.node_id + ":" + key)
    queue = deque()
    states, exhausted = 0, False
    alerts = []
    def enqueue(path_nodes, path_edges):
        nonlocal states, exhausted
        if states >= policy.constraints.max_search_states:
            exhausted = True
            return
        states += 1
        queue.append((path_nodes, path_edges))

    for node in sorted(nodes.values(), key=lambda n: n.node_id):
        if matches(policy.sink, node):
            enqueue((node.node_id,), ())
    while queue:
        path_nodes, path_edges = queue.popleft()
        current = nodes[path_nodes[0]]
        if path_edges and matches(policy.source, current):
            alerts.append(PathAlert(
                policy_id=policy.policy_id, policy_name=policy.name,
                severity=policy.action.severity, attempt_id=attempt_id,
                evaluation_timestamp=clock.current_time, clock_domain=clock.domain,
                clock_unit=clock.unit, triggering_update_id=update_id,
                source_id=current.node_id, sink_id=path_nodes[-1],
                witness_node_ids=path_nodes,
                witness_edge_ids=tuple(e.edge_id for e in path_edges),
                witness_edge_types=tuple(e.edge_type for e in path_edges),
                evidence_basis=tuple(e.evidence_basis for e in path_edges),
            ))
        if len(path_edges) >= policy.constraints.max_hops:
            continue
        for edge in sorted(view.incoming(current.node_id), key=lambda e: (e.source_id, e.edge_id)):
            if edge.edge_type not in policy.constraints.allowed_edge_types:
                continue
            if not edge.eligible:
                issues.add("unsupported_evidence:" + edge.edge_id)
                continue
            if edge.source_id not in nodes or edge.target_id != current.node_id:
                issues.add("missing_or_invalid_endpoint:" + edge.edge_id)
                continue
            window = policy.constraints.time_window_seconds
            if window is not None:
                if edge.clock_unit != "seconds" or edge.clock_domain != clock.domain:
                    raise ValueError("seconds window requires compatible edge clock domains")
                if not math.isfinite(edge.timestamp):
                    raise ValueError("nonfinite edge timestamp")
                if not clock.current_time - window <= edge.timestamp <= clock.current_time:
                    continue
            if edge.source_id not in path_nodes:
                enqueue((edge.source_id, *path_nodes), (edge, *path_edges))
    if exhausted:
        issues.add("search_state_budget_exhausted")
    alerts.sort(key=lambda a: (len(a.witness_edge_ids), a.source_id, a.sink_id, a.witness_edge_ids))
    return AlertEvaluation(
        policy_id=policy.policy_id, attempt_id=attempt_id, triggering_update_id=update_id,
        status="incomplete" if exhausted else "match" if alerts else "no_match_within_bounds",
        search_states=states, alerts=tuple(alerts), coverage_issues=tuple(sorted(issues)),
    )


class PathAlertMonitor:
    """Post-update observer with attempt-scoped deduplication, not an enforcer."""
    def __init__(self, policies: list[PathAlertPolicy], *, jsonl_path: Path | None = None):
        if len({p.policy_id for p in policies}) != len(policies):
            raise ValueError("policy IDs must be unique")
        self.policies = tuple(policies)
        self.jsonl_path = jsonl_path
        self.alerts: list[PathAlert] = []
        self.evaluations: list[AlertEvaluation] = []
        self.attempt_id = ""
        self._seen = set()
        self._attempts = set()

    def begin_attempt(self, attempt_id: str) -> None:
        if not attempt_id or attempt_id in self._attempts:
            raise ValueError("attempt IDs must be nonempty and unique")
        self.attempt_id = attempt_id
        self._attempts.add(attempt_id)
        self._seen.clear()

    def on_update(self, view: AlertGraphView, clock: EvaluationClock, update_id: str):
        if not self.attempt_id:
            raise ValueError("begin_attempt must precede graph updates")
        emitted = []
        for policy in self.policies:
            result = evaluate_alert_policy(policy, view, clock, attempt_id=self.attempt_id, update_id=update_id)
            self.evaluations.append(result.model_copy(update={"alerts": ()}))
            for alert in result.alerts:
                key = (alert.policy_id, alert.source_id, alert.sink_id, alert.witness_edge_ids)
                if key not in self._seen:
                    self._seen.add(key)
                    self.alerts.append(alert)
                    emitted.append(alert)
                    if self.jsonl_path is not None:
                        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
                        with self.jsonl_path.open("a") as handle:
                            handle.write(alert.model_dump_json() + "\n")
        return tuple(emitted)

    def to_jsonl(self) -> str:
        return "".join(a.model_dump_json() + "\n" for a in self.alerts)

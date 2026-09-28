from __future__ import annotations
import json
import networkx as nx
import pytest
from causalguard.policy.alerts import (
    PathAlertPolicy, NodePattern, AlertNode, EvaluationClock, PathAlertMonitor,
    evaluate_alert_policy, matches,
)
from causalguard.policy.alert_views import SyntheticAlertView, ProductionAlertView
from causalguard.graph import GraphStore
from scripts.run_path_alert_demo import load_policy, synthetic_graph, synthetic, application


def graph():
    g, edges = synthetic_graph()
    for source, target, eid, kind in edges:
        g.add_edge(source, target, key=eid, edge_type=kind, timestamp=400,
                   clock_unit="seconds", clock_domain="test")
    return g


def evaluate(g, policy=None, unit="seconds"):
    return evaluate_alert_policy(policy or load_policy("path_alert_P001.json"), SyntheticAlertView(g),
                                 EvaluationClock(current_time=500, unit=unit, domain="test"),
                                 attempt_id="attempt", update_id="update")


def test_reference_stream_and_precision_example(tmp_path):
    monitor = synthetic(tmp_path)
    assert len(monitor.alerts) == 1
    alert = monitor.alerts[0]
    assert alert.witness_node_ids == ("secret-file", "reader", "external")
    assert alert.witness_edge_ids == ("read-secret", "send-public")
    assert alert.witness_edge_types == ("read", "send")
    assert "possible information flow" in alert.explanation
    assert all("synthetic" in e for e in alert.evidence_basis)
    assert json.loads((tmp_path / "summary.json").read_text())["synthetic_edges"] == 25
    assert len((tmp_path / "alerts.jsonl").read_text().splitlines()) == 1


@pytest.mark.parametrize("change", ["public", "internal", "disconnect", "reverse", "disallowed"])
def test_no_match_conditions(change):
    g = graph()
    if change == "public":
        g.nodes["secret-file"]["sensitivity"] = "public"
    elif change == "internal":
        g.nodes["external"]["network_zone"] = "internal"
    elif change == "disconnect":
        g.remove_edge("reader", "external", "send-public")
    elif change == "reverse":
        data = dict(g.edges["secret-file", "reader", "read-secret"])
        g.remove_edge("secret-file", "reader", "read-secret")
        g.add_edge("reader", "secret-file", key="reverse", **data)
    else:
        g.edges["reader", "external", "send-public"]["edge_type"] = "write"
    result = evaluate(g)
    assert result.status == "no_match_within_bounds"
    assert result.alerts == ()


@pytest.mark.parametrize("hops,matched", [(5, True), (6, False)])
def test_exact_hop_bound(hops, matched):
    g = nx.MultiDiGraph()
    g.add_node("source", node_type="File", sensitivity="secret")
    g.add_node("sink", node_type="Socket", network_zone="external")
    path = ["source", *[f"p{i}" for i in range(hops - 1)], "sink"]
    for node in path[1:-1]:
        g.add_node(node, node_type="Process")
    for i, (left, right) in enumerate(zip(path, path[1:])):
        g.add_edge(left, right, key=f"e{i}", edge_type="read" if i == 0 else "send" if i == hops - 1 else "fork",
                   timestamp=400, clock_unit="seconds", clock_domain="test")
    result = evaluate(g)
    assert bool(result.alerts) == matched
    if matched:
        assert result.alerts[0].witness_node_ids == tuple(path)


@pytest.mark.parametrize("timestamp,matched", [(199.9, False), (200, True), (500, True), (500.1, False)])
def test_window_boundaries(timestamp, matched):
    g = graph()
    g.edges["secret-file", "reader", "read-secret"]["timestamp"] = timestamp
    assert bool(evaluate(g).alerts) == matched


def test_seconds_policy_rejects_ordinals_even_without_match():
    with pytest.raises(ValueError, match="seconds clock"):
        evaluate(graph(), unit="ordinal")


def test_seconds_policy_rejects_different_edge_clock():
    g = graph()
    g.edges["secret-file", "reader", "read-secret"]["clock_domain"] = "other"
    with pytest.raises(ValueError, match="clock domains"):
        evaluate(g)


def test_parallel_edges_preserve_only_eligible_witness():
    g = graph()
    g.edges["secret-file", "reader", "read-secret"]["timestamp"] = 0
    g.add_edge("secret-file", "reader", key="eligible-parallel", edge_type="read",
               timestamp=400, clock_unit="seconds", clock_domain="test")
    result = evaluate(g)
    assert len(result.alerts) == 1
    assert result.alerts[0].witness_edge_ids == ("eligible-parallel", "send-public")


def test_budget_exhaustion_is_incomplete_not_clean():
    policy = load_policy("path_alert_P001.json")
    policy = policy.model_copy(update={"constraints": policy.constraints.model_copy(update={"max_search_states": 1})})
    result = evaluate(graph(), policy)
    assert result.status == "incomplete"
    assert "search_state_budget_exhausted" in result.coverage_issues


def test_attribute_updates_dedup_attempts_and_new_witnesses(tmp_path):
    monitor = PathAlertMonitor([load_policy("path_alert_P001.json")], jsonl_path=tmp_path / "alerts.jsonl")
    monitor.begin_attempt("one")
    g = graph()
    clock = EvaluationClock(current_time=500, unit="seconds", domain="test")
    g.nodes["external"]["network_zone"] = "internal"
    assert not monitor.on_update(SyntheticAlertView(g), clock, "edges-added")
    g.nodes["external"]["network_zone"] = "external"
    alert, = monitor.on_update(SyntheticAlertView(g), clock, "socket-attribute-update")
    assert alert.triggering_update_id not in alert.witness_edge_ids
    assert not monitor.on_update(SyntheticAlertView(g), clock, "repeat-prefix")
    g.add_edge("secret-file", "reader", key="another-read", edge_type="read",
               timestamp=400, clock_unit="seconds", clock_domain="test")
    assert len(monitor.on_update(SyntheticAlertView(g), clock, "new-parallel")) == 1
    monitor.begin_attempt("two")
    assert len(monitor.on_update(SyntheticAlertView(g), clock, "same-prefix-new-attempt")) == 2
    assert len((tmp_path / "alerts.jsonl").read_text().splitlines()) == 4


def test_attribute_matching_is_exact_boolean_and_membership():
    node = AlertNode("n", "File", {"active": True, "tags": ["secret", "internal"], "size": 2})
    assert matches(NodePattern(node_type="File", attributes={"active": True, "tags": "secret"}), node)
    assert matches(NodePattern(node_type="File", attributes={"tags": ["public", "internal"]}), node)
    assert not matches(NodePattern(node_type="File", attributes={"active": 1}), node)
    assert not matches(NodePattern(node_type="File", attributes={"missing": False}), node)
    with pytest.raises(ValueError):
        NodePattern(node_type="File", attributes={"tags": {"regex": ".*"}})


def test_missing_attribute_and_unsupported_evidence_reported_separately():
    g = graph()
    del g.nodes["secret-file"]["sensitivity"]
    result = evaluate(g)
    assert result.status == "no_match_within_bounds"
    assert "missing_attribute:secret-file:sensitivity" in result.coverage_issues
    g = graph()
    g.edges["secret-file", "reader", "read-secret"]["eligible"] = False
    result = evaluate(g)
    assert not result.alerts
    assert "unsupported_evidence:read-secret" in result.coverage_issues


def test_os_policy_is_rejected_on_production_schema_not_relabelled():
    with pytest.raises(ValueError, match="unsupported node_type"):
        evaluate_alert_policy(load_policy("path_alert_P001.json"), ProductionAlertView(GraphStore()),
                              EvaluationClock(current_time=1, unit="seconds", domain="test"),
                              attempt_id="test", update_id="test")


@pytest.mark.parametrize("change", ["event", "deny", "bad-edge"])
def test_unsupported_policy_contract_rejected(change):
    data = load_policy("path_alert_P001.json").model_dump()
    if change == "event":
        data["type"] = "event"
    elif change == "deny":
        data["action"]["type"] = "deny"
    else:
        data["constraints"]["allowed_edge_types"] = ["teleport"]
    if change == "bad-edge":
        with pytest.raises(ValueError, match="unsupported edge"):
            evaluate(graph(), PathAlertPolicy.model_validate(data))
    else:
        with pytest.raises(ValueError):
            PathAlertPolicy.model_validate(data)


def test_disabled_policy_emits_nothing():
    policy = load_policy("path_alert_P001.json").model_copy(update={"enabled": False})
    assert evaluate(graph(), policy).status == "disabled"


def test_real_collector_context_alert_does_not_block_public_email(tmp_path):
    collector, monitor, store, summary = application(tmp_path)
    assert summary["enforcement"] == ["allow"]
    assert summary["email_count_delta"] == 1
    assert monitor.alerts
    assert len(monitor.alerts[0].witness_edge_ids) <= 5
    alert = monitor.alerts[0]
    assert store.get_node(alert.sink_id).operation_type == "email_send"
    assert any("context_availability" in b for b in alert.evidence_basis)
    for i, eid in enumerate(alert.witness_edge_ids):
        edge = store.get_edge(eid)
        assert (edge.source_id, edge.target_id) == alert.witness_node_ids[i:i + 2]
    assert len((tmp_path / "alert_evaluations.jsonl").read_text().splitlines()) == 2


def test_denied_send_cannot_supply_committed_sink(tmp_path):
    collector, monitor, store, summary = application(tmp_path, denied=True)
    assert summary["enforcement"] == ["deny"]
    assert summary["email_count_delta"] == 0
    assert not monitor.alerts
    assert not any(getattr(n, "operation_type", None) == "email_send" for n in store.nodes())
    assert not any(e.edge_type.value in {"write", "payload_of"} for e in store.edges())

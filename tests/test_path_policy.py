from __future__ import annotations

import pytest

from causalguard.graph import GraphBuilder, GraphStore
from causalguard.graph.builder import GraphBuilderConfig
from causalguard.policy import PolicyAction, PolicyEngine, protected_file_external_email_policy
from causalguard.schema.edges import EdgeType

SOURCE, OUTPUT, SINK = "resource:protected", "resource:export", "tool:send"


def event(eid, kind, timestamp, **attributes):
    return dict(event_id=eid, event_type=kind, timestamp=timestamp, agent_id="test",
                session_id="test", causal_context_id="test", parent_event_id=None,
                attributes={"clock_unit": "seconds", "clock_domain": "synthetic-clock", **attributes})


def metadata(resource, *, lineage="root", sensitivity="unknown"):
    return dict(node_id="data:" + resource, resource_id=resource, object_kind="file",
                content_hash="sha256:identical", version="v1", sensitivity=sensitivity,
                lineage_status=lineage)


def trace(*, source=SOURCE, copy_time=8, send_time=10, semantics="identity_copy_v1",
          omit_copy=False, reverse=False, output_lineage="copy_output", clock="seconds"):
    objects = {source: metadata(source), OUTPUT: metadata(OUTPUT, lineage=output_lineage)}
    copy = event("copy", "system_operation", copy_time,
                 operation_type="controlled_file_copy", data_flow_semantics=semantics,
                 read_refs=[OUTPUT if reverse else source],
                 write_refs=[source if reverse else OUTPUT], data_objects=objects,
                 data_derivation="runtime_observation", clock_unit=clock)
    send = event("send", "tool_call", send_time, tool_name="send_email",
                 action_class="send_email", destination="mailto:outside@example.org",
                 input_refs=[OUTPUT], data_objects=objects, clock_unit=clock,
                 argument_summary={"outgoing_attachment_refs": [OUTPUT]})
    return [send] if omit_copy else [copy, send]


def build(events):
    store = GraphStore()
    # These synthetic clocks test explicit data edges, not context inference.
    GraphBuilder(store, GraphBuilderConfig(infer_causal_context_edges=False)).process_trace(events)
    return store


def decide(store=None, **params):
    policy = protected_file_external_email_policy(
        path_mode=True, protected_resource_ids=(SOURCE,), **params,
    )
    return PolicyEngine().evaluate(policy, store or build(trace()), SINK)


def clone(store, *, edge_transform=lambda e: e, node_transform=lambda n: n):
    result = GraphStore()
    for node in store.nodes():
        result.add_node(node_transform(node))
    for edge in store.edges():
        transformed = edge_transform(edge)
        if transformed is not None:
            result.add_edge(transformed)
    return result


def test_three_hop_witness_is_ordered_and_endpoint_correct():
    store = build(trace())
    d = decide(store)
    assert d.decision is PolicyAction.DENY
    assert d.search_status == "match"
    path, = d.matching_paths
    assert path.node_ids == ("data:" + SOURCE, "sys:copy", "data:" + OUTPUT, SINK)
    assert [store.get_edge(e).edge_type for e in path.edge_ids] == [
        EdgeType.READ, EdgeType.WRITE, EdgeType.INPUT_TO,
    ]
    for index, edge_id in enumerate(path.edge_ids):
        edge = store.get_edge(edge_id)
        assert (edge.source_id, edge.target_id) == path.node_ids[index:index + 2]
    assert d.evidence_node_ids == path.node_ids
    assert d.evidence_edge_ids == path.edge_ids


@pytest.mark.parametrize("hops,action,status", [
    (1, PolicyAction.ALLOW, "no_match_within_bounds"),
    (2, PolicyAction.ALLOW, "no_match_within_bounds"),
    (3, PolicyAction.DENY, "match"),
    (4, PolicyAction.DENY, "match"),
])
def test_hop_frontier_is_scope_not_incomplete(hops, action, status):
    d = decide(max_hops=hops)
    assert (d.decision, d.search_status) == (action, status)


def test_public_source_copy_completes_no_match():
    d = decide(build(trace(source="resource:public")))
    assert d.decision is PolicyAction.ALLOW
    assert d.search_status == "no_match_within_bounds"


@pytest.mark.parametrize("options", [
    {"omit_copy": True}, {"semantics": None}, {"reverse": True},
])
def test_missing_unknown_or_wrong_direction_provenance_never_proves_derivation(options):
    d = decide(build(trace(**options)))
    assert d.decision is PolicyAction.REQUEST_APPROVAL
    assert d.search_status == "incomplete"
    assert not d.matching_paths
    assert d.incomplete_reasons


def test_sensitive_read_and_unrelated_attested_root_are_not_derivation():
    events = trace(omit_copy=True, output_lineage="root")
    events.insert(0, event("read", "data_access", 5, operation_type="file_read",
                           read_refs=[SOURCE], data_objects={SOURCE: metadata(SOURCE)}))
    d = decide(build(events))
    assert d.decision is PolicyAction.ALLOW
    assert not d.matching_paths


@pytest.mark.parametrize("edges", [
    (EdgeType.INPUT_TO,), (EdgeType.WRITE, EdgeType.INPUT_TO),
    (EdgeType.READ, EdgeType.INPUT_TO), (EdgeType.READ, EdgeType.WRITE),
])
def test_excluded_flow_edges_give_only_scoped_no_match(edges):
    d = decide(allowed_edge_types=edges)
    assert d.decision is PolicyAction.ALLOW
    assert d.search_status == "no_match_within_bounds"


@pytest.mark.parametrize("edge_type", [
    EdgeType.PRODUCES, EdgeType.INVOKES, EdgeType.TRIGGERS,
    EdgeType.AUTHORIZES, EdgeType.ACCESSES, EdgeType.PAYLOAD_OF,
])
def test_non_flow_edge_configuration_rejected(edge_type):
    with pytest.raises(ValueError, match="supported data-flow"):
        decide(allowed_edge_types=(edge_type,))


@pytest.mark.parametrize("copy_time,expected", [
    (7.999, PolicyAction.ALLOW), (8, PolicyAction.DENY),
    (10, PolicyAction.DENY), (10.001, PolicyAction.ALLOW),
])
def test_closed_seconds_window_is_anchored_at_proposed_call(copy_time, expected):
    d = decide(build(trace(copy_time=copy_time)), time_window=2, time_window_unit="seconds")
    assert d.decision is expected
    assert d.evidence_status == "complete"


def test_zero_window_includes_only_sink_timestamp():
    assert decide(build(trace(copy_time=10)), time_window=0,
                  time_window_unit="seconds").decision is PolicyAction.DENY


def test_time_window_requires_units():
    with pytest.raises(ValueError, match="explicit time_window_unit"):
        decide(time_window=2)


@pytest.mark.parametrize("clock", ["ordinal", None])
def test_seconds_rejects_ordinal_or_unknown_sink_clock(clock):
    with pytest.raises(ValueError, match="sink clock"):
        decide(build(trace(clock=clock)), time_window=2, time_window_unit="seconds")


def test_edge_clock_domains_must_match_sink():
    store = clone(build(trace()), edge_transform=lambda e: e.model_copy(update={
        "attributes": {**e.attributes, "clock_domain": "other-clock"},
    }) if e.edge_type is EdgeType.READ else e)
    with pytest.raises(ValueError, match="edge clock"):
        decide(store, time_window=2, time_window_unit="seconds")


def test_ordinal_window_is_explicit_not_seconds():
    d = decide(build(trace(clock="ordinal")), time_window=2, time_window_unit="ordinal")
    assert d.decision is PolicyAction.DENY


def test_parallel_edges_and_multiple_paths_retain_identity():
    events = trace()
    another = {**events[0], "event_id": "copy-2"}
    store = build([events[0], another, events[1]])
    read = next(e for e in store.edges() if e.edge_type is EdgeType.READ)
    store.add_edge(read.model_copy(update={"edge_id": "parallel-read"}))
    d = decide(store)
    assert len(d.matching_paths) == 3
    assert len({p.edge_ids for p in d.matching_paths}) == 3
    assert any("parallel-read" in p.edge_ids for p in d.matching_paths)


def test_search_budget_is_incomplete_not_safe():
    d = decide(max_search_states=1)
    assert d.decision is PolicyAction.REQUEST_APPROVAL
    assert "search_state_budget_exhausted" in d.incomplete_reasons


def test_hash_mismatch_or_non_runtime_read_is_incomplete():
    for store in [
        clone(build(trace()), node_transform=lambda n: n.model_copy(update={
            "content_hash": "sha256:different",
        }) if n.node_id == "data:" + OUTPUT else n),
        clone(build(trace()), edge_transform=lambda e: e.model_copy(update={
            "derivation": "explicit_data_reference",
        }) if e.edge_type is EdgeType.READ else e),
    ]:
        d = decide(store)
        assert d.decision is PolicyAction.REQUEST_APPROVAL
        assert not d.matching_paths


def test_direct_path_is_supported_in_path_mode_without_prior_read():
    events = trace(omit_copy=True)
    events[0]["attributes"]["input_refs"] = [SOURCE]
    events[0]["attributes"]["argument_summary"]["outgoing_attachment_refs"] = [SOURCE]
    d = decide(build(events), max_hops=1)
    assert d.decision is PolicyAction.DENY
    assert len(d.matching_paths[0].edge_ids) == 1


@pytest.mark.parametrize("invalid_window", [float("inf"), float("nan"), -1])
def test_windows_must_be_finite_and_nonnegative(invalid_window):
    with pytest.raises(ValueError):
        decide(time_window=invalid_window, time_window_unit="seconds")


def test_configuration_cannot_execute_on_incomplete_path_evidence():
    from causalguard.policy import PolicyDefinition
    data = protected_file_external_email_policy(path_mode=True).model_dump()
    data["action"]["on_missing_evidence"] = "allow"
    with pytest.raises(ValueError, match="non-executing"):
        PolicyDefinition.model_validate(data)


def test_two_copies_require_five_hops():
    events = trace()
    middle = "resource:middle"
    first, send = events
    first["attributes"]["write_refs"] = [middle]
    first["attributes"]["data_objects"][middle] = metadata(middle, lineage="copy_output")
    second = event("copy-2", "system_operation", 9,
                   operation_type="controlled_file_copy", data_flow_semantics="identity_copy_v1",
                   read_refs=[middle], write_refs=[OUTPUT], data_derivation="runtime_observation",
                   data_objects=first["attributes"]["data_objects"])
    store = build([first, second, send])
    assert decide(store, max_hops=4).search_status == "no_match_within_bounds"
    d = decide(store, max_hops=5)
    assert d.decision is PolicyAction.DENY
    assert len(d.matching_paths[0].edge_ids) == 5

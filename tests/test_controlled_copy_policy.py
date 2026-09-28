from __future__ import annotations

import pytest
pytest.importorskip("agentdojo")

from scripts.run_controlled_copy_policy import run
from causalguard.policy import PolicyAction
from causalguard.schema.edges import EdgeType
from causalguard.schema.nodes import DataObjectNode, SystemOperationNode


def assert_no_email_effects(result):
    assert result["email_count_delta"] == 0
    store = result["final_store"]
    assert not any(isinstance(n, SystemOperationNode) and n.operation_type == "email_send"
                   for n in store.nodes())
    assert not any(isinstance(n, DataObjectNode) and n.object_kind == "email"
                   for n in store.nodes())
    assert not any(e.edge_type is EdgeType.PAYLOAD_OF for e in store.edges())
    # The controlled copy's WRITE is committed, but no email WRITE is.
    for edge in store.edges():
        if edge.edge_type is EdgeType.WRITE:
            assert store.get_node(edge.source_id).operation_type == "controlled_file_copy"


def test_real_copy_execution_and_same_facts_direct_comparison():
    result = run()
    assert result["copy_correct"]
    assert result["source_resource"] != result["output_resource"]
    d = result["decision"]
    assert d.decision is PolicyAction.DENY
    assert result["direct_decision_same_graph"].decision is PolicyAction.ALLOW
    assert d.protected_resource_ids == (result["source_resource"],)
    path, = d.matching_paths
    store = result["evaluation_store"]
    source, operation, output, sink = [store.get_node(n) for n in path.node_ids]
    assert source.content_hash == output.content_hash
    assert source.sensitivity == output.sensitivity  # No propagated label hidden from baseline.
    assert output.resource_id not in d.protected_resource_ids
    assert operation.data_flow_semantics == "identity_copy_v1"
    assert [store.get_edge(e).edge_type for e in path.edge_ids] == [
        EdgeType.READ, EdgeType.WRITE, EdgeType.INPUT_TO,
    ]
    assert_no_email_effects(result)


@pytest.mark.parametrize("options,expected", [
    ({"source_id": "7"}, PolicyAction.ALLOW),
    ({"trusted_domains": ("example.org",)}, PolicyAction.ALLOW),
    ({"approval_scope": "output"}, PolicyAction.ALLOW_WITH_AUDIT),
])
def test_permitted_controlled_copies_execute(options, expected):
    result = run(**options)
    assert result["copy_correct"]
    assert result["decision"].decision is expected
    assert result["email_count_delta"] == 1


@pytest.mark.parametrize("options,expected", [
    ({"approval_scope": "source"}, PolicyAction.DENY),
    ({"omit_copy_provenance": True}, PolicyAction.REQUEST_APPROVAL),
    ({"trusted_domains": ("example.org",), "cc": ("outside@untrusted.org",)}, PolicyAction.DENY),
    ({"approval_scope": "output", "cc": ("outside@untrusted.org",)}, PolicyAction.DENY),
    ({"max_search_states": 1}, PolicyAction.REQUEST_APPROVAL),
])
def test_denied_or_incomplete_controlled_sends_have_no_email_effects(options, expected):
    result = run(**options)
    assert result["copy_correct"]
    assert result["decision"].decision is expected
    assert_no_email_effects(result)


def test_seconds_window_rejected_on_actual_mapper_ordinals():
    with pytest.raises(ValueError, match="sink clock"):
        run(time_window=30, time_window_unit="seconds")


def test_explicit_ordinal_window_on_real_mapper():
    result = run(time_window=30, time_window_unit="ordinal")
    assert result["decision"].decision is PolicyAction.DENY
    assert_no_email_effects(result)


def test_direct_checker_is_not_weakened_and_can_execute_controlled_baseline():
    result = run(path_mode=False)
    assert result["decision"].decision is PolicyAction.ALLOW
    assert result["email_count_delta"] == 1

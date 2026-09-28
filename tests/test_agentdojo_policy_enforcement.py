from __future__ import annotations

import json
from collections.abc import Sequence

import pytest

pytest.importorskip("agentdojo")

from agentdojo.agent_pipeline.agent_pipeline import AgentPipeline
from agentdojo.agent_pipeline.base_pipeline_element import BasePipelineElement
from agentdojo.agent_pipeline.basic_elements import InitQuery, SystemMessage
from agentdojo.agent_pipeline.tool_execution import ToolsExecutionLoop, ToolsExecutor
from agentdojo.functions_runtime import EmptyEnv, Env, FunctionCall, FunctionsRuntime
from agentdojo.task_suite.load_suites import get_suite
from agentdojo.types import (
    ChatAssistantMessage,
    ChatMessage,
    text_content_block_from_string,
)

from causalguard.graph import GraphStore
from causalguard.integrations.agentdojo import (
    AgentDojoCollector,
    AgentDojoPolicyEnforcer,
    AgentDojoRuntimeObserver,
    AgentDojoTraceMapper,
    PolicyEnforcingToolsExecutor,
    WorkspaceReadSendExtractor,
    install_policy_enforcement,
)
from causalguard.policy import PolicyAction, protected_file_external_email_policy
from causalguard.schema.events import NormalizedEvent
from causalguard.schema.edges import EdgeType


PROTECTED = "agentdojo:workspace:file:19"
SESSION_ID = "workspace-policy-enforcement"


class _PlannedLLM(BasePipelineElement):
    name = "policy-enforcement-planned"

    def __init__(self, calls: Sequence[FunctionCall]) -> None:
        self.calls = tuple(calls)

    def query(
        self,
        query: str,
        runtime: FunctionsRuntime,
        env: Env = EmptyEnv(),
        messages: Sequence[ChatMessage] = [],
        extra_args: dict = {},
    ) -> tuple[str, FunctionsRuntime, Env, Sequence[ChatMessage], dict]:
        completed = sum(message["role"] == "tool" for message in messages)
        if completed < len(self.calls):
            message = ChatAssistantMessage(
                role="assistant",
                content=None,
                tool_calls=[self.calls[completed]],
            )
        else:
            message = ChatAssistantMessage(
                role="assistant",
                content=[text_content_block_from_string("done")],
                tool_calls=None,
            )
        return query, runtime, env, [*messages, message], extra_args


def _run_policy_flow(
    *,
    trusted_domains: tuple[str, ...] = (),
    include_protected_attachment: bool = True,
    retry_without_attachment: bool = False,
    attachment_override: object | None = None,
    without_read: bool = False,
    send_overrides: dict | None = None,
    approval_events: Sequence[NormalizedEvent] = (),
    extra_policies: tuple = (),
    with_extractor: bool = True,
) -> tuple[GraphStore, AgentDojoPolicyEnforcer, int, int]:
    suite = get_suite("v1.2.2", "workspace")
    task = suite.get_user_task_by_id("user_task_33")
    base_environment = suite.load_and_inject_default_environment({})
    environment = task.init_environment(base_environment)
    ground_truth = task.ground_truth(environment.model_copy(deep=True))
    calls = [
        FunctionCall(
            id=f"policy-{index}",
            function=call.function,
            args=dict(call.args),
        )
        for index, call in enumerate(ground_truth)
    ]
    if not include_protected_attachment:
        calls[-1].args["attachments"] = []
    if attachment_override is not None:
        calls[-1].args["attachments"] = attachment_override
    if without_read:
        calls = [calls[-1]]
    if send_overrides:
        calls[-1].args.update(send_overrides)
    if retry_without_attachment:
        calls.append(
            FunctionCall(
                id="policy-revised-send",
                function=calls[-1].function,
                args={**calls[-1].args, "attachments": []},
            )
        )

    observer = AgentDojoRuntimeObserver(
        [WorkspaceReadSendExtractor()] if with_extractor else []
    )
    mapper = AgentDojoTraceMapper(
        session_id=SESSION_ID,
        model_name=_PlannedLLM.name,
    )
    policy = protected_file_external_email_policy(
        protected_resource_ids=(PROTECTED,),
        trusted_domains=trusted_domains,
    )
    enforcer = AgentDojoPolicyEnforcer(
        [policy, *extra_policies],
        approval_events=approval_events,
        mapper=mapper,
        observer=observer,
    )
    llm = _PlannedLLM(calls)
    pipeline = AgentPipeline(
        [
            SystemMessage("Complete the task."),
            InitQuery(),
            llm,
            ToolsExecutionLoop([PolicyEnforcingToolsExecutor(enforcer), llm]),
        ]
    )
    pipeline.name = llm.name
    collector = AgentDojoCollector(
        pipeline,
        session_id=SESSION_ID,
        model_name=llm.name,
        runtime_observer=observer,
        policy_enforcer=enforcer,
    )
    before_count = len(environment.inbox.emails)
    _, _, final_environment, _, _ = collector.query(
        task.PROMPT,
        FunctionsRuntime(suite.tools),
        environment,
    )
    after_count = len(final_environment.inbox.emails)
    return collector.build_graph(), enforcer, before_count, after_count


def test_protected_external_send_is_denied_before_email_mutation() -> None:
    store, enforcer, before_count, after_count = _run_policy_flow()

    assert after_count == before_count
    assert len(enforcer.decisions) == 1
    decision = enforcer.decisions[0]
    assert decision.decision is PolicyAction.DENY
    assert decision.protected_resource_ids == (PROTECTED,)
    assert not _successful_send_nodes(store)
    assert not any(edge.edge_type is EdgeType.WRITE for edge in store.edges())
    assert not any(edge.edge_type is EdgeType.PAYLOAD_OF for edge in store.edges())
    assert not any(
        node.node_type.value == "data_object" and node.object_kind == "email"
        for node in store.nodes()
    )
    send_tool = next(
        node
        for node in store.nodes()
        if node.node_type.value == "tool_call" and node.tool_name == "send_email"
    )
    protected_node = next(
        node
        for node in store.nodes()
        if node.node_type.value == "data_object"
        and node.resource_id == PROTECTED
    )
    assert send_tool.destination == "mailto:john.mitchell@gmail.com"
    assert any(
        edge.source_id == protected_node.node_id
        and edge.target_id == send_tool.node_id
        and edge.edge_type is EdgeType.INPUT_TO
        for edge in store.edges()
    )
    assert set(decision.evidence_node_ids) == {protected_node.node_id, send_tool.node_id}
    assert len(decision.evidence_edge_ids) == 1
    edge = enforcer.last_evaluation_store.get_edge(decision.evidence_edge_ids[0])
    assert (edge.source_id, edge.target_id, edge.edge_type) == (
        protected_node.node_id, send_tool.node_id, EdgeType.INPUT_TO,
    )


def test_protected_send_to_trusted_domain_executes_normally() -> None:
    store, enforcer, before_count, after_count = _run_policy_flow(
        trusted_domains=("gmail.com",),
    )

    assert after_count == before_count + 1
    assert enforcer.decisions[0].decision is PolicyAction.ALLOW
    assert enforcer.decisions[0].exception_status == "trusted_destination"
    assert _successful_send_nodes(store)
    assert any(edge.edge_type is EdgeType.WRITE for edge in store.edges())


def test_prior_protected_read_without_outgoing_attachment_is_allowed() -> None:
    store, enforcer, before_count, after_count = _run_policy_flow(
        include_protected_attachment=False,
    )

    assert after_count == before_count + 1
    assert enforcer.decisions[0].decision is PolicyAction.ALLOW
    assert enforcer.decisions[0].protected_resource_ids == ()
    assert any(edge.edge_type is EdgeType.READ for edge in store.edges())
    assert _successful_send_nodes(store)
    protected_nodes = {
        node.node_id
        for node in store.nodes()
        if node.node_type.value == "data_object"
        and node.resource_id == PROTECTED
    }
    assert not any(
        edge.source_id in protected_nodes
        and edge.edge_type is EdgeType.PAYLOAD_OF
        for edge in store.edges()
    )


def test_policy_decision_and_denied_exports_do_not_leak_raw_content(tmp_path) -> None:
    store, enforcer, _, _ = _run_policy_flow()
    decision_json = enforcer.decisions[0].model_dump_json()
    graph_json = store.to_json()
    graph_dot = store.to_dot()
    combined = decision_json + graph_json + graph_dot

    assert "Summary of the client meeting" not in combined
    assert "2024-06-01" not in combined
    assert "client-meeting-minutes.docx" not in combined
    (tmp_path / "decision.json").write_text(
        json.dumps(json.loads(decision_json), indent=2) + "\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "attachment_override",
    [["missing-file-id"], "missing-file-id"],
)
def test_unresolved_attachment_is_held_before_email_mutation(attachment_override) -> None:
    store, enforcer, before_count, after_count = _run_policy_flow(
        attachment_override=attachment_override,
    )

    assert after_count == before_count
    assert len(enforcer.decisions) == 1
    decision = enforcer.decisions[0]
    assert decision.decision is PolicyAction.REQUEST_APPROVAL
    assert decision.evidence_status == "missing"
    assert not _successful_send_nodes(store)
    assert not any(edge.edge_type is EdgeType.WRITE for edge in store.edges())
    assert not any(edge.edge_type is EdgeType.PAYLOAD_OF for edge in store.edges())


def test_revised_send_after_denial_receives_a_fresh_decision() -> None:
    store, enforcer, before_count, after_count = _run_policy_flow(
        retry_without_attachment=True,
    )

    assert after_count == before_count + 1
    assert [item.decision for item in enforcer.decisions] == [
        PolicyAction.DENY,
        PolicyAction.ALLOW,
    ]
    assert enforcer.decisions[0].protected_resource_ids == (PROTECTED,)
    assert enforcer.decisions[1].protected_resource_ids == ()
    assert (
        enforcer.decisions[0].triggering_tool_call_id
        != enforcer.decisions[1].triggering_tool_call_id
    )
    assert len(_successful_send_nodes(store)) == 1


def test_standard_agentdojo_executor_can_be_replaced_for_real_llm_pipeline() -> None:
    observer = AgentDojoRuntimeObserver([WorkspaceReadSendExtractor()])
    mapper = AgentDojoTraceMapper(session_id="install-policy")
    enforcer = AgentDojoPolicyEnforcer(
        [
            protected_file_external_email_policy(
                protected_resource_ids=(PROTECTED,),
            )
        ],
        mapper=mapper,
        observer=observer,
    )
    llm = _PlannedLLM(())
    loop = ToolsExecutionLoop([ToolsExecutor(), llm])
    pipeline = AgentPipeline([loop])

    installed = install_policy_enforcement(pipeline, enforcer)

    assert installed is pipeline
    assert isinstance(loop.elements[0], PolicyEnforcingToolsExecutor)
    assert loop.elements[0].enforcer is enforcer


def _successful_send_nodes(store: GraphStore) -> list[object]:
    return [
        node
        for node in store.nodes()
        if node.node_type.value == "system_operation"
        and node.operation_type == "email_send"
    ]


def _assert_no_send_effects(store, before_count, after_count):
    assert before_count == after_count
    assert not _successful_send_nodes(store)
    assert not any(edge.edge_type in {EdgeType.WRITE, EdgeType.PAYLOAD_OF}
                   for edge in store.edges())
    assert not any(node.node_type.value == "data_object" and node.object_kind == "email"
                   for node in store.nodes())


def test_attachment_without_preceding_read_is_enforced():
    store, enforcer, before, after = _run_policy_flow(without_read=True)
    assert enforcer.decisions[0].decision is PolicyAction.DENY
    assert not any(edge.edge_type is EdgeType.READ for edge in store.edges())
    assert len(enforcer.decisions[0].evidence_edge_ids) == 1
    _assert_no_send_effects(store, before, after)


@pytest.mark.parametrize("field", ["recipients", "cc", "bcc"])
def test_untrusted_recipient_in_any_scope_is_denied(field):
    addresses = ["outsider@example.org"]
    if field == "recipients":
        addresses.insert(0, "john.mitchell@gmail.com")
    store, enforcer, before, after = _run_policy_flow(
        trusted_domains=("gmail.com",), send_overrides={field: addresses},
    )
    decision = enforcer.decisions[0]
    assert decision.decision is PolicyAction.DENY
    assert "outsider@example.org" in decision.recipient_addresses
    _assert_no_send_effects(store, before, after)


def test_all_to_cc_bcc_recipients_trusted():
    store, enforcer, before, after = _run_policy_flow(
        trusted_domains=("gmail.com",),
        send_overrides={"cc": ["cc@gmail.com"], "bcc": ["bcc@gmail.com"]},
    )
    assert enforcer.decisions[0].decision is PolicyAction.ALLOW
    assert set(enforcer.decisions[0].recipient_addresses) == {
        "john.mitchell@gmail.com", "cc@gmail.com", "bcc@gmail.com",
    }
    assert after == before + 1
    assert _successful_send_nodes(store)


@pytest.mark.parametrize("overrides", [
    {"recipients": []}, {"recipients": ["invalid"]},
    {"cc": "outsider@example.org"}, {"bcc": [""]}, {"cc": [None]},
])
def test_unresolved_destination_holds_without_mutation(overrides):
    store, enforcer, before, after = _run_policy_flow(
        trusted_domains=("gmail.com",), send_overrides=overrides,
    )
    decision = enforcer.decisions[0]
    assert decision.decision is PolicyAction.REQUEST_APPROVAL
    assert decision.destination_classification == "unresolved"
    _assert_no_send_effects(store, before, after)


@pytest.mark.parametrize("attachments", [None, []])
def test_explicit_no_attachments_is_not_unresolved(attachments):
    store, enforcer, before, after = _run_policy_flow(
        send_overrides={"attachments": attachments}, without_read=True,
    )
    assert enforcer.decisions[0].decision is PolicyAction.ALLOW
    assert enforcer.decisions[0].evidence_status == "complete"
    assert after == before + 1


def test_public_attachment_is_permitted():
    store, enforcer, before, after = _run_policy_flow(
        attachment_override=[{"type": "file", "file_id": "7"}], without_read=True,
    )
    assert enforcer.decisions[0].decision is PolicyAction.ALLOW
    assert enforcer.decisions[0].protected_resource_ids == ()
    assert after == before + 1


def test_unknown_attachment_holds_even_with_trusted_destination():
    store, enforcer, before, after = _run_policy_flow(
        trusted_domains=("gmail.com",), attachment_override=["missing-file-id"],
    )
    assert enforcer.decisions[0].decision is PolicyAction.REQUEST_APPROVAL
    _assert_no_send_effects(store, before, after)


@pytest.mark.parametrize("overrides,expected", [
    ({}, PolicyAction.ALLOW_WITH_AUDIT),
    ({"expiration": 0.0}, PolicyAction.DENY),
    ({"resource_scope": "agentdojo:workspace:file:7"}, PolicyAction.DENY),
    ({"destination_scope": "mailto:other@gmail.com"}, PolicyAction.DENY),
    ({"action_class": "upload"}, PolicyAction.DENY),
])
def test_approval_exception_at_real_runtime_gate(overrides, expected):
    approval = NormalizedEvent.model_validate({
        "event_id": "exact-approval", "event_type": "human_approval",
        "timestamp": 0.0, "agent_id": "human", "session_id": SESSION_ID,
        "causal_context_id": SESSION_ID, "parent_event_id": None,
        "attributes": {
            "approver_id": "user-1", "action_class": "send_email",
            "resource_scope": PROTECTED,
            "destination_scope": "mailto:john.mitchell@gmail.com",
            "expiration": 1e20, **overrides,
        },
    })
    store, enforcer, before, after = _run_policy_flow(
        approval_events=[approval], without_read=True,
    )
    decision = enforcer.decisions[0]
    assert decision.decision is expected
    if expected is PolicyAction.ALLOW_WITH_AUDIT:
        assert after == before + 1
        assert decision.exception_evidence_node_ids
        assert not set(decision.exception_evidence_node_ids) & set(decision.evidence_node_ids)
    else:
        _assert_no_send_effects(store, before, after)


def test_attachment_allow_does_not_override_another_policy():
    second = protected_file_external_email_policy(
        protected_resource_ids=(PROTECTED,),
    ).model_copy(update={"policy_id": "other-policy"})
    store, enforcer, before, after = _run_policy_flow(
        trusted_domains=("gmail.com",), extra_policies=(second,),
    )
    assert [d.decision for d in enforcer.decisions] == [PolicyAction.ALLOW, PolicyAction.DENY]
    _assert_no_send_effects(store, before, after)


def test_missing_attachment_adapter_evidence_holds():
    store, enforcer, before, after = _run_policy_flow(
        without_read=True, with_extractor=False,
    )
    assert enforcer.decisions[0].decision is PolicyAction.REQUEST_APPROVAL
    _assert_no_send_effects(store, before, after)


def test_partially_unresolved_attachments_hold_before_mutation():
    store, enforcer, before, after = _run_policy_flow(
        attachment_override=[
            {"type": "file", "file_id": "19"},
            {"type": "file", "file_id": "missing"},
        ],
    )
    decision = enforcer.decisions[0]
    assert decision.decision is PolicyAction.REQUEST_APPROVAL
    assert decision.protected_resource_ids == (PROTECTED,)
    assert decision.evidence_status == "missing"
    _assert_no_send_effects(store, before, after)


def test_to_only_approval_cannot_authorize_added_cc():
    approval = NormalizedEvent.model_validate({
        "event_id": "to-only-approval", "event_type": "human_approval",
        "timestamp": 0.0, "agent_id": "human", "session_id": SESSION_ID,
        "causal_context_id": SESSION_ID, "parent_event_id": None,
        "attributes": {
            "approver_id": "user-1", "action_class": "send_email",
            "resource_scope": PROTECTED,
            "destination_scope": "mailto:john.mitchell@gmail.com",
            "expiration": 1e20,
        },
    })
    store, enforcer, before, after = _run_policy_flow(
        approval_events=[approval], send_overrides={"cc": ["outsider@example.org"]},
    )
    assert enforcer.decisions[0].decision is PolicyAction.DENY
    assert enforcer.decisions[0].exception_status == "none"
    _assert_no_send_effects(store, before, after)


def test_model_supplied_policy_claims_do_not_override_adapter_evidence():
    store, enforcer, before, after = _run_policy_flow(send_overrides={
        "outgoing_attachment_refs": [], "sensitivity": "public",
        "approved": True, "trusted_destination": True,
    })
    assert enforcer.decisions[0].decision is PolicyAction.DENY
    assert enforcer.decisions[0].protected_resource_ids == (PROTECTED,)
    _assert_no_send_effects(store, before, after)

"""Controlled Phase 2 demonstration, not a benchmark or model-inference run.

Run: .venv/bin/python -m scripts.run_controlled_copy_policy
No artifact files or raw contents are exported.
"""
from __future__ import annotations

import json

from agentdojo.functions_runtime import FunctionCall, FunctionsRuntime
from agentdojo.task_suite.load_suites import get_suite
from agentdojo.types import ChatAssistantMessage

from causalguard.graph import GraphBuilder, GraphStore
from causalguard.integrations.agentdojo import (
    AgentDojoPolicyEnforcer, AgentDojoRuntimeObserver, AgentDojoTraceMapper,
    PolicyEnforcingToolsExecutor,
)
from causalguard.integrations.agentdojo.extractors.controlled_copy import (
    ControlledCopyExtractor, controlled_copy_file,
)
from causalguard.integrations.agentdojo.runtime import ObservingFunctionsRuntime
from causalguard.policy import PolicyEngine, protected_file_external_email_policy
from causalguard.schema.events import NormalizedEvent

PROTECTED = "agentdojo:workspace:file:19"


def run(*, source_id="19", path_mode=True, omit_copy_provenance=False,
        trusted_domains=(), cc=(), approval_scope=None, **policy_kwargs):
    suite = get_suite("v1.2.2", "workspace")
    environment = suite.load_and_inject_default_environment({})
    initial_ids = set(environment.cloud_drive.files)

    class FaultInjectionExtractor(ControlledCopyExtractor):
        def extract(self, context):
            evidence = super().extract(context)
            if omit_copy_provenance and context.function_name == "controlled_copy_file":
                return ()
            return evidence

    extractor = FaultInjectionExtractor(environment)
    observer = AgentDojoRuntimeObserver([extractor])
    mapper = AgentDojoTraceMapper(session_id="controlled-copy")
    policy = protected_file_external_email_policy(
        protected_resource_ids=(PROTECTED,), path_mode=path_mode,
        trusted_domains=trusted_domains, **policy_kwargs,
    )
    enforcer = AgentDojoPolicyEnforcer([policy], mapper=mapper, observer=observer)
    executor = PolicyEnforcingToolsExecutor(enforcer)
    delegate = FunctionsRuntime(suite.tools)
    delegate.register_function(controlled_copy_file)
    runtime = ObservingFunctionsRuntime(delegate, observer)
    copy_call = FunctionCall(id="copy", function="controlled_copy_file",
                             args={"source_id": source_id})
    messages = [ChatAssistantMessage(role="assistant", content=None, tool_calls=[copy_call])]
    _, _, environment, messages, _ = executor.query("Controlled copy test", runtime, environment, messages)
    # Independently inspect effects, not the extractor's own correctness claim.
    outputs = [f for f in environment.cloud_drive.files.values() if f.id_ not in initial_ids]
    if len(outputs) != 1:
        raise AssertionError("controlled copy did not produce exactly one new resource")
    output = outputs[0]
    output_id = str(output.id_)
    copy_correct = (output.content == environment.cloud_drive.files[source_id].content
                    and output_id != source_id)
    if not copy_correct:
        raise AssertionError("independent in-memory copy verification failed")
    if approval_scope is not None:
        resource = output_id if approval_scope == "output" else source_id
        enforcer.approval_events = (NormalizedEvent.model_validate({
            "event_id": "controlled-approval", "event_type": "human_approval",
            "timestamp": 0, "agent_id": "human", "session_id": "controlled-copy",
            "causal_context_id": "controlled-copy", "parent_event_id": None,
            "attributes": {"approver_id": "user-1", "action_class": "send_email",
                           "resource_scope": "agentdojo:workspace:file:" + resource,
                           "destination_scope": "mailto:outside@example.org",
                           "expiration": 1000},
        }),)
    send_call = FunctionCall(id="send", function="send_email", args={
        "recipients": ["outside@example.org"], "cc": list(cc),
        "subject": "Controlled export", "body": "Attached export.",
        "attachments": [{"type": "file", "file_id": output_id}],
    })
    messages = [*messages, ChatAssistantMessage(role="assistant", content=None, tool_calls=[send_call])]
    before = len(environment.inbox.emails)
    _, _, environment, messages, _ = executor.query("Controlled copy test", runtime, environment, messages)
    decision = enforcer.decisions[-1]
    evaluation_store = enforcer.last_evaluation_store
    # Same graph, configuration, attachment facts and labels; only selector differs.
    direct_policy = policy.model_copy(update={
        "select": policy.select.model_copy(update={"kind": "bounded_outgoing_provenance"}),
    })
    direct = PolicyEngine().evaluate(direct_policy, evaluation_store, decision.triggering_tool_call_id)
    final_store = GraphStore()
    GraphBuilder(final_store).process_trace(mapper.map_trace(
        "Controlled copy test", messages, observer.observations, observer.proposals,
    ))
    return {
        "copy_correct": copy_correct, "source_resource": "agentdojo:workspace:file:" + source_id,
        "output_resource": "agentdojo:workspace:file:" + output_id,
        "email_count_delta": len(environment.inbox.emails) - before,
        "decision": decision, "direct_decision_same_graph": direct,
        "evaluation_store": evaluation_store, "final_store": final_store,
    }


if __name__ == "__main__":
    result = run()
    print(json.dumps({
        k: (v.model_dump(mode="json") if hasattr(v, "model_dump") else v)
        for k, v in result.items() if not k.endswith("store")
    }, indent=2))

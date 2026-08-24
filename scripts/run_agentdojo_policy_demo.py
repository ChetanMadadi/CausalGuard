"""Demonstrate pre-mutation CausalGuard enforcement in AgentDojo."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from agentdojo.agent_pipeline.agent_pipeline import AgentPipeline
from agentdojo.agent_pipeline.base_pipeline_element import BasePipelineElement
from agentdojo.agent_pipeline.basic_elements import InitQuery, SystemMessage
from agentdojo.agent_pipeline.tool_execution import ToolsExecutionLoop
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
)
from causalguard.policy import PolicyDecision, protected_file_external_email_policy
from causalguard.schema.edges import EdgeType


BENCHMARK_VERSION = "v1.2.2"
SUITE_NAME = "workspace"
TASK_ID = "user_task_33"
SESSION_ID = "workspace-user_task_33-policy-deny"
MODEL_NAME = "causalguard-policy-demo"
PROTECTED_RESOURCE = "agentdojo:workspace:file:19"


class PlannedTaskLLM(BasePipelineElement):
    """Replay the task's real calls through AgentDojo's execution loop."""

    name = MODEL_NAME

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
                content=[text_content_block_from_string("Task finished.")],
                tool_calls=None,
            )
        return query, runtime, env, [*messages, message], extra_args


def run(output_dir: Path) -> tuple[GraphStore, PolicyDecision, bool, bool]:
    suite = get_suite(BENCHMARK_VERSION, SUITE_NAME)
    task = suite.get_user_task_by_id(TASK_ID)
    environment = task.init_environment(
        suite.load_and_inject_default_environment({})
    )
    source_file = environment.cloud_drive.files["19"]
    ground_truth = task.ground_truth(environment.model_copy(deep=True))
    calls = [
        FunctionCall(
            id=f"policy-demo-{index}",
            function=call.function,
            args=dict(call.args),
        )
        for index, call in enumerate(ground_truth)
    ]

    observer = AgentDojoRuntimeObserver([WorkspaceReadSendExtractor()])
    mapper = AgentDojoTraceMapper(
        session_id=SESSION_ID,
        model_name=MODEL_NAME,
    )
    policy = protected_file_external_email_policy(
        protected_resource_ids=(PROTECTED_RESOURCE,),
        trusted_recipients=(),
        trusted_domains=(),
        max_provenance_depth=6,
    )
    enforcer = AgentDojoPolicyEnforcer(
        [policy],
        mapper=mapper,
        observer=observer,
    )
    llm = PlannedTaskLLM(calls)
    pipeline = AgentPipeline(
        [
            SystemMessage("Complete the task with the available tools."),
            InitQuery(),
            llm,
            ToolsExecutionLoop([PolicyEnforcingToolsExecutor(enforcer), llm]),
        ]
    )
    pipeline.name = MODEL_NAME
    collector = AgentDojoCollector(
        pipeline,
        session_id=SESSION_ID,
        model_name=MODEL_NAME,
        runtime_observer=observer,
        policy_enforcer=enforcer,
    )

    email_count_before = len(environment.inbox.emails)
    _, _, final_environment, _, _ = collector.query(
        task.PROMPT,
        FunctionsRuntime(suite.tools),
        environment,
    )
    email_count_after = len(final_environment.inbox.emails)
    store = collector.write_outputs(output_dir)
    decision = enforcer.decisions[0]
    (output_dir / "decision.json").write_text(
        decision.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )

    mutation_suppressed = email_count_after == email_count_before
    no_success_provenance = not any(
        (
            node.node_type.value == "system_operation"
            and node.operation_type == "email_send"
        )
        or (node.node_type.value == "data_object" and node.object_kind == "email")
        for node in store.nodes()
    ) and not any(edge.edge_type is EdgeType.WRITE for edge in store.edges())
    privacy_safe = _privacy_safe(
        output_dir,
        forbidden=(
            task.PROMPT,
            source_file.filename,
            source_file.content,
            str(calls[-1].args["subject"]),
            str(calls[-1].args["body"]),
        ),
    )
    (output_dir / "policy_report.md").write_text(
        _report(
            store,
            decision,
            mutation_suppressed=mutation_suppressed,
            no_success_provenance=no_success_provenance,
            privacy_safe=privacy_safe,
            observation_count=len(observer.observations),
        ),
        encoding="utf-8",
    )
    return store, decision, mutation_suppressed, privacy_safe


def _privacy_safe(output_dir: Path, *, forbidden: Sequence[str]) -> bool:
    exported = "\n".join(
        (output_dir / filename).read_text(encoding="utf-8")
        for filename in ("trace.jsonl", "graph.json", "graph.dot", "decision.json")
    )
    return not any(value and value in exported for value in forbidden)


def _report(
    store: GraphStore,
    decision: PolicyDecision,
    *,
    mutation_suppressed: bool,
    no_success_provenance: bool,
    privacy_safe: bool,
    observation_count: int,
) -> str:
    return f"""# AgentDojo pre-mutation policy enforcement demo

- Benchmark: {BENCHMARK_VERSION} / {SUITE_NAME} / {TASK_ID}
- Policy: {decision.policy_id}
- Decision: {decision.decision.value}
- Destination classification: {decision.destination_classification}
- Exception status: {decision.exception_status}
- Protected resource matches: {len(decision.protected_resource_ids)}
- Runtime observations: {observation_count}
- Email mutation suppressed: {mutation_suppressed}
- No successful send provenance fabricated: {no_success_provenance}
- Graph acyclic: {store.is_acyclic()}
- Privacy export check passed: {privacy_safe}
- Evidence nodes selected: {len(decision.evidence_node_ids)}
- Evidence edges selected: {len(decision.evidence_edge_ids)}

The file-read call executed normally. The proposed `send_email` call was then
enriched with exact pre-execution attachment, hashed content, and destination
evidence. The bounded policy decision ran before `FunctionsRuntime` and denied
the call, so AgentDojo's email state was not mutated. The proposed ToolCall and
its privacy-safe input provenance remain in the graph, while no successful
`email_send` SystemOperation, created-email DataObject, or write edge exists.
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("outputs/agentdojo/policy_enforcement"),
    )
    args = parser.parse_args()
    store, decision, mutation_suppressed, privacy_safe = run(args.output_dir)
    print(
        json.dumps(
            {
                "decision": decision.decision.value,
                "edges": store.edge_count,
                "mutation_suppressed": mutation_suppressed,
                "nodes": store.node_count,
                "output_dir": str(args.output_dir),
                "privacy_safe": privacy_safe,
            },
            sort_keys=True,
        )
    )
    if decision.decision.value != "deny" or not mutation_suppressed or not privacy_safe:
        raise SystemExit(2)


if __name__ == "__main__":
    main()

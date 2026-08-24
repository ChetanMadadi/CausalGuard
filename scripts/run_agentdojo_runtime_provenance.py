"""Run a real workspace read/send task with runtime-level provenance."""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

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
    AgentDojoRuntimeObserver,
    WorkspaceReadSendExtractor,
)


BENCHMARK_VERSION = "v1.2.2"
SUITE_NAME = "workspace"
TASK_ID = "user_task_33"
MODEL_NAME = "causalguard-runtime-provenance-demo"


class PlannedTaskLLM(BasePipelineElement):
    """Replay task calls while exercising AgentDojo's real execution loop."""

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
                content=[text_content_block_from_string("Task completed.")],
                tool_calls=None,
            )
        return query, runtime, env, [*messages, message], extra_args


def build_pipeline(calls: Sequence[FunctionCall]) -> AgentPipeline:
    llm = PlannedTaskLLM(calls)
    pipeline = AgentPipeline(
        [
            SystemMessage("Complete the user task with the available tools."),
            InitQuery(),
            llm,
            ToolsExecutionLoop([ToolsExecutor(), llm]),
        ]
    )
    pipeline.name = MODEL_NAME
    return pipeline


def run(output_dir: Path) -> tuple[GraphStore, bool, bool]:
    suite = get_suite(BENCHMARK_VERSION, SUITE_NAME)
    task = suite.get_user_task_by_id(TASK_ID)
    environment = suite.load_and_inject_default_environment({})
    ground_truth_environment = task.init_environment(
        environment.model_copy(deep=True)
    )
    calls = [
        FunctionCall(
            function=call.function,
            args=call.args,
            id=f"causalguard-runtime-{index}",
        )
        for index, call in enumerate(task.ground_truth(ground_truth_environment))
    ]
    observer = AgentDojoRuntimeObserver([WorkspaceReadSendExtractor()])
    collector = AgentDojoCollector(
        build_pipeline(calls),
        session_id=f"{SUITE_NAME}-{TASK_ID}-runtime",
        model_name=MODEL_NAME,
        runtime_observer=observer,
    )
    utility, security = suite.run_task_with_pipeline(
        collector,
        task,
        injection_task=None,
        injections={},
        environment=environment,
    )
    store = collector.write_outputs(output_dir)
    report = runtime_report(store, utility=utility, security=security)
    (output_dir / "runtime_report.md").write_text(report, encoding="utf-8")
    return store, utility, security


def runtime_report(
    store: GraphStore,
    *,
    utility: bool,
    security: bool,
) -> str:
    node_counts = Counter(node.node_type.value for node in store.nodes())
    edge_counts = Counter(edge.edge_type.value for edge in store.edges())
    nodes = list(store.nodes())
    file_node = next(
        node for node in nodes
        if node.node_type.value == "data_object"
        and node.resource_id == "agentdojo:workspace:file:19"
    )
    content_node = next(
        node for node in nodes
        if node.node_type.value == "data_object"
        and node.object_kind == "email_content"
    )
    email_node = next(
        node for node in nodes
        if node.node_type.value == "data_object"
        and node.object_kind == "email"
    )
    read_operation = next(
        node for node in nodes
        if node.node_type.value == "system_operation"
        and node.operation_type == "file_read"
    )
    send_operation = next(
        node for node in nodes
        if node.node_type.value == "system_operation"
        and node.operation_type == "email_send"
    )
    tool_results = sorted(
        (
            node for node in nodes
            if node.node_type.value == "data_object"
            and node.object_kind == "tool_result"
        ),
        key=lambda node: node.resource_id,
    )
    llms = sorted(
        (node for node in nodes if node.node_type.value == "llm_invocation"),
        key=lambda node: node.timestamp,
    )
    tools = sorted(
        (node for node in nodes if node.node_type.value == "tool_call"),
        key=lambda node: node.timestamp,
    )
    path = [
        file_node.node_id,
        read_operation.node_id,
        tool_results[0].node_id,
        llms[1].node_id,
        tools[1].node_id,
        send_operation.node_id,
        email_node.node_id,
    ]

    return f"""# AgentDojo runtime-enriched provenance report

- AgentDojo benchmark: {BENCHMARK_VERSION} / {SUITE_NAME} / {TASK_ID}
- Execution: real AgentDojo task environment, ToolsExecutor, and FunctionsRuntime
- Model element: deterministic stand-in for reproducibility
- Utility success: {utility}
- No-injection security result: {security}
- Graph acyclic: {store.is_acyclic()}

## Graph counts

- Nodes: {dict(node_counts)}
- Edges: {dict(edge_counts)}

## Runtime evidence

- Concrete read object: {file_node.resource_id}, version {file_node.version}
- Created outgoing object: {email_node.resource_id}, version {email_node.version}
- Stable payload object: {content_node.resource_id}, hash {content_node.content_hash}
- Destination: {send_operation.destination}
- Payload references: {", ".join(send_operation.payload_refs)}
- Approvals: unavailable in AgentDojo; none fabricated

## Recovered policy path

```text
{"\n→ ".join(path)}
```

The file read, email write, and payload relations are derived from direct
runtime observations. Raw file and email body content is not exported.
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("outputs/agentdojo/runtime_enriched"),
    )
    args = parser.parse_args()
    store, utility, _ = run(args.output_dir)
    print(
        f"Wrote {store.node_count} nodes and {store.edge_count} edges to "
        f"{args.output_dir}; utility={utility}"
    )


if __name__ == "__main__":
    main()

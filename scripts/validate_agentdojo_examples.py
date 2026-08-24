"""Validate CausalGuard graphs across diverse existing AgentDojo tasks."""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
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
from causalguard.integrations.agentdojo import AgentDojoCollector


BENCHMARK_VERSION = "v1.2.2"
SUITE_NAME = "workspace"
MODEL_NAME = "causalguard-planned-validation"


@dataclass(frozen=True)
class ExampleSpec:
    label: str
    task_id: str
    call_groups: tuple[tuple[int, ...], ...]
    shape: str
    prepend_invalid_call: bool = False


EXAMPLES = (
    ExampleSpec(
        "single_read",
        "user_task_28",
        ((0,),),
        "one successful tool call",
    ),
    ExampleSpec(
        "sequential_delete",
        "user_task_35",
        ((0,), (1,)),
        "two sequential successful calls",
    ),
    ExampleSpec(
        "sequential_write",
        "user_task_29",
        ((0,), (1,)),
        "read followed by state mutation",
    ),
    ExampleSpec(
        "parallel_then_write",
        "user_task_20",
        ((0, 1), (2,)),
        "two parallel calls followed by one write",
    ),
    ExampleSpec(
        "outgoing_email",
        "user_task_33",
        ((0,), (1,)),
        "read followed by outgoing action",
    ),
    ExampleSpec(
        "failed_then_recover",
        "user_task_28",
        ((0,),),
        "invalid call followed by successful recovery",
        prepend_invalid_call=True,
    ),
)


class PlannedLLM(BasePipelineElement):
    """Replay an existing task's ground-truth calls through the real tool loop."""

    name = MODEL_NAME

    def __init__(
        self,
        turns: Sequence[Sequence[FunctionCall]],
        final_text: str,
    ) -> None:
        self.turns = [
            [FunctionCall.model_validate(call.model_dump()) for call in turn]
            for turn in turns
        ]
        self.final_text = final_text

    def query(
        self,
        query: str,
        runtime: FunctionsRuntime,
        env: Env = EmptyEnv(),
        messages: Sequence[ChatMessage] = [],
        extra_args: dict = {},
    ) -> tuple[str, FunctionsRuntime, Env, Sequence[ChatMessage], dict]:
        completed_turns = sum(
            message["role"] == "assistant"
            and bool(message.get("tool_calls"))
            for message in messages
        )
        if completed_turns < len(self.turns):
            message = ChatAssistantMessage(
                role="assistant",
                content=None,
                tool_calls=self.turns[completed_turns],
            )
        else:
            message = ChatAssistantMessage(
                role="assistant",
                content=[text_content_block_from_string(self.final_text)],
                tool_calls=None,
            )
        return query, runtime, env, [*messages, message], extra_args


def build_pipeline(
    turns: Sequence[Sequence[FunctionCall]],
    final_text: str,
) -> AgentPipeline:
    llm = PlannedLLM(turns, final_text)
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


def expected_counts(
    turns: Sequence[Sequence[FunctionCall]],
    *,
    failed_calls: int,
) -> tuple[Counter[str], Counter[str]]:
    tool_calls = sum(len(turn) for turn in turns)
    successful_calls = tool_calls - failed_calls
    llm_calls = len(turns) + 1
    input_edges = sum(
        len(turn) * (len(turns) - index)
        for index, turn in enumerate(turns)
    )
    nodes = Counter(
        {
            "llm_invocation": llm_calls,
            "tool_call": tool_calls,
            "system_operation": successful_calls,
            "data_object": tool_calls,
            "human_approval": 0,
        }
    )
    edges = Counter(
        {
            "invokes": tool_calls,
            "triggers": successful_calls,
            "read": 0,
            "write": 0,
            "produces": tool_calls,
            "input_to": input_edges,
            "payload_of": 0,
            "authorizes": 0,
        }
    )
    return nodes, edges


def validate_store(
    store: GraphStore,
    expected_nodes: Counter[str],
    expected_edges: Counter[str],
    *,
    failed_calls: int,
) -> list[str]:
    issues: list[str] = []
    actual_nodes = Counter(node.node_type.value for node in store.nodes())
    actual_edges = Counter(edge.edge_type.value for edge in store.edges())
    if actual_nodes != +expected_nodes:
        issues.append(
            f"node counts differ: expected {dict(+expected_nodes)}, "
            f"got {dict(actual_nodes)}"
        )
    if actual_edges != +expected_edges:
        issues.append(
            f"edge counts differ: expected {dict(+expected_edges)}, "
            f"got {dict(actual_edges)}"
        )
    if not store.is_acyclic():
        issues.append("graph is cyclic")

    edges = list(store.edges())
    nodes_by_id = {node.node_id: node for node in store.nodes()}
    valid_endpoint_types = {
        "invokes": {("llm_invocation", "tool_call")},
        "triggers": {("tool_call", "system_operation")},
        "produces": {
            ("system_operation", "data_object"),
            ("tool_call", "data_object"),
        },
        "input_to": {("data_object", "llm_invocation")},
    }
    for edge in edges:
        source_type = nodes_by_id[edge.source_id].node_type.value
        target_type = nodes_by_id[edge.target_id].node_type.value
        allowed = valid_endpoint_types.get(edge.edge_type.value)
        if allowed is not None and (source_type, target_type) not in allowed:
            issues.append(
                f"{edge.edge_id} has invalid endpoints "
                f"{source_type}->{target_type}"
            )
        if edge.confidence.value != "high":
            issues.append(f"{edge.edge_id} is not high confidence")

    for node in store.nodes():
        if node.node_type.value == "tool_call":
            incoming = [
                edge for edge in edges
                if edge.target_id == node.node_id
                and edge.edge_type.value == "invokes"
            ]
            if len(incoming) != 1:
                issues.append(f"{node.node_id} has {len(incoming)} invokes edges")
            elif nodes_by_id[incoming[0].source_id].timestamp >= node.timestamp:
                issues.append(f"{node.node_id} is not after its invoking LLM")
        elif node.node_type.value == "system_operation":
            triggers = [
                edge for edge in edges
                if edge.target_id == node.node_id
                and edge.edge_type.value == "triggers"
            ]
            if len(triggers) != 1:
                issues.append(f"{node.node_id} has {len(triggers)} triggers edges")
            else:
                tool = nodes_by_id[triggers[0].source_id]
                if tool.timestamp >= node.timestamp:
                    issues.append(f"{node.node_id} is not after its triggering tool")
                if tool.tool_name != node.action_class:
                    issues.append(
                        f"{node.node_id} action {node.action_class!r} does not "
                        f"match triggering tool {tool.tool_name!r}"
                    )
        elif node.node_type.value == "data_object":
            producers = [
                edge for edge in edges
                if edge.target_id == node.node_id
                and edge.edge_type.value == "produces"
            ]
            if len(producers) != 1:
                issues.append(f"{node.node_id} has {len(producers)} producers")
                continue

            producer = producers[0]
            producer_type = nodes_by_id[producer.source_id].node_type.value
            expected_producer_type = (
                "tool_call" if node.object_kind == "tool_error"
                else "system_operation"
            )
            if producer_type != expected_producer_type:
                issues.append(
                    f"{node.node_id} expected producer type "
                    f"{expected_producer_type}, got {producer_type}"
                )

            actual_consumers = {
                edge.target_id for edge in edges
                if edge.source_id == node.node_id
                and edge.edge_type.value == "input_to"
            }
            expected_consumers = {
                candidate.node_id for candidate in store.nodes()
                if candidate.node_type.value == "llm_invocation"
                and candidate.timestamp > producer.timestamp
            }
            if actual_consumers != expected_consumers:
                issues.append(
                    f"{node.node_id} input_to consumers differ: expected "
                    f"{sorted(expected_consumers)}, got {sorted(actual_consumers)}"
                )

    tool_errors = sum(
        node.node_type.value == "data_object"
        and node.object_kind == "tool_error"
        for node in store.nodes()
    )
    if tool_errors != failed_calls:
        issues.append(
            f"expected {failed_calls} tool_error artifacts, got {tool_errors}"
        )
    return issues


def raw_values(calls: Sequence[FunctionCall]) -> set[str]:
    values: set[str] = set()

    def visit(value: object) -> None:
        if isinstance(value, str):
            if len(value) >= 6:
                values.add(value)
        elif isinstance(value, dict):
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    for call in calls:
        visit(call.args)
    return values


def run_example(
    spec: ExampleSpec,
    output_root: Path,
) -> dict[str, object]:
    suite = get_suite(BENCHMARK_VERSION, SUITE_NAME)
    task = suite.get_user_task_by_id(spec.task_id)
    environment = suite.load_and_inject_default_environment({})
    ground_truth_environment = task.init_environment(
        environment.model_copy(deep=True)
    )
    calls = task.ground_truth(ground_truth_environment)
    turns: list[list[FunctionCall]] = [
        [calls[index] for index in group]
        for group in spec.call_groups
    ]
    failed_calls = int(spec.prepend_invalid_call)
    if spec.prepend_invalid_call:
        turns.insert(
            0,
            [
                FunctionCall(
                    function="causalguard_missing_tool",
                    args={},
                    id="causalguard-validation-invalid",
                )
            ],
        )

    final_text = task.GROUND_TRUTH_OUTPUT or "Task completed."
    collector = AgentDojoCollector(
        build_pipeline(turns, final_text),
        session_id=f"{SUITE_NAME}-{spec.task_id}-{spec.label}",
        model_name=MODEL_NAME,
    )
    utility, security = suite.run_task_with_pipeline(
        collector,
        task,
        injection_task=None,
        injections={},
        environment=environment,
    )

    destination = output_root / spec.label
    store = collector.write_outputs(destination)
    expected_nodes, expected_edges = expected_counts(
        turns,
        failed_calls=failed_calls,
    )
    issues = validate_store(
        store,
        expected_nodes,
        expected_edges,
        failed_calls=failed_calls,
    )
    if not utility:
        issues.append("AgentDojo utility evaluation failed")
    if not security:
        issues.append("unexpected no-injection security failure")

    exported = "".join(
        (destination / filename).read_text(encoding="utf-8")
        for filename in ("trace.jsonl", "graph.json", "graph.dot")
    )
    forbidden = raw_values([call for turn in turns for call in turn])
    forbidden.update({task.PROMPT, final_text})
    leaked = sorted(value for value in forbidden if value and value in exported)
    if leaked:
        issues.append(f"raw values leaked into exports: {leaked}")

    actual_nodes = Counter(node.node_type.value for node in store.nodes())
    actual_edges = Counter(edge.edge_type.value for edge in store.edges())
    return {
        "label": spec.label,
        "task_id": spec.task_id,
        "shape": spec.shape,
        "utility": utility,
        "security": security,
        "expected_nodes": expected_nodes,
        "actual_nodes": actual_nodes,
        "expected_edges": expected_edges,
        "actual_edges": actual_edges,
        "node_count": store.node_count,
        "edge_count": store.edge_count,
        "issues": issues,
        "tools": [
            node.tool_name
            for node in store.nodes()
            if node.node_type.value == "tool_call"
        ],
    }


def validation_report(results: Sequence[dict[str, object]]) -> str:
    passed = sum(not result["issues"] for result in results)
    rows = []
    for result in results:
        status = "PASS" if not result["issues"] else "FAIL"
        rows.append(
            "| {label} | {task} | {shape} | {utility} | {nodes} | {edges} | {status} |".format(
                label=result["label"],
                task=result["task_id"],
                shape=result["shape"],
                utility=result["utility"],
                nodes=result["node_count"],
                edges=result["edge_count"],
                status=status,
            )
        )

    details = []
    for result in results:
        details.extend(
            [
                f"### {result['label']}",
                "",
                f"- Tools: {', '.join(result['tools'])}",
                f"- Expected nodes: {dict(+result['expected_nodes'])}",
                f"- Actual nodes: {dict(result['actual_nodes'])}",
                f"- Expected edges: {dict(+result['expected_edges'])}",
                f"- Actual edges: {dict(result['actual_edges'])}",
                f"- Issues: {result['issues'] or 'none'}",
                "",
            ]
        )

    return """# AgentDojo multi-example graph validation

Validated {passed}/{total} examples.

| Example | Existing task | Execution shape | Utility | Nodes | Edges | Result |
|---|---|---|---:|---:|---:|---|
{rows}

The expectations are computed independently from the planned AgentDojo turns:
one LLM node per model call, one Tool node per FunctionCall, one SysOp for each
successful runtime execution, and one Data node per exact tool feedback
artifact. Every prior tool feedback message is expected to be input_to every
later LLM call because AgentDojo passes the full transcript to its standard LLM
elements. Failed pre-runtime calls produce a tool_error Data artifact from the
Tool node without fabricating a SysOp.

PASS requires more than matching totals: every edge must have the expected
endpoint types and high confidence; each Tool must follow exactly one invoking
LLM; each SysOp must follow a matching Tool action; each Data artifact must have
the correct producer kind; every artifact must connect to exactly the complete
set of later LLM calls; and the graph must remain acyclic and privacy-safe.

No domain read, write, payload_of, destination, or approval relation is expected
because AgentDojo 0.1.35 does not expose stable evidence for those relations at
the transcript boundary.

## Details

{details}
""".format(
        passed=passed,
        total=len(results),
        rows="\n".join(rows),
        details="\n".join(details).rstrip(),
    )


def run(output_root: Path) -> list[dict[str, object]]:
    output_root.mkdir(parents=True, exist_ok=True)
    results = [run_example(spec, output_root) for spec in EXAMPLES]
    report = validation_report(results)
    (output_root / "validation_report.md").write_text(
        report,
        encoding="utf-8",
    )
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("outputs/agentdojo/examples"),
    )
    args = parser.parse_args()
    results = run(args.output_dir)
    failures = [result for result in results if result["issues"]]
    print(
        f"Validated {len(results) - len(failures)}/{len(results)} "
        f"AgentDojo example graphs in {args.output_dir}"
    )
    if failures:
        for result in failures:
            print(f"{result['label']}: {result['issues']}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()

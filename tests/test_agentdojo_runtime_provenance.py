from __future__ import annotations

import json
from collections.abc import Sequence

import networkx as nx
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

from causalguard.integrations.agentdojo import (
    AgentDojoCollector,
    AgentDojoRuntimeObserver,
    WorkspaceReadSendExtractor,
)
from causalguard.schema.edges import Confidence, Derivation, EdgeType
from scripts.run_agentdojo_runtime_provenance import run


def test_real_workspace_read_send_builds_policy_ready_graph(tmp_path) -> None:
    store, utility, security = run(tmp_path)

    assert utility is True
    assert security is True
    assert store.is_acyclic()
    assert store.node_count == 13
    assert store.edge_count == 17

    nodes = list(store.nodes())
    edges = list(store.edges())
    file_node = next(
        node for node in nodes
        if node.node_type.value == "data_object"
        and node.resource_id == "agentdojo:workspace:file:19"
    )
    email_node = next(
        node for node in nodes
        if node.node_type.value == "data_object"
        and node.object_kind == "email"
    )
    content_node = next(
        node for node in nodes
        if node.node_type.value == "data_object"
        and node.object_kind == "email_content"
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
    send_tool = next(
        node for node in nodes
        if node.node_type.value == "tool_call"
        and node.tool_name == "send_email"
    )

    assert file_node.version and file_node.content_hash
    assert email_node.version and email_node.content_hash
    assert content_node.version and content_node.content_hash
    assert send_operation.destination == "mailto:john.mitchell@gmail.com"
    assert send_tool.destination == send_operation.destination
    assert send_operation.destination_trust is None
    assert not any(
        node.node_type.value == "human_approval" for node in nodes
    )
    assert content_node.resource_id in send_operation.payload_refs
    assert file_node.resource_id in send_operation.payload_refs

    assert _has_edge(
        edges,
        file_node.node_id,
        read_operation.node_id,
        EdgeType.READ,
        Derivation.RUNTIME_OBSERVATION,
    )
    assert _has_edge(
        edges,
        send_operation.node_id,
        email_node.node_id,
        EdgeType.WRITE,
        Derivation.RUNTIME_OBSERVATION,
    )
    assert _has_edge(
        edges,
        content_node.node_id,
        send_operation.node_id,
        EdgeType.PAYLOAD_OF,
        Derivation.RUNTIME_OBSERVATION,
    )
    assert _has_edge(
        edges,
        file_node.node_id,
        send_operation.node_id,
        EdgeType.PAYLOAD_OF,
        Derivation.RUNTIME_OBSERVATION,
    )
    assert _has_edge(
        edges,
        file_node.node_id,
        send_tool.node_id,
        EdgeType.INPUT_TO,
    )
    assert any(
        edge.source_id.startswith("llm:")
        and edge.target_id == content_node.node_id
        and edge.edge_type is EdgeType.PRODUCES
        for edge in edges
    )

    read_tool = next(
        node for node in nodes
        if node.node_type.value == "tool_call"
        and node.tool_name == "search_files_by_filename"
    )
    assert _has_edge(
        edges,
        read_tool.node_id,
        read_operation.node_id,
        EdgeType.TRIGGERS,
    )
    assert _has_edge(
        edges,
        send_tool.node_id,
        send_operation.node_id,
        EdgeType.TRIGGERS,
    )

    graph = store.copy_networkx()
    assert nx.has_path(graph, file_node.node_id, email_node.node_id)
    assert len(
        [node for node in nodes if node.node_type.value == "llm_invocation"]
    ) == 3
    assert len(
        [node for node in nodes if node.node_type.value == "tool_call"]
    ) == 2

    exported_json = json.loads((tmp_path / "graph.json").read_text())
    exported_dot = (tmp_path / "graph.dot").read_text()
    exported_trace = (tmp_path / "trace.jsonl").read_text()
    assert len(exported_json["nodes"]) == store.node_count
    assert any(
        node.get("destination") == "mailto:john.mitchell@gmail.com"
        for node in exported_json["nodes"]
    )
    assert any(
        edge["edge_type"] == "payload_of" for edge in exported_json["edges"]
    )
    assert "payload_of" in exported_dot
    assert "runtime_observation/high" in exported_dot
    combined = exported_dot + exported_trace + json.dumps(exported_json)
    assert "2024-06-01" not in combined
    assert "Summary of the client meeting" not in combined


def test_failed_outgoing_call_does_not_create_successful_write() -> None:
    suite = get_suite("v1.2.2", "workspace")
    task = suite.get_user_task_by_id("user_task_33")
    environment = suite.load_and_inject_default_environment({})
    calls = [
        FunctionCall(
            id="read",
            function="search_files_by_filename",
            args={"filename": "client-meeting-minutes.docx"},
        ),
        FunctionCall(
            id="bad-send",
            function="send_email",
            args={
                "recipients": ["john.mitchell@gmail.com"],
                "subject": "Summary of the client meeting",
            },
        ),
    ]
    observer = AgentDojoRuntimeObserver([WorkspaceReadSendExtractor()])
    collector = AgentDojoCollector(
        _pipeline(calls),
        session_id="failed-runtime-send",
        runtime_observer=observer,
    )
    before_email_count = len(environment.inbox.emails)

    _, _, final_environment, _, _ = collector.query(
        task.PROMPT,
        FunctionsRuntime(suite.tools),
        environment,
    )
    store = collector.build_graph()

    assert len(final_environment.inbox.emails) == before_email_count
    assert len(observer.observations) == 2
    assert observer.observations[0].succeeded is True
    assert observer.observations[1].succeeded is False
    assert observer.observations[1].operations == ()
    assert sum(edge.edge_type is EdgeType.READ for edge in store.edges()) == 2
    assert sum(edge.edge_type is EdgeType.WRITE for edge in store.edges()) == 0
    assert sum(edge.edge_type is EdgeType.PAYLOAD_OF for edge in store.edges()) == 0
    assert not any(
        node.node_type.value == "system_operation"
        and node.operation_type == "email_send"
        for node in store.nodes()
    )
    assert any(
        node.node_type.value == "data_object"
        and node.object_kind == "tool_error"
        for node in store.nodes()
    )


class _PlannedLLM(BasePipelineElement):
    name = "runtime-test-planned"

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


def _pipeline(calls: Sequence[FunctionCall]) -> AgentPipeline:
    llm = _PlannedLLM(calls)
    pipeline = AgentPipeline(
        [
            SystemMessage("Complete the task."),
            InitQuery(),
            llm,
            ToolsExecutionLoop([ToolsExecutor(), llm]),
        ]
    )
    pipeline.name = llm.name
    return pipeline


def _has_edge(
    edges,
    source_id: str,
    target_id: str,
    edge_type: EdgeType,
    derivation: Derivation | None = None,
) -> bool:
    return any(
        edge.source_id == source_id
        and edge.target_id == target_id
        and edge.edge_type is edge_type
        and (derivation is None or edge.derivation is derivation)
        and edge.confidence is Confidence.HIGH
        for edge in edges
    )

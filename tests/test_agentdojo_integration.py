from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

import pytest

pytest.importorskip("agentdojo")

from agentdojo.agent_pipeline.base_pipeline_element import BasePipelineElement
from agentdojo.functions_runtime import (
    EmptyEnv,
    Env,
    FunctionCall,
    FunctionsRuntime,
)
from agentdojo.types import (
    ChatAssistantMessage,
    ChatMessage,
    ChatToolResultMessage,
    text_content_block_from_string,
)

from causalguard.graph import GraphBuilder, GraphStore
from causalguard.integrations.agentdojo import (
    AgentDojoCollector,
    AgentDojoTraceMapper,
)
from causalguard.schema.edges import EdgeType


def successful_transcript() -> list[ChatMessage]:
    first_call = FunctionCall(function="list_files", args={}, id="call-1")
    second_call = FunctionCall(
        function="delete_file",
        args={"file_id": "sensitive-file-id"},
        id="call-2",
    )
    return [
        ChatAssistantMessage(
            role="assistant",
            content=None,
            tool_calls=[first_call],
        ),
        ChatToolResultMessage(
            role="tool",
            content=[text_content_block_from_string("private file contents")],
            tool_call_id=first_call.id,
            tool_call=first_call,
            error=None,
        ),
        ChatAssistantMessage(
            role="assistant",
            content=None,
            tool_calls=[second_call],
        ),
        ChatToolResultMessage(
            role="tool",
            content=[text_content_block_from_string("private deletion result")],
            tool_call_id=second_call.id,
            tool_call=second_call,
            error=None,
        ),
        ChatAssistantMessage(
            role="assistant",
            content=[text_content_block_from_string("done")],
            tool_calls=None,
        ),
    ]


def test_mapper_builds_only_evidence_backed_return_path() -> None:
    mapper = AgentDojoTraceMapper(session_id="test", model_name="scripted")
    events = mapper.map_trace(
        "delete the largest file",
        successful_transcript(),
    )
    store = GraphStore()
    GraphBuilder(store).process_trace(events)

    assert store.node_count == 9
    assert store.edge_count == 9
    assert store.is_acyclic()

    edge_counts = {
        edge_type: sum(edge.edge_type is edge_type for edge in store.edges())
        for edge_type in EdgeType
    }
    assert edge_counts[EdgeType.INVOKES] == 2
    assert edge_counts[EdgeType.TRIGGERS] == 2
    assert edge_counts[EdgeType.PRODUCES] == 2
    assert edge_counts[EdgeType.INPUT_TO] == 3
    assert edge_counts[EdgeType.READ] == 0
    assert edge_counts[EdgeType.WRITE] == 0
    assert edge_counts[EdgeType.PAYLOAD_OF] == 0
    assert edge_counts[EdgeType.AUTHORIZES] == 0

    topological_types = [
        store.get_node(node_id).node_type.value
        for node_id in store.topological_node_ids()
    ]
    assert topological_types == [
        "llm_invocation",
        "tool_call",
        "system_operation",
        "data_object",
        "llm_invocation",
        "tool_call",
        "system_operation",
        "data_object",
        "llm_invocation",
    ]

    assert any(
        edge.source_id == "data:agentdojo:test:tool_result:1"
        and edge.target_id == "llm:agentdojo:test:llm:4"
        and edge.edge_type is EdgeType.INPUT_TO
        for edge in store.edges()
    )
    serialized_events = "\n".join(
        event.model_dump_json() for event in events
    )
    assert "private file contents" not in serialized_events
    assert "private deletion result" not in serialized_events
    assert "sensitive-file-id" not in serialized_events
    assert '"argument_names":["file_id"]' in serialized_events


def test_failed_result_does_not_fabricate_system_operation() -> None:
    call = FunctionCall(function="missing_tool", args={}, id="missing")
    messages: list[ChatMessage] = [
        ChatAssistantMessage(role="assistant", content=None, tool_calls=[call]),
        ChatToolResultMessage(
            role="tool",
            content=[text_content_block_from_string("")],
            tool_call_id=call.id,
            tool_call=call,
            error="Invalid tool missing_tool provided.",
        ),
        ChatAssistantMessage(
            role="assistant",
            content=[text_content_block_from_string("unable")],
            tool_calls=None,
        ),
    ]

    events = AgentDojoTraceMapper(session_id="failed").map_trace(
        "try",
        messages,
    )
    store = GraphStore()
    GraphBuilder(store).process_trace(events)

    assert [
        event.event_type.value for event in events
    ].count("system_operation") == 0
    assert store.node_count == 4
    assert store.edge_count == 3
    assert sum(
        edge.edge_type is EdgeType.PRODUCES for edge in store.edges()
    ) == 1
    assert any(
        edge.source_id == "tool:agentdojo:failed:tool:0:0"
        and edge.target_id == "data:agentdojo:failed:tool_result:1"
        and edge.edge_type is EdgeType.PRODUCES
        for edge in store.edges()
    )
    assert sum(
        edge.edge_type is EdgeType.INPUT_TO for edge in store.edges()
    ) == 1
    error_node = store.get_node("data:agentdojo:failed:tool_result:1")
    assert error_node.object_kind == "tool_error"
    serialized = "\n".join(event.model_dump_json() for event in events)
    assert "Invalid tool missing_tool provided" not in serialized


def test_parallel_calls_fan_out_and_join_with_full_history() -> None:
    first = FunctionCall(function="read_calendar", args={}, id="parallel-1")
    second = FunctionCall(function="read_contacts", args={}, id="parallel-2")
    third = FunctionCall(function="create_event", args={}, id="parallel-3")
    messages: list[ChatMessage] = [
        ChatAssistantMessage(
            role="assistant",
            content=None,
            tool_calls=[first, second],
        ),
        ChatToolResultMessage(
            role="tool",
            content=[text_content_block_from_string("calendar result")],
            tool_call_id=first.id,
            tool_call=first,
            error=None,
        ),
        ChatToolResultMessage(
            role="tool",
            content=[text_content_block_from_string("contacts result")],
            tool_call_id=second.id,
            tool_call=second,
            error=None,
        ),
        ChatAssistantMessage(
            role="assistant",
            content=None,
            tool_calls=[third],
        ),
        ChatToolResultMessage(
            role="tool",
            content=[text_content_block_from_string("created event")],
            tool_call_id=third.id,
            tool_call=third,
            error=None,
        ),
        ChatAssistantMessage(
            role="assistant",
            content=[text_content_block_from_string("done")],
            tool_calls=None,
        ),
    ]

    events = AgentDojoTraceMapper(session_id="parallel").map_trace(
        "schedule it",
        messages,
    )
    store = GraphStore()
    GraphBuilder(store).process_trace(events)
    edge_counts = Counter(edge.edge_type for edge in store.edges())

    assert store.node_count == 12
    assert store.edge_count == 14
    assert edge_counts[EdgeType.INVOKES] == 3
    assert edge_counts[EdgeType.TRIGGERS] == 3
    assert edge_counts[EdgeType.PRODUCES] == 3
    assert edge_counts[EdgeType.INPUT_TO] == 5
    assert store.is_acyclic()


def test_collector_wraps_pipeline_and_writes_privacy_safe_outputs(
    tmp_path,
) -> None:
    delegate = StaticPipeline(successful_transcript())
    collector = AgentDojoCollector(
        delegate,
        session_id="export",
        model_name="static",
    )

    collector.query("delete the largest file", FunctionsRuntime())
    store = collector.write_outputs(tmp_path)

    assert store.node_count == 9
    assert (tmp_path / "trace.jsonl").is_file()
    assert (tmp_path / "graph.json").is_file()
    assert (tmp_path / "graph.dot").is_file()
    combined = "".join(
        path.read_text(encoding="utf-8")
        for path in (
            tmp_path / "trace.jsonl",
            tmp_path / "graph.json",
            tmp_path / "graph.dot",
        )
    )
    assert "private file contents" not in combined
    assert "private deletion result" not in combined


class StaticPipeline(BasePipelineElement):
    name = "static"

    def __init__(self, transcript: Sequence[ChatMessage]) -> None:
        self.transcript = transcript

    def query(
        self,
        query: str,
        runtime: FunctionsRuntime,
        env: Env = EmptyEnv(),
        messages: Sequence[ChatMessage] = [],
        extra_args: dict = {},
    ) -> tuple[str, FunctionsRuntime, Env, Sequence[ChatMessage], dict]:
        return query, runtime, env, self.transcript, extra_args

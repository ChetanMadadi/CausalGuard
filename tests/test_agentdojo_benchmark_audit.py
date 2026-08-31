from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

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
from causalguard.integrations.agentdojo.audit import (
    audit_graph_fidelity,
    collect_executed_calls,
    state_diff,
    state_view,
    task_inventory,
)
from causalguard.schema.privacy import assert_no_raw_content_keys
from scripts.finalize_agentdojo_benchmark_audit import (
    find_candidates,
    normalize_call_accounting,
    sanitize_slurm_logs,
)


def test_v122_inventory_is_the_installed_normal_task_inventory() -> None:
    inventory = task_inventory("v1.2.2")

    assert len(inventory) == 97
    assert Counter(item["suite"] for item in inventory) == {
        "workspace": 40,
        "travel": 20,
        "banking": 16,
        "slack": 21,
    }
    assert len({(item["suite"], item["task_id"]) for item in inventory}) == 97
    assert all("PROMPT" not in item for item in inventory)
    assert all(str(item["prompt_hash"]).startswith("sha256:") for item in inventory)


def test_observation_only_send_mutates_one_fresh_environment_and_not_another() -> None:
    suite = get_suite("v1.2.2", "workspace")
    first = suite.load_and_inject_default_environment({})
    second = suite.load_and_inject_default_environment({})
    before_first = first.model_copy(deep=True)
    before_count = len(first.inbox.emails)
    second_count = len(second.inbox.emails)
    call = FunctionCall(
        id="send",
        function="send_email",
        args={
            "recipients": ["audit-recipient@example.org"],
            "subject": "audit subject",
            "body": "audit private body",
            "attachments": None,
            "cc": None,
            "bcc": None,
        },
    )
    observer = AgentDojoRuntimeObserver(
        [WorkspaceReadSendExtractor()], capture_all_state=True
    )
    collector = AgentDojoCollector(
        _pipeline([call]),
        session_id="observation-only-send",
        runtime_observer=observer,
        policy_enforcer=None,
    )

    collector.query("send it", FunctionsRuntime(suite.tools), first)
    store = collector.build_graph()

    assert collector.policy_enforcer is None
    assert len(first.inbox.emails) == before_count + 1
    assert len(second.inbox.emails) == second_count
    assert observer.observations[0].state_changed is True
    assert store.is_acyclic()
    fidelity = audit_graph_fidelity(store, collector.messages, observer.observations)
    assert fidelity["tool_call_coverage_correct"] is True
    assert fidelity["system_operation_coverage_correct"] is True
    assert fidelity["payload_provenance_correct"] is True
    assert fidelity["destination_correlation_correct"] is True
    assert fidelity["actual_mutation_vs_graph_consistent"] is True
    assert fidelity["instrumentation_failure"] is False

    effect = {
        "before": state_view(before_first),
        "after": state_view(first),
        "changes": state_diff(before_first, first, suite="workspace"),
    }
    assert effect["changes"]
    assert_no_raw_content_keys(effect)
    exported = json.dumps(effect)
    assert "audit private body" not in exported
    assert "audit subject" not in exported


def test_unsupported_slack_mutation_is_coverage_gap_not_collector_failure() -> None:
    suite = get_suite("v1.2.2", "slack")
    environment = suite.load_and_inject_default_environment({})
    recipient = next(user for user in environment.slack.users if user != "bot")
    call = FunctionCall(
        id="dm",
        function="send_direct_message",
        args={"recipient": recipient, "body": "private audit message"},
    )
    observer = AgentDojoRuntimeObserver(
        [WorkspaceReadSendExtractor()], capture_all_state=True
    )
    collector = AgentDojoCollector(
        _pipeline([call]),
        session_id="unsupported-slack-send",
        runtime_observer=observer,
        policy_enforcer=None,
    )

    collector.query("send a DM", FunctionsRuntime(suite.tools), environment)
    store = collector.build_graph()
    calls = collect_executed_calls(
        collector.attempt_messages,
        collector.attempt_runtime_observations,
    )
    fidelity = audit_graph_fidelity(store, collector.messages, observer.observations)

    assert calls[0].state_changed is True
    assert calls[0].destinations == (f"slack:user:{recipient}",)
    assert fidelity["tool_call_coverage_correct"] is True
    assert fidelity["invocation_causality_correct"] is True
    assert fidelity["runtime_observer_correlation_correct"] is True
    assert fidelity["system_operation_coverage_correct"] is False
    assert fidelity["write_provenance_correct"] is False
    assert fidelity["payload_provenance_correct"] is False
    assert fidelity["destination_correlation_correct"] is False
    assert fidelity["actual_mutation_vs_graph_consistent"] is False
    assert fidelity["instrumentation_failure"] is False


def test_finalizer_separates_proposals_from_result_bearing_calls() -> None:
    rows = [{"number_of_tool_calls": 3, "tool_calls": [{}, {}]}]

    normalize_call_accounting(rows)

    assert rows[0]["number_of_tool_calls"] == 2
    assert rows[0]["number_of_tool_call_proposals"] == 3
    assert rows[0]["unexecuted_tool_call_proposals"] == 1


def test_finalizer_does_not_call_structural_near_match_a_strict_match() -> None:
    def row(task_id: str, argument_hash: str, destination: str) -> dict:
        return {
            "suite": "slack",
            "task_id": task_id,
            "candidate_security_policy_case": True,
            "candidate_matched_near_matched_case": False,
            "tool_calls": [
                {
                    "function_name": "send_direct_message",
                    "argument_hash": argument_hash,
                    "argument_summary": {
                        "argument_names": ["body", "recipient"],
                        "argument_type_names": ["str", "str"],
                    },
                    "error_hash": None,
                    "state_changed": True,
                }
            ],
            "actual_destinations": [destination],
            "actual_payload_object_ids_types": [],
            "resources_written_mutated": [f"state:{task_id}"],
        }

    rows = [row("one", "sha256:one", "slack:user:one"), row("two", "sha256:two", "slack:user:two")]

    exact, near = find_candidates(rows)

    assert exact == []
    assert len(near) == 1
    assert all(item["candidate_matched_near_matched_case"] for item in rows)


def test_slurm_log_sanitizer_keeps_hash_only_evidence(tmp_path: Path) -> None:
    log_dir = tmp_path / "slurm"
    log_dir.mkdir()
    log = log_dir / "slurm-1_0.out"
    log.write_text('[debug] broken JSON: {"body": "private"}>\n')

    result = sanitize_slurm_logs(tmp_path)

    sanitized = log.read_text()
    assert result["redacted_line_count"] == 1
    assert "private" not in sanitized
    assert "sha256:" in sanitized
    assert json.loads((tmp_path / "privacy_sanitization.json").read_text())["raw_text_retained"] is False


class _PlannedLLM(BasePipelineElement):
    name = "benchmark-audit-planned"

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

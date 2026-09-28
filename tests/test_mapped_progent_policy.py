from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("agentdojo")

from agentdojo.functions_runtime import FunctionCall, FunctionsRuntime
from agentdojo.task_suite.load_suites import get_suite
from agentdojo.types import ChatAssistantMessage

from causalguard.integrations.agentdojo import (
    AgentDojoPolicyEnforcer,
    AgentDojoRuntimeObserver,
    AgentDojoTraceMapper,
    PolicyEnforcingToolsExecutor,
)
from causalguard.policy import (
    MappedProgentPolicy,
    MappedProgentPolicyEngine,
    PolicyAction,
)
from scripts.run_mapped_progent_policy_eval import label_consequential_calls


ARTIFACT = Path("policies/progent_slack_user_task_12.json")


def _policy() -> MappedProgentPolicy:
    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    return MappedProgentPolicy(policy_id=payload["policy_id"], **payload["rule"])


class _TrackingRuntime:
    def __init__(self, delegate: FunctionsRuntime) -> None:
        self.delegate = delegate
        self.functions = delegate.functions
        self.executed: list[tuple[str, dict]] = []

    def run_function(self, env, function_name, args):
        self.executed.append((function_name, dict(args)))
        return self.delegate.run_function(env, function_name, args)


def _evaluate(call: FunctionCall):
    suite = get_suite("v1.2.2", "slack")
    task = suite.get_user_task_by_id("user_task_12")
    environment = task.init_environment(suite.load_and_inject_default_environment({}))
    observer = AgentDojoRuntimeObserver([])
    enforcer = AgentDojoPolicyEnforcer(
        mapper=AgentDojoTraceMapper(session_id="mapped-progent-test"),
        observer=observer,
        tool_argument_policies=[_policy()],
    )
    executor = PolicyEnforcingToolsExecutor(enforcer)
    runtime = _TrackingRuntime(FunctionsRuntime(suite.tools))
    messages = [
        ChatAssistantMessage(role="assistant", content=None, tool_calls=[call])
    ]
    before = {
        channel: len(items)
        for channel, items in environment.slack.channel_inbox.items()
    }
    _, _, final_environment, final_messages, _ = executor.query(
        task.PROMPT, runtime, environment, messages
    )
    after = {
        channel: len(items)
        for channel, items in final_environment.slack.channel_inbox.items()
    }
    return enforcer, runtime, before, after, final_messages[-1]


def test_frozen_artifact_is_the_exact_generated_rule() -> None:
    policy = _policy()

    assert policy.tool_name == "send_channel_message"
    assert policy.priority == 100
    assert policy.effect == 0
    assert policy.conditions == {
        "body": "Hi, i am a bot",
        "channel": "External",
    }
    assert policy.fallback == 0


def test_legitimate_ground_truth_call_is_allowed() -> None:
    call = FunctionCall(
        id="legitimate",
        function="send_channel_message",
        args={"channel": "External_0", "body": "Hi, i am a bot"},
    )

    enforcer, runtime, before, after, result = _evaluate(call)

    assert enforcer.decisions[0].decision is PolicyAction.ALLOW
    assert runtime.executed == [(call.function, call.args)]
    assert after["External_0"] == before.get("External_0", 0) + 1
    assert result.get("error") is None


def test_violating_call_is_denied_before_tool_execution() -> None:
    call = FunctionCall(
        id="violation",
        function="send_channel_message",
        args={"channel": "general", "body": "Hi, i am a bot"},
    )

    enforcer, runtime, before, after, result = _evaluate(call)

    assert enforcer.decisions[0].decision is PolicyAction.DENY
    assert runtime.executed == []
    assert after == before
    assert (result.get("error") or "").startswith("CausalGuard suppressed")


def test_null_tool_call_id_is_reconciled_with_online_decision() -> None:
    call = FunctionCall(
        id=None,
        function="send_channel_message",
        args={"channel": "general", "body": "Hi, i am a bot"},
    )
    decision = MappedProgentPolicyEngine().evaluate(
        _policy(),
        tool_name=call.function,
        arguments=call.args,
        tool_call_id="proposal-0",
    )

    labels = label_consequential_calls([call], {None}, _policy(), (decision,))

    assert labels[0]["causalguard_decision"] == "deny"
    assert labels[0]["blocked_violation"] is True

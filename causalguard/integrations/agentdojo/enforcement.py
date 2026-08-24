"""Pre-execution AgentDojo policy enforcement at the tool boundary."""

from __future__ import annotations

from ast import literal_eval
from collections.abc import Sequence

from agentdojo.agent_pipeline.llms.google_llm import EMPTY_FUNCTION_NAME
from agentdojo.agent_pipeline.tool_execution import ToolsExecutor, is_string_list
from agentdojo.functions_runtime import EmptyEnv, Env, FunctionsRuntime
from agentdojo.types import (
    ChatMessage,
    ChatToolResultMessage,
    text_content_block_from_string,
)

from causalguard.graph import GraphBuilder, GraphStore
from causalguard.integrations.agentdojo.extractors.base import ToolProposalContext
from causalguard.integrations.agentdojo.mapper import AgentDojoTraceMapper
from causalguard.integrations.agentdojo.runtime import AgentDojoRuntimeObserver
from causalguard.policy import PolicyAction, PolicyDecision, PolicyDefinition, PolicyEngine
from causalguard.schema.events import NormalizedEvent


_EXECUTABLE_DECISIONS = {PolicyAction.ALLOW, PolicyAction.ALLOW_WITH_AUDIT}


class AgentDojoPolicyEnforcer:
    """Build proposed-action provenance and evaluate policies before mutation."""

    def __init__(
        self,
        policies: Sequence[PolicyDefinition],
        *,
        mapper: AgentDojoTraceMapper,
        observer: AgentDojoRuntimeObserver,
        approval_events: Sequence[NormalizedEvent] = (),
    ) -> None:
        self.policies = tuple(policies)
        self.mapper = mapper
        self.observer = observer
        self.approval_events = tuple(approval_events)
        self.engine = PolicyEngine()
        self.decisions: list[PolicyDecision] = []
        self.last_evaluation_store: GraphStore | None = None

    def begin_attempt(self) -> None:
        self.decisions.clear()
        self.last_evaluation_store = None

    def evaluate(
        self,
        *,
        query: str,
        messages: Sequence[ChatMessage],
        environment: Env,
        call_index: int,
    ) -> tuple[PolicyDecision, ...]:
        message = messages[-1]
        tool_call = (message.get("tool_calls") or [])[call_index]
        triggered = [
            policy
            for policy in self.policies
            if policy.trigger.action_class == tool_call.function
        ]
        if not triggered:
            return ()

        self.observer.propose(
            ToolProposalContext(
                function_name=tool_call.function,
                arguments=tool_call.args,
                environment=environment,
            ),
            source_tool_call_id=tool_call.id,
        )
        events = [
            *self.approval_events,
            *self.mapper.map_trace(
                query,
                messages,
                self.observer.observations,
                self.observer.proposals,
            ),
        ]
        store = GraphStore()
        GraphBuilder(store).process_trace(events)
        self.last_evaluation_store = store
        tool_node_id = self.mapper.tool_node_id(
            message_index=len(messages) - 1,
            call_index=call_index,
        )
        decisions = tuple(
            decision
            for policy in triggered
            if (
                decision := self.engine.evaluate(
                    policy,
                    store,
                    tool_node_id,
                )
            )
            is not None
        )
        self.decisions.extend(decisions)
        return decisions

    @staticmethod
    def permits_execution(decisions: Sequence[PolicyDecision]) -> bool:
        return all(decision.decision in _EXECUTABLE_DECISIONS for decision in decisions)


class PolicyEnforcingToolsExecutor(ToolsExecutor):
    """AgentDojo ToolsExecutor that suppresses calls denied by CausalGuard."""

    def __init__(self, enforcer: AgentDojoPolicyEnforcer, tool_output_formatter=None):
        if tool_output_formatter is None:
            super().__init__()
        else:
            super().__init__(tool_output_formatter)
        self.enforcer = enforcer

    def query(
        self,
        query: str,
        runtime: FunctionsRuntime,
        env: Env = EmptyEnv(),
        messages: Sequence[ChatMessage] = [],
        extra_args: dict = {},
    ) -> tuple[str, FunctionsRuntime, Env, Sequence[ChatMessage], dict]:
        if not messages or messages[-1]["role"] != "assistant":
            return query, runtime, env, messages, extra_args
        tool_calls = messages[-1].get("tool_calls")
        if not tool_calls:
            return query, runtime, env, messages, extra_args

        results = []
        available_functions = {tool.name for tool in runtime.functions.values()}
        for call_index, tool_call in enumerate(tool_calls):
            if tool_call.function == EMPTY_FUNCTION_NAME:
                results.append(
                    _error_result(
                        tool_call,
                        "Empty function name provided. Provide a valid function name.",
                    )
                )
                continue
            if tool_call.function not in available_functions:
                results.append(
                    _error_result(
                        tool_call,
                        f"Invalid tool {tool_call.function} provided.",
                    )
                )
                continue

            for key, value in tool_call.args.items():
                if isinstance(value, str) and is_string_list(value):
                    tool_call.args[key] = literal_eval(value)

            decisions = self.enforcer.evaluate(
                query=query,
                messages=messages,
                environment=env,
                call_index=call_index,
            )
            if not self.enforcer.permits_execution(decisions):
                policy_ids = ",".join(decision.policy_id for decision in decisions)
                results.append(
                    _error_result(
                        tool_call,
                        f"CausalGuard suppressed this call under policy {policy_ids}.",
                    )
                )
                continue

            tool_result, error = runtime.run_function(
                env,
                tool_call.function,
                tool_call.args,
            )
            formatted = self.output_formatter(tool_result)
            results.append(
                ChatToolResultMessage(
                    role="tool",
                    content=[text_content_block_from_string(formatted)],
                    tool_call_id=tool_call.id,
                    tool_call=tool_call,
                    error=error,
                )
            )

        return query, runtime, env, [*messages, *results], extra_args


def _error_result(tool_call, error: str) -> ChatToolResultMessage:
    return ChatToolResultMessage(
        role="tool",
        content=[text_content_block_from_string("")],
        tool_call_id=tool_call.id,
        tool_call=tool_call,
        error=error,
    )

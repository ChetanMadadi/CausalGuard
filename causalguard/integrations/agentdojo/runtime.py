"""Transparent observation of AgentDojo's actual FunctionsRuntime boundary."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from agentdojo.functions_runtime import (
    Function,
    FunctionReturnType,
    FunctionsRuntime,
    TaskEnvironment,
)

from causalguard.integrations.agentdojo.common import stable_hash
from causalguard.integrations.agentdojo.extractors.base import (
    AgentDojoDomainExtractor,
    DomainOperationEvidence,
    ProposedActionEvidence,
    ToolExecutionContext,
    ToolProposalContext,
)


@dataclass(frozen=True)
class RuntimeCallObservation:
    """Privacy-safe evidence captured for one top-level runtime invocation."""

    ordinal: int
    function_name: str
    argument_hash: str
    succeeded: bool
    error_hash: str | None
    operations: tuple[DomainOperationEvidence, ...]
    state_before_hash: str | None = None
    state_after_hash: str | None = None

    @property
    def state_changed(self) -> bool | None:
        if self.state_before_hash is None or self.state_after_hash is None:
            return None
        return self.state_before_hash != self.state_after_hash


@dataclass(frozen=True)
class ProposedCallObservation:
    """Privacy-safe evidence captured before a proposed call executes."""

    ordinal: int
    function_name: str
    argument_hash: str
    source_tool_call_id: str | None
    evidence: tuple[ProposedActionEvidence, ...]


class AgentDojoRuntimeObserver:
    """Collect domain evidence without retaining raw arguments or state snapshots."""

    def __init__(
        self,
        extractors: Sequence[AgentDojoDomainExtractor] = (),
        *,
        capture_all_state: bool = False,
    ) -> None:
        self.extractors = tuple(extractors)
        self.capture_all_state = capture_all_state
        self._observations: list[RuntimeCallObservation] = []
        self._proposals: list[ProposedCallObservation] = []

    @property
    def observations(self) -> tuple[RuntimeCallObservation, ...]:
        return tuple(self._observations)

    @property
    def proposals(self) -> tuple[ProposedCallObservation, ...]:
        return tuple(self._proposals)

    def begin_attempt(self) -> None:
        self._observations.clear()
        self._proposals.clear()

    def supports(self, function_name: str) -> bool:
        return any(extractor.supports(function_name) for extractor in self.extractors)

    def record(self, context: ToolExecutionContext) -> RuntimeCallObservation:
        operations = tuple(
            operation
            for extractor in self.extractors
            if extractor.supports(context.function_name)
            for operation in extractor.extract(context)
        )
        observation = RuntimeCallObservation(
            ordinal=len(self._observations),
            function_name=context.function_name,
            argument_hash=stable_hash(context.arguments),
            succeeded=context.error is None,
            error_hash=(
                stable_hash(context.error) if context.error is not None else None
            ),
            operations=operations,
            state_before_hash=(
                stable_hash(context.environment_before)
                if context.environment_before is not None
                else None
            ),
            state_after_hash=(
                stable_hash(context.environment_after)
                if context.environment_after is not None
                else None
            ),
        )
        self._observations.append(observation)
        return observation

    def propose(
        self,
        context: ToolProposalContext,
        *,
        source_tool_call_id: str | None,
    ) -> ProposedCallObservation:
        evidence = tuple(
            proposed
            for extractor in self.extractors
            if extractor.supports(context.function_name)
            if (proposed := extractor.propose(context)) is not None
        )
        observation = ProposedCallObservation(
            ordinal=len(self._proposals),
            function_name=context.function_name,
            argument_hash=stable_hash(context.arguments),
            source_tool_call_id=source_tool_call_id,
            evidence=evidence,
        )
        self._proposals.append(observation)
        return observation


class ObservingFunctionsRuntime(FunctionsRuntime):
    """Proxy a runtime while observing supported function executions."""

    def __init__(
        self,
        delegate: FunctionsRuntime,
        observer: AgentDojoRuntimeObserver,
    ) -> None:
        self.delegate = delegate
        self.observer = observer
        self.functions = delegate.functions

    def run_function(
        self,
        env: TaskEnvironment | None,
        function: str,
        kwargs: Mapping[str, Any],
        raise_on_error: bool = False,
    ) -> tuple[FunctionReturnType, str | None]:
        capture_state = self.observer.capture_all_state or self.observer.supports(function)
        before = _snapshot(env) if capture_state else None
        try:
            result, error = self.delegate.run_function(
                env,
                function,
                kwargs,
                raise_on_error=raise_on_error,
            )
        except Exception as exc:
            after = _snapshot(env) if capture_state else None
            self.observer.record(
                ToolExecutionContext(
                    function_name=function,
                    arguments=kwargs,
                    result="",
                    error=f"{type(exc).__name__}: {exc}",
                    environment_before=before,
                    environment_after=after,
                )
            )
            raise

        after = _snapshot(env) if capture_state else None
        self.observer.record(
            ToolExecutionContext(
                function_name=function,
                arguments=kwargs,
                result=result,
                error=error,
                environment_before=before,
                environment_after=after,
            )
        )
        return result, error

    def register_function(self, function: Any) -> Any:
        registered = self.delegate.register_function(function)
        self.functions = self.delegate.functions
        return registered

    def update_functions(self, new_functions: dict[str, Function]) -> None:
        self.delegate.update_functions(new_functions)
        self.functions = self.delegate.functions


def _snapshot(environment: TaskEnvironment | None) -> TaskEnvironment | None:
    if environment is None:
        return None
    return environment.model_copy(deep=True)

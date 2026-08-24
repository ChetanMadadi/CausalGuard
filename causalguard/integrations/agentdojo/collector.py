"""Least-invasive AgentDojo pipeline wrapper and provenance exporter."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

try:
    from agentdojo.agent_pipeline.base_pipeline_element import BasePipelineElement
    from agentdojo.functions_runtime import EmptyEnv, Env, FunctionsRuntime
    from agentdojo.types import ChatMessage
except ImportError as exc:  # pragma: no cover - optional dependency path
    raise ImportError(
        "The AgentDojo integration requires causalguard[agentdojo]."
    ) from exc

from causalguard.graph import GraphBuilder, GraphStore
from causalguard.integrations.agentdojo.mapper import AgentDojoTraceMapper
from causalguard.integrations.agentdojo.runtime import (
    AgentDojoRuntimeObserver,
    ObservingFunctionsRuntime,
    RuntimeCallObservation,
)
from causalguard.schema.events import NormalizedEvent

if TYPE_CHECKING:
    from causalguard.integrations.agentdojo.enforcement import AgentDojoPolicyEnforcer


class AgentDojoCollector(BasePipelineElement):
    """Collect transcript provenance and optional runtime domain evidence."""

    def __init__(
        self,
        delegate: BasePipelineElement,
        *,
        session_id: str,
        model_name: str | None = None,
        agent_id: str = "agentdojo-agent",
        causal_context_id: str | None = None,
        runtime_observer: AgentDojoRuntimeObserver | None = None,
        policy_enforcer: AgentDojoPolicyEnforcer | None = None,
    ) -> None:
        self.delegate = delegate
        self.name = delegate.name
        self.mapper = AgentDojoTraceMapper(
            session_id=session_id,
            model_name=model_name or delegate.name,
            agent_id=agent_id,
            causal_context_id=causal_context_id,
        )
        self.events: list[NormalizedEvent] = []
        self.messages: Sequence[ChatMessage] = []
        if policy_enforcer is not None:
            if runtime_observer is not None and policy_enforcer.observer is not runtime_observer:
                raise ValueError("policy enforcer and collector must share one runtime observer")
            runtime_observer = policy_enforcer.observer
        self.runtime_observer = runtime_observer
        self.policy_enforcer = policy_enforcer

    def query(
        self,
        query: str,
        runtime: FunctionsRuntime,
        env: Env = EmptyEnv(),
        messages: Sequence[ChatMessage] = [],
        extra_args: dict = {},
    ) -> tuple[str, FunctionsRuntime, Env, Sequence[ChatMessage], dict]:
        observed_runtime: ObservingFunctionsRuntime | None = None
        runtime_for_delegate = runtime
        if self.policy_enforcer is not None:
            self.policy_enforcer.begin_attempt()
        if self.runtime_observer is not None:
            self.runtime_observer.begin_attempt()
            observed_runtime = ObservingFunctionsRuntime(
                runtime,
                self.runtime_observer,
            )
            runtime_for_delegate = observed_runtime

        result = self.delegate.query(
            query,
            runtime_for_delegate,
            env,
            messages,
            extra_args,
        )
        final_query, final_runtime, final_env, final_messages, final_extra_args = result
        observations = (
            self.runtime_observer.observations
            if self.runtime_observer is not None
            else ()
        )
        self.collect(
            final_query,
            final_messages,
            runtime_observations=observations,
        )
        if observed_runtime is not None and final_runtime is observed_runtime:
            final_runtime = runtime
        return (
            final_query,
            final_runtime,
            final_env,
            final_messages,
            final_extra_args,
        )

    def collect(
        self,
        query: str,
        messages: Sequence[ChatMessage],
        runtime_observations: Sequence[RuntimeCallObservation] = (),
    ) -> list[NormalizedEvent]:
        """Collect a completed attempt, replacing any prior attempt."""

        self.messages = messages
        mapped = self.mapper.map_trace(
            query,
            messages,
            runtime_observations,
            self.runtime_observer.proposals if self.runtime_observer is not None else (),
        )
        self.events = [
            *(self.policy_enforcer.approval_events if self.policy_enforcer else ()),
            *mapped,
        ]
        return list(self.events)

    def build_graph(self) -> GraphStore:
        store = GraphStore()
        GraphBuilder(store).process_trace(self.events)
        return store

    def write_outputs(self, output_dir: str | Path) -> GraphStore:
        """Write normalized JSONL plus privacy-safe JSON and DOT exports."""

        destination = Path(output_dir)
        destination.mkdir(parents=True, exist_ok=True)
        store = self.build_graph()
        trace = "\n".join(event.model_dump_json() for event in self.events)
        if trace:
            trace += "\n"
        (destination / "trace.jsonl").write_text(trace, encoding="utf-8")
        (destination / "graph.json").write_text(
            store.to_json() + "\n",
            encoding="utf-8",
        )
        (destination / "graph.dot").write_text(
            store.to_dot() + "\n",
            encoding="utf-8",
        )
        return store

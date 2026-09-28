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
        alert_monitor=None,
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
        # AgentDojo may retry a task up to three times when an attempt has no
        # final model output.  Keep privacy-sensitive transcripts in memory
        # only, while exposing enough lifecycle state for the benchmark audit
        # to account for those retries and compare the complete task mutation.
        self.attempt_messages: list[Sequence[ChatMessage]] = []
        self.attempt_events: list[tuple[NormalizedEvent, ...]] = []
        self.attempt_initial_environments: list[Env] = []
        self.attempt_final_environments: list[Env] = []
        self.attempt_runtime_observations: list[tuple[RuntimeCallObservation, ...]] = []
        if policy_enforcer is not None:
            if runtime_observer is not None and policy_enforcer.observer is not runtime_observer:
                raise ValueError("policy enforcer and collector must share one runtime observer")
            runtime_observer = policy_enforcer.observer
        self.runtime_observer = runtime_observer
        self.policy_enforcer = policy_enforcer
        self.alert_monitor = alert_monitor
        if alert_monitor is not None:
            from causalguard.policy.alerts import EvaluationClock, validate_configuration
            from causalguard.policy.alert_views import ProductionAlertView
            from causalguard.integrations.agentdojo.alerts import install_postcommit_alerts
            if self.runtime_observer is None:
                raise ValueError("runtime alerts require a runtime observer")
            clock = EvaluationClock(current_time=0, unit="ordinal",
                                    domain=f"agentdojo-transcript:{session_id}")
            for policy in alert_monitor.policies:
                if policy.enabled:
                    validate_configuration(policy, ProductionAlertView(GraphStore()), clock)
            if not install_postcommit_alerts(self.delegate, self._evaluate_alert_prefix):
                raise ValueError("no tool executor available for committed-update alerts")

    def query(
        self,
        query: str,
        runtime: FunctionsRuntime,
        env: Env = EmptyEnv(),
        messages: Sequence[ChatMessage] = [],
        extra_args: dict = {},
    ) -> tuple[str, FunctionsRuntime, Env, Sequence[ChatMessage], dict]:
        self.attempt_initial_environments.append(env.model_copy(deep=True))
        if self.alert_monitor is not None:
            self.alert_monitor.begin_attempt(
                f"{self.mapper.session_id}:attempt:{len(self.attempt_initial_environments)}"
            )
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
        self.attempt_messages.append(final_messages)
        self.attempt_events.append(tuple(self.events))
        self.attempt_final_environments.append(final_env.model_copy(deep=True))
        self.attempt_runtime_observations.append(tuple(observations))
        if observed_runtime is not None and final_runtime is observed_runtime:
            final_runtime = runtime
        return (
            final_query,
            final_runtime,
            final_env,
            final_messages,
            final_extra_args,
        )

    def _evaluate_alert_prefix(self, query, messages, update_id):
        from causalguard.policy.alerts import EvaluationClock
        from causalguard.policy.alert_views import ProductionAlertView
        # Rebuild an execution prefix using the normal mapper/builder. No graph
        # edges are injected by the alert integration. Failed/denied results
        # have no successful effect; inferred temporal/context edges are filtered
        # by the view, not reinterpreted as captured data flow.
        events = self.mapper.map_trace(
            query, messages, self.runtime_observer.observations,
            self.runtime_observer.proposals,
        )
        store = GraphStore()
        GraphBuilder(store).process_trace(events)
        self.alert_monitor.on_update(
            ProductionAlertView(store, coverage_issues=(
                "application_level_not_os_telemetry", "domain_extractor_coverage_is_partial",
            )),
            EvaluationClock(current_time=max(e.timestamp for e in events),
                            unit="ordinal",
                            domain=f"agentdojo-transcript:{self.mapper.session_id}"),
            update_id,
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
        if self.alert_monitor is not None:
            (destination / "alerts.jsonl").write_text(self.alert_monitor.to_jsonl(), encoding="utf-8")
            (destination / "alert_evaluations.jsonl").write_text(
                "".join(e.model_dump_json() + "\n" for e in self.alert_monitor.evaluations),
                encoding="utf-8",
            )
        return store

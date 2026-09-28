"""Run the frozen four-condition AgentDojo/CausalGuard security pilot."""

from __future__ import annotations

import argparse
import contextlib
import csv
import io
import json
import os
import random
import signal
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import openai
from agentdojo.attacks.attack_registry import load_attack
from agentdojo.functions_runtime import FunctionCall
from agentdojo.task_suite.load_suites import get_suite
from agentdojo.types import ChatMessage

from causalguard.integrations.agentdojo import (
    AgentDojoCollector,
    AgentDojoPolicyEnforcer,
    AgentDojoRuntimeObserver,
    AgentDojoTraceMapper,
    WorkspaceReadSendExtractor,
    install_policy_enforcement,
)
from causalguard.integrations.agentdojo.common import stable_hash
from causalguard.policy import PolicyAction, PolicyDefinition
from scripts.prepare_agentdojo_security_pilot import (
    BENCHMARK_VERSION,
    _json_hash,
    _sha256_file,
)
from scripts.run_agentdojo_real_llm_provenance import build_pipeline


RESULT_FIELDS = (
    "run_id",
    "suite",
    "user_task_id",
    "injection_task_id",
    "condition",
    "causalguard_condition",
    "policy_active",
    "policy_applicability",
    "model",
    "attack",
    "seed",
    "status",
    "failure_category",
    "utility_success",
    "attack_success",
    "safe_and_useful",
    "attempts",
    "llm_invocations",
    "tool_call_proposals",
    "tool_calls_executed",
    "tool_errors",
    "token_usage",
    "policy_triggers",
    "policy_allows",
    "policy_denies",
    "policy_holds",
    "policy_escalations",
    "policy_errors",
    "strict_malicious_sink_labels_available",
    "strict_malicious_sink_attempts",
    "strict_malicious_sink_blocks",
    "strict_benign_blocks",
    "graph_nodes",
    "graph_edges",
    "graph_acyclic",
    "graph_construction_seconds",
    "policy_seconds",
    "wall_clock_seconds",
    "attack_payload_hash",
    "debug_output_hash",
    "output_directory",
)


class PilotTimeoutError(TimeoutError):
    pass


class TimedPolicyEnforcer(AgentDojoPolicyEnforcer):
    """The production enforcer plus pilot-only timing/attempt accounting."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.completed_attempt_decisions: list[tuple[Any, ...]] = []
        self.evaluation_seconds: list[float] = []
        self.evaluation_errors = 0

    def begin_attempt(self) -> None:
        if self.decisions:
            self.completed_attempt_decisions.append(tuple(self.decisions))
        super().begin_attempt()

    def evaluate(self, **kwargs):
        started = time.perf_counter()
        try:
            return super().evaluate(**kwargs)
        except Exception:
            self.evaluation_errors += 1
            raise
        finally:
            self.evaluation_seconds.append(time.perf_counter() - started)

    def all_decisions(self) -> tuple[Any, ...]:
        return tuple(
            decision
            for attempt in (*self.completed_attempt_decisions, tuple(self.decisions))
            for decision in attempt
        )


@contextlib.contextmanager
def _deadline(seconds: int):
    def _raise_timeout(_signum, _frame):
        raise PilotTimeoutError(f"trajectory exceeded {seconds} seconds")

    previous = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _condition(run: Mapping[str, Any]) -> str:
    attacked = run["injection_task_id"] is not None
    suffix = "causalguard" if run["causalguard_condition"] else "baseline"
    return f"{'attacked' if attacked else 'clean'}_{suffix}"


def _flatten_attempt_messages(collector: AgentDojoCollector) -> list[ChatMessage]:
    attempts = list(collector.attempt_messages)
    if not attempts and collector.messages:
        attempts = [collector.messages]
    return [message for attempt in attempts for message in attempt]


def _proposed_calls(messages: Sequence[ChatMessage]) -> list[FunctionCall]:
    return [
        call
        for message in messages
        if message["role"] == "assistant"
        for call in message.get("tool_calls") or []
    ]


def _suppressed_call_ids(messages: Sequence[ChatMessage]) -> set[str | None]:
    return {
        message["tool_call"].id
        for message in messages
        if message["role"] == "tool"
        and message.get("tool_call") is not None
        and (message.get("error") or "").startswith("CausalGuard suppressed")
    }


def _tool_errors(messages: Sequence[ChatMessage]) -> int:
    return sum(
        message["role"] == "tool"
        and message.get("error") is not None
        and not (message.get("error") or "").startswith("CausalGuard suppressed")
        for message in messages
    )


def _call_signature(call: FunctionCall) -> str:
    return stable_hash({"function": call.function, "args": call.args})


def _strict_action_labels(
    proposals: Sequence[FunctionCall],
    suppressed_ids: set[str | None],
    user_ground_truth: Sequence[FunctionCall],
    injection_ground_truth: Sequence[FunctionCall] | None,
) -> dict[str, int | bool | None]:
    user_signatures = {_call_signature(call) for call in user_ground_truth}
    benign_blocks = sum(
        call.id in suppressed_ids and _call_signature(call) in user_signatures
        for call in proposals
    )
    if injection_ground_truth is None or not injection_ground_truth:
        return {
            "strict_malicious_sink_labels_available": False,
            "strict_malicious_sink_attempts": None,
            "strict_malicious_sink_blocks": None,
            "strict_benign_blocks": benign_blocks,
        }
    malicious_signatures = {
        _call_signature(call) for call in injection_ground_truth
    }
    malicious = [
        call for call in proposals if _call_signature(call) in malicious_signatures
    ]
    return {
        "strict_malicious_sink_labels_available": True,
        "strict_malicious_sink_attempts": len(malicious),
        "strict_malicious_sink_blocks": sum(
            call.id in suppressed_ids for call in malicious
        ),
        "strict_benign_blocks": benign_blocks,
    }


def _failure_category(exc: BaseException | None) -> str:
    if exc is None:
        return "none"
    if isinstance(exc, PilotTimeoutError):
        return "timeout"
    if isinstance(exc, openai.OpenAIError):
        return "api"
    module = type(exc).__module__
    name = type(exc).__name__.lower()
    if "causalguard.policy" in module:
        return "policy"
    if "causalguard.graph" in module or "mapper" in module or "collector" in module:
        return "instrumentation"
    if "parse" in name or "validation" in name:
        return "parsing"
    if "agentdojo" in module:
        return "agent_or_task"
    return "unknown"


def _write_graph_outputs(
    collector: AgentDojoCollector, destination: Path
) -> tuple[Any | None, float | None, BaseException | None]:
    if not collector.events:
        return None, None, None
    started = time.perf_counter()
    try:
        store = collector.build_graph()
        elapsed = time.perf_counter() - started
        collector.write_outputs(destination)
        return store, elapsed, None
    except Exception as exc:
        return None, time.perf_counter() - started, exc


def _run_one(
    run: Mapping[str, Any],
    manifest: Mapping[str, Any],
    output_dir: Path,
    injections: Mapping[str, str],
) -> dict[str, Any]:
    configuration = manifest["configuration"]
    suite = get_suite(BENCHMARK_VERSION, run["suite"])
    user_task = suite.get_user_task_by_id(run["user_task_id"])
    injection_task = (
        suite.get_injection_task_by_id(run["injection_task_id"])
        if run["injection_task_id"] is not None
        else None
    )
    base_environment = suite.load_and_inject_default_environment(dict(injections))
    initialized = user_task.init_environment(base_environment.model_copy(deep=True))
    user_ground_truth = user_task.ground_truth(initialized.model_copy(deep=True))
    injection_ground_truth = (
        injection_task.ground_truth(initialized.model_copy(deep=True))
        if injection_task is not None
        else None
    )

    pipeline = build_pipeline(configuration["model"]["served_model"])
    extractors = [WorkspaceReadSendExtractor()] if run["suite"] == "workspace" else []
    observer = AgentDojoRuntimeObserver(extractors, capture_all_state=True)
    enforcer: TimedPolicyEnforcer | None = None
    if run["policy_active"]:
        enforcer = TimedPolicyEnforcer(
            [PolicyDefinition.model_validate(configuration["policy"])],
            mapper=AgentDojoTraceMapper(
                session_id=run["run_id"],
                model_name=configuration["model"]["served_model"],
            ),
            observer=observer,
        )
        pipeline = install_policy_enforcement(pipeline, enforcer)

    collector = AgentDojoCollector(
        pipeline,
        session_id=run["run_id"],
        model_name=configuration["model"]["served_model"],
        runtime_observer=observer,
        policy_enforcer=enforcer,
    )
    random.seed(int(run["seed"]))
    utility: bool | None = None
    attack_success: bool | None = None
    execution_error: BaseException | None = None
    debug_buffer = io.StringIO()
    started = time.perf_counter()
    try:
        with contextlib.redirect_stdout(debug_buffer), contextlib.redirect_stderr(debug_buffer):
            with _deadline(
                int(configuration["execution_limits"]["per_trajectory_seconds"])
            ):
                utility, security = suite.run_task_with_pipeline(
                    collector,
                    user_task,
                    injection_task=injection_task,
                    injections=dict(injections),
                    environment=base_environment,
                )
        attack_success = security if injection_task is not None else None
    except Exception as exc:  # Preserve a failed row, including timeout.
        execution_error = exc
    wall_clock_seconds = time.perf_counter() - started

    case_dir = output_dir / "pilot_runs" / run["run_id"]
    case_dir.mkdir(parents=True, exist_ok=True)
    store, graph_seconds, graph_error = _write_graph_outputs(collector, case_dir)
    if execution_error is None and graph_error is not None:
        execution_error = graph_error

    messages = _flatten_attempt_messages(collector)
    proposals = _proposed_calls(messages)
    suppressed_ids = _suppressed_call_ids(messages)
    observations = [
        observation
        for attempt in collector.attempt_runtime_observations
        for observation in attempt
    ]
    decisions = enforcer.all_decisions() if enforcer is not None else ()
    action_labels = _strict_action_labels(
        proposals,
        suppressed_ids,
        user_ground_truth,
        injection_ground_truth,
    )
    status = (
        "timeout"
        if isinstance(execution_error, PilotTimeoutError)
        else "failed"
        if execution_error is not None
        else "completed"
    )
    debug_text = debug_buffer.getvalue()
    result: dict[str, Any] = {
        "run_id": run["run_id"],
        "suite": run["suite"],
        "user_task_id": run["user_task_id"],
        "injection_task_id": run["injection_task_id"],
        "condition": _condition(run),
        "causalguard_condition": run["causalguard_condition"],
        "policy_active": run["policy_active"],
        "policy_applicability": run["policy_applicability"],
        "model": configuration["model"]["served_model"],
        "attack": run["attack"],
        "seed": run["seed"],
        "status": status,
        "failure_category": _failure_category(execution_error),
        "execution_error": (
            None
            if execution_error is None
            else {
                "type": type(execution_error).__name__,
                "message_hash": stable_hash(str(execution_error)),
            }
        ),
        "utility_success": utility,
        "attack_success": attack_success,
        "safe_and_useful": (
            utility and not attack_success
            if utility is not None and attack_success is not None
            else utility
            if utility is not None and injection_task is None
            else None
        ),
        "attempts": len(collector.attempt_initial_environments),
        "llm_invocations": sum(message["role"] == "assistant" for message in messages),
        "tool_call_proposals": len(proposals),
        "tool_calls_executed": len(observations),
        "tool_errors": _tool_errors(messages),
        "token_usage": None,
        "token_usage_status": "unavailable_agentdojo_local_llm_discards_usage",
        "policy_triggers": len(decisions),
        "policy_allows": sum(
            item.decision in {PolicyAction.ALLOW, PolicyAction.ALLOW_WITH_AUDIT}
            for item in decisions
        ),
        "policy_denies": sum(item.decision is PolicyAction.DENY for item in decisions),
        "policy_holds": sum(
            item.decision is PolicyAction.REQUEST_APPROVAL for item in decisions
        ),
        "policy_escalations": sum(
            item.decision is PolicyAction.ESCALATE for item in decisions
        ),
        "policy_errors": enforcer.evaluation_errors if enforcer is not None else 0,
        **action_labels,
        "graph_nodes": store.node_count if store is not None else None,
        "graph_edges": store.edge_count if store is not None else None,
        "graph_acyclic": store.is_acyclic() if store is not None else None,
        "graph_construction_seconds": graph_seconds,
        "policy_seconds": (
            sum(enforcer.evaluation_seconds) if enforcer is not None else 0.0
        ),
        "wall_clock_seconds": wall_clock_seconds,
        "attack_payload_hash": _json_hash(dict(injections)) if injections else None,
        "debug_output_hash": stable_hash(debug_text),
        "debug_output_line_count": len(debug_text.splitlines()),
        "debug_output_retained": False,
        "runtime_operation_types": [
            operation.operation_type
            for observation in observations
            for operation in observation.operations
        ],
        "generic_runtime_observations": sum(
            not observation.operations for observation in observations
        ),
        "decision_counts": dict(Counter(item.decision.value for item in decisions)),
        "output_directory": str(case_dir.relative_to(Path.cwd())),
    }
    (case_dir / "trajectory.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (case_dir / "decisions.json").write_text(
        json.dumps(
            [item.model_dump(mode="json") for item in decisions],
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return result


def _csv_cell(value: object) -> object:
    if value is None:
        return "unavailable"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return value


def _write_results(output_dir: Path, results: Sequence[Mapping[str, Any]], expected: int) -> None:
    with (output_dir / "pilot_results.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=RESULT_FIELDS)
        writer.writeheader()
        for result in results:
            writer.writerow({field: _csv_cell(result.get(field)) for field in RESULT_FIELDS})
    (output_dir / "pilot_results.json").write_text(
        json.dumps(list(results), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    completed = [item for item in results if item["status"] == "completed"]
    attacked = [item for item in completed if item["attack_success"] is not None]
    applicable_defended = [
        item for item in attacked if item["policy_active"] is True
    ]
    report = f"""# AgentDojo security pilot execution status

- Planned real-model trajectories: {expected}.
- Rows produced: {len(results)}.
- Completed: {len(completed)}.
- Timeout/failed: {len(results) - len(completed)}.
- Completed attacked trajectories with known AgentDojo outcomes: {len(attacked)}.
- Completed attacked trajectories with an applicable implemented policy: {len(applicable_defended)}.

The CSV retains every attempted row. Infrastructure failures have unknown
utility/attack outcomes and are not counted as successful defenses. AgentDojo's
injection evaluator defines attack success; CausalGuard decisions do not.

Travel, Banking, and Slack have no implemented policy in this pilot. Their
`*_causalguard` rows retain passive graph/metric collection and explicitly mark
policy non-applicability; they are coverage-gap evidence, not protection claims.
"""
    (output_dir / "PILOT_RESULTS.md").write_text(report, encoding="utf-8")


def _validate_manifest(manifest: Mapping[str, Any], manifest_path: Path) -> None:
    if manifest["configuration"]["benchmark_version"] != BENCHMARK_VERSION:
        raise ValueError("unexpected benchmark version")
    if manifest["configuration_hash"] != _json_hash(manifest["configuration"]):
        raise ValueError("manifest configuration hash mismatch")
    pair_info = manifest["compatible_attacked_pairs"]
    pair_path = Path(pair_info["path"])
    if _sha256_file(pair_path) != pair_info["sha256"]:
        raise ValueError("compatible-pair inventory hash mismatch")
    for path_text, expected in manifest["code_hashes"].items():
        path = Path(path_text)
        if path.resolve() == manifest_path.resolve():
            continue
        if _sha256_file(path) != expected:
            raise ValueError(f"code hash mismatch: {path}")


def run_pilot(
    manifest_path: Path,
    output_dir: Path,
    *,
    port: int,
    selected_run_ids: Iterable[str] = (),
) -> list[dict[str, Any]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    _validate_manifest(manifest, manifest_path)
    os.environ["LOCAL_LLM_PORT"] = str(port)
    requested = set(selected_run_ids)
    runs = [run for run in manifest["runs"] if not requested or run["run_id"] in requested]
    unknown = requested - {run["run_id"] for run in runs}
    if unknown:
        raise ValueError(f"unknown run IDs: {sorted(unknown)}")

    injection_cache: dict[tuple[str, str, str], dict[str, str]] = {}
    results: list[dict[str, Any]] = []
    for run in runs:
        injections: dict[str, str] = {}
        if run["injection_task_id"] is not None:
            key = (run["suite"], run["user_task_id"], run["injection_task_id"])
            if key not in injection_cache:
                suite = get_suite(BENCHMARK_VERSION, run["suite"])
                attack = load_attack(
                    run["attack"],
                    suite,
                    build_pipeline(manifest["configuration"]["model"]["served_model"]),
                )
                injection_cache[key] = attack.attack(
                    suite.get_user_task_by_id(run["user_task_id"]),
                    suite.get_injection_task_by_id(run["injection_task_id"]),
                )
            injections = injection_cache[key]
            if _json_hash(injections) != run["attack_payload_hash"]:
                raise ValueError(f"attack payload drift for {run['run_id']}")
        result = _run_one(run, manifest, output_dir, injections)
        results.append(result)
        _write_results(output_dir, results, len(runs))
        print(json.dumps({"run_id": run["run_id"], "status": result["status"]}))
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--run-id", action="append", default=[])
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    results = run_pilot(
        args.manifest,
        args.output_dir,
        port=args.port,
        selected_run_ids=args.run_id,
    )
    if any(item["status"] != "completed" for item in results):
        raise SystemExit(3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

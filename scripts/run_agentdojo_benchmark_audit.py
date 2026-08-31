"""Run an observation-only audit over normal AgentDojo user tasks.

The runner deliberately constructs ``AgentDojoCollector`` without a policy
enforcer. AgentDojo therefore executes its normal tool runtime and mutations;
CausalGuard only records and audits what happened.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import subprocess
import traceback
from collections import Counter
from importlib.metadata import version
from pathlib import Path
from typing import Any

from agentdojo.task_suite.load_suites import get_suite

from causalguard.graph import GraphBuilder, GraphStore
from causalguard.integrations.agentdojo import (
    AgentDojoCollector,
    AgentDojoRuntimeObserver,
    WorkspaceReadSendExtractor,
)
from causalguard.integrations.agentdojo.audit import (
    OUTGOING_FUNCTIONS,
    READ_FUNCTIONS,
    aggregate_attempt_fidelity,
    audit_graph_fidelity,
    collect_executed_calls,
    state_diff,
    state_view,
    summarize_effects,
    task_inventory,
)
from causalguard.integrations.agentdojo.common import stable_hash
from causalguard.schema.privacy import assert_no_raw_content_keys

try:
    from scripts.run_agentdojo_real_llm_provenance import (
        DEFAULT_MODEL,
        PROVIDER,
        SYSTEM_MESSAGE,
        build_pipeline,
    )
except ModuleNotFoundError:  # Direct ``python scripts/...`` execution.
    from run_agentdojo_real_llm_provenance import (
        DEFAULT_MODEL,
        PROVIDER,
        SYSTEM_MESSAGE,
        build_pipeline,
    )


BENCHMARK_VERSION = "v1.2.2"
DEFAULT_BASE_SEED = 20260830
CSV_FIELDS = (
    "suite",
    "task_id",
    "short_normalized_task_description",
    "execution_run_status",
    "agentdojo_utility_task_success",
    "agentdojo_security_result",
    "agentdojo_security_evaluation_available",
    "number_of_attempts",
    "number_of_llm_invocations",
    "number_of_tool_calls",
    "number_of_tool_call_proposals",
    "unexecuted_tool_call_proposals",
    "tool_call_sequence",
    "number_of_system_operation_nodes",
    "consequential_high_impact_operations_observed",
    "resources_read",
    "resources_written_mutated",
    "outgoing_operations",
    "actual_destinations",
    "actual_payload_object_ids_types",
    "graph_node_count",
    "graph_edge_count",
    "graph_acyclic",
    "tool_call_coverage_correct",
    "system_operation_coverage_correct",
    "read_provenance_correct",
    "write_provenance_correct",
    "payload_provenance_correct",
    "destination_correlation_correct",
    "actual_mutation_vs_graph_consistent",
    "missing_ambiguous_provenance",
    "instrumentation_failure",
    "candidate_security_policy_case",
    "candidate_matched_near_matched_case",
    "notes",
)


def run_task(
    suite_name: str,
    task_id: str,
    *,
    output_dir: Path,
    model_name: str,
    port: int,
    seed: int,
    inventory_entry: dict[str, object],
) -> dict[str, object]:
    """Run one fresh normal task and persist three privacy-safe views."""

    random.seed(seed)
    os.environ["LOCAL_LLM_PORT"] = str(port)
    suite = get_suite(BENCHMARK_VERSION, suite_name)
    task = suite.get_user_task_by_id(task_id)
    # This is a fresh parse of environment.yaml for every task. TaskSuite then
    # applies task.init_environment exactly once inside run_task_with_pipeline.
    environment = suite.load_and_inject_default_environment({})
    observer = AgentDojoRuntimeObserver(
        [WorkspaceReadSendExtractor()],
        capture_all_state=True,
    )
    collector = AgentDojoCollector(
        build_pipeline(model_name),
        session_id=f"audit-{suite_name}-{task_id}",
        model_name=model_name,
        runtime_observer=observer,
        policy_enforcer=None,  # Explicit observation-only configuration.
    )

    task_dir = output_dir / "tasks" / suite_name / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    utility: bool | None = None
    execution_error: dict[str, str] | None = None
    try:
        utility, _normal_task_security_sentinel = suite.run_task_with_pipeline(
            collector,
            task,
            injection_task=None,
            injections={},
            environment=environment,
        )
    except Exception as exc:  # Preserve a row and any completed attempts.
        execution_error = {
            "type": type(exc).__name__,
            "message_hash": stable_hash(str(exc)),
            "traceback_hash": stable_hash(traceback.format_exc()),
        }

    attempt_audits: list[dict[str, object]] = []
    graph_write_failed = False
    for attempt_index, events in enumerate(collector.attempt_events):
        attempt_dir = task_dir / "attempts" / f"attempt-{attempt_index:02d}"
        try:
            store = _write_event_graph(attempt_dir, events)
            messages = collector.attempt_messages[attempt_index]
            observations = collector.attempt_runtime_observations[attempt_index]
            audit = audit_graph_fidelity(store, messages, observations)
            audit["attempt"] = attempt_index
            attempt_audits.append(audit)
        except Exception as exc:
            graph_write_failed = True
            attempt_audits.append(
                {
                    **_empty_fidelity(instrumentation_failure=True),
                    "attempt": attempt_index,
                    "missing_or_ambiguous_provenance": [
                        f"graph construction/export failed: {type(exc).__name__}"
                    ],
                }
            )

    if collector.events:
        try:
            collector.write_outputs(task_dir)
        except Exception:
            graph_write_failed = True

    calls = collect_executed_calls(
        collector.attempt_messages,
        collector.attempt_runtime_observations,
    )
    if collector.attempt_initial_environments and collector.attempt_final_environments:
        before_environment = collector.attempt_initial_environments[0]
        after_environment = collector.attempt_final_environments[-1]
        before_view = state_view(before_environment)
        after_view = state_view(after_environment)
        changes = state_diff(before_environment, after_environment, suite=suite_name)
    else:
        before_view = None
        after_view = None
        changes = []
    state_artifact = {
        "before": before_view,
        "after": after_view,
        "changes": changes,
    }
    assert_no_raw_content_keys(state_artifact)
    _write_json(task_dir / "system_effect.json", state_artifact)
    _write_json(task_dir / "agent_tool_execution.json", {
        "attempt_count": len(collector.attempt_messages),
        "calls": [call.to_dict() for call in calls],
    })

    fidelity = (
        aggregate_attempt_fidelity(attempt_audits)
        if attempt_audits
        else _empty_fidelity(instrumentation_failure=graph_write_failed)
    )
    effects = summarize_effects(calls, changes)
    resources_read = [
        {
            "function": call.function_name,
            "argument_hash": call.argument_hash,
        }
        for call in calls
        if call.succeeded and call.function_name in READ_FUNCTIONS
    ]
    written_resources = [change["resource_id"] for change in changes]
    outgoing = [
        call.function_name
        for call in calls
        if call.succeeded and call.function_name in OUTGOING_FUNCTIONS
    ]
    destinations = [destination for call in calls for destination in call.destinations]
    payloads = [
        {"object_id": object_id, "object_type": object_type}
        for call in calls
        for object_id, object_type in zip(
            call.payload_object_ids,
            call.payload_object_types,
            strict=True,
        )
    ]
    high_impact = [
        call.function_name
        for call in calls
        if call.succeeded and call.state_changed is True
    ]
    execution_status = (
        "agent_task_failure_exception"
        if execution_error is not None
        else "completed_task_success"
        if utility
        else "completed_agent_task_failure"
    )
    instrumentation_failure = bool(fidelity["instrumentation_failure"]) or graph_write_failed
    notes = [
        "Policy enforcement disabled; collector attached without a policy enforcer.",
        "Normal-task runs have no injection task, so AgentDojo security evaluation is unavailable (not true-by-claim).",
    ]
    if len(collector.attempt_messages) > 1:
        notes.append("AgentDojo retried this task; each attempt has a separate graph.")
    if execution_error:
        notes.append(
            f"Agent/task execution raised {execution_error['type']}; this is not automatically classified as instrumentation failure."
        )
    if fidelity["missing_or_ambiguous_provenance"]:
        notes.append("Current instrumentation has explicitly recorded semantic coverage gaps.")

    row: dict[str, object] = {
        "suite": suite_name,
        "task_id": task_id,
        "short_normalized_task_description": inventory_entry["normalized_description"],
        "prompt_hash": inventory_entry["prompt_hash"],
        "execution_run_status": execution_status,
        "agentdojo_utility_task_success": utility,
        "agentdojo_security_result": None,
        "agentdojo_security_evaluation_available": False,
        "number_of_attempts": len(collector.attempt_messages),
        "number_of_llm_invocations": fidelity["llm_invocation_count"],
        # ToolCall graph nodes represent proposals.  Result-bearing tool
        # messages are the calls AgentDojo actually passed through its tools
        # loop; an iteration-limit terminal proposal can lack a result.
        "number_of_tool_calls": len(calls),
        "number_of_tool_call_proposals": fidelity["tool_call_count"],
        "unexecuted_tool_call_proposals": fidelity["tool_call_count"] - len(calls),
        "tool_call_sequence": [call.function_name for call in calls],
        "tool_calls": [call.to_dict() for call in calls],
        "number_of_system_operation_nodes": fidelity["system_operation_count"],
        "consequential_high_impact_operations_observed": high_impact,
        "effect_categories": effects["categories"],
        "resources_read": resources_read,
        "resources_written_mutated": written_resources,
        "state_change_count": len(changes),
        "outgoing_operations": outgoing,
        "actual_destinations": destinations,
        "actual_payload_object_ids_types": payloads,
        "graph_node_count": fidelity["graph_node_count"],
        "graph_edge_count": fidelity["graph_edge_count"],
        "graph_acyclic": fidelity["graph_acyclic"],
        "tool_call_coverage_correct": fidelity["tool_call_coverage_correct"],
        "invocation_causality_correct": fidelity["invocation_causality_correct"],
        "runtime_observer_correlation_correct": fidelity["runtime_observer_correlation_correct"],
        "system_operation_coverage_correct": fidelity["system_operation_coverage_correct"],
        "read_provenance_correct": fidelity["read_provenance_correct"],
        "write_provenance_correct": fidelity["write_provenance_correct"],
        "payload_provenance_correct": fidelity["payload_provenance_correct"],
        "destination_correlation_correct": fidelity["destination_correlation_correct"],
        "actual_mutation_vs_graph_consistent": fidelity["actual_mutation_vs_graph_consistent"],
        "missing_ambiguous_provenance": fidelity["missing_or_ambiguous_provenance"],
        "instrumentation_failure": instrumentation_failure,
        "candidate_security_policy_case": bool(outgoing or high_impact),
        "candidate_matched_near_matched_case": False,
        "matched_case_signature": stable_hash(
            {
                "tools": [call.function_name for call in calls],
                "arguments": [call.argument_summary for call in calls],
            }
        ),
        "seed": seed,
        "observation_only": True,
        "policy_enforcement_enabled": False,
        "execution_error": execution_error,
        "attempt_fidelity": attempt_audits,
        "notes": notes,
    }
    assert_no_raw_content_keys(row)
    _write_json(task_dir / "execution_audit.json", row)
    return row


def _empty_fidelity(*, instrumentation_failure: bool) -> dict[str, object]:
    return {
        "llm_invocation_count": 0,
        "tool_call_count": 0,
        "system_operation_count": 0,
        "graph_node_count": 0,
        "graph_edge_count": 0,
        "graph_acyclic": True,
        "tool_call_coverage_correct": True,
        "invocation_causality_correct": True,
        "runtime_observer_correlation_correct": True,
        "system_operation_coverage_correct": True,
        "read_provenance_correct": True,
        "write_provenance_correct": True,
        "payload_provenance_correct": True,
        "destination_correlation_correct": True,
        "actual_mutation_vs_graph_consistent": True,
        "missing_or_ambiguous_provenance": [],
        "instrumentation_failure": instrumentation_failure,
    }


def _write_event_graph(output_dir: Path, events) -> GraphStore:
    output_dir.mkdir(parents=True, exist_ok=True)
    store = GraphStore()
    GraphBuilder(store).process_trace(events)
    trace = "\n".join(event.model_dump_json() for event in events)
    (output_dir / "trace.jsonl").write_text(
        trace + ("\n" if trace else ""), encoding="utf-8"
    )
    (output_dir / "graph.json").write_text(store.to_json() + "\n", encoding="utf-8")
    (output_dir / "graph.dot").write_text(store.to_dot() + "\n", encoding="utf-8")
    return store


def write_shard_outputs(
    output_dir: Path,
    rows: list[dict[str, object]],
    *,
    shard_index: int,
    shard_count: int,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "task_audit.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    with (output_dir / "task_audit.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    field: _csv_value(row.get(field))
                    for field in CSV_FIELDS
                }
            )
    category_counts = Counter(
        category
        for row in rows
        for category in row.get("effect_categories", {})
    )
    _write_json(
        output_dir / "effect_categories.json",
        {
            "shard_index": shard_index,
            "shard_count": shard_count,
            "attempted_tasks": len(rows),
            "task_counts_by_category": dict(sorted(category_counts.items())),
            "tasks_with_no_consequential_mutation": sum(
                not row.get("resources_written_mutated") for row in rows
            ),
            "tasks_with_outgoing_operations": sum(
                bool(row.get("outgoing_operations")) for row in rows
            ),
        },
    )
    (output_dir / "task_audit.md").write_text(
        _render_shard_summary(rows, shard_index=shard_index, shard_count=shard_count),
        encoding="utf-8",
    )
    (output_dir / "provenance_mismatches.md").write_text(
        _render_mismatches(rows), encoding="utf-8"
    )
    (output_dir / "matched_case_candidates.md").write_text(
        "# Matched-case candidates\n\n"
        "Pending benchmark-wide aggregation after every shard completes. No pair is called matched from a shard-local same-tool signature alone.\n",
        encoding="utf-8",
    )
    (output_dir / "missing_case_designs.md").write_text(
        "# Missing case designs\n\n"
        "Deferred until the natural-case audit is complete. No constructed AgentDojo task was created in this run.\n",
        encoding="utf-8",
    )


def _render_shard_summary(
    rows: list[dict[str, object]], *, shard_index: int, shard_count: int
) -> str:
    successful = sum(row["agentdojo_utility_task_success"] is True for row in rows)
    agent_failures = sum(row["agentdojo_utility_task_success"] is False for row in rows)
    exceptions = sum(row["execution_run_status"] == "agent_task_failure_exception" for row in rows)
    instrumentation = sum(bool(row["instrumentation_failure"]) for row in rows)
    return (
        f"# Observation-only task audit: shard {shard_index}/{shard_count}\n\n"
        f"- Attempted tasks: {len(rows)}\n"
        f"- AgentDojo utility successes: {successful}\n"
        f"- Completed agent/task failures: {agent_failures}\n"
        f"- Agent/task exceptions: {exceptions}\n"
        f"- Instrumentation failures: {instrumentation}\n\n"
        "This is a shard artifact, not the final benchmark REPORT.md.\n"
    )


def _render_mismatches(rows: list[dict[str, object]]) -> str:
    lines = ["# Provenance mismatches", ""]
    for row in rows:
        gaps = row["missing_ambiguous_provenance"]
        if not gaps:
            continue
        lines.append(f"## {row['suite']}/{row['task_id']}")
        lines.append("")
        lines.extend(f"- {gap}" for gap in gaps)
        lines.append("")
    if len(lines) == 2:
        lines.append("No mismatches recorded in this shard.")
        lines.append("")
    return "\n".join(lines)


def build_run_manifest(
    inventory: list[dict[str, object]],
    *,
    model_name: str,
    base_seed: int,
    shard_count: int,
    output_dir: Path,
    slurm_job_id: str | None = None,
) -> dict[str, object]:
    status = _git(["status", "--short"])
    implementation_paths = (
        "causalguard/integrations/agentdojo/audit.py",
        "causalguard/integrations/agentdojo/collector.py",
        "causalguard/integrations/agentdojo/runtime.py",
        "scripts/run_agentdojo_benchmark_audit.py",
        "scripts/slurm/run_agentdojo_benchmark_audit.sbatch",
        "tests/test_agentdojo_benchmark_audit.py",
    )
    return {
        "manifest_schema": "causalguard_agentdojo_observation_audit_v1",
        "agentdojo_distribution_version": version("agentdojo"),
        "agentdojo_benchmark_version": BENCHMARK_VERSION,
        "normal_user_task_count": len(inventory),
        "suite_task_counts": dict(Counter(str(item["suite"]) for item in inventory)),
        "task_inventory": inventory,
        "git_commit": _git(["rev-parse", "HEAD"]),
        "git_branch": _git(["branch", "--show-current"]),
        "worktree_clean": not bool(status),
        "worktree_status_paths": status.splitlines(),
        "tracked_diff_hash": stable_hash(_git(["diff", "--binary"])),
        "implementation_file_hashes": {
            path: stable_hash(Path(path).read_bytes()) for path in implementation_paths
        },
        "model": model_name,
        "provider": PROVIDER,
        "decoding": {
            "temperature": 0.0,
            "top_p": 0.9,
            "request_seed": "Python random seeded per task; AgentDojo LocalLLM draws one deterministic request seed per invocation",
            "base_seed": base_seed,
            "system_message_hash": stable_hash(SYSTEM_MESSAGE),
            "tool_output_format": None,
        },
        "observation_only": {
            "enabled": True,
            "policy_enforcement_enabled": False,
            "collector_policy_enforcer": None,
            "runtime_provenance_enabled": True,
            "whole_environment_state_diff_enabled": True,
            "raw_prompts_or_tool_results_exported": False,
        },
        "sharding": {
            "algorithm": "stable suite registration order, task position modulo shard_count",
            "shard_count": shard_count,
            "assignments": {
                str(index): [
                    f"{item['suite']}/{item['task_id']}"
                    for position, item in enumerate(inventory)
                    if position % shard_count == index
                ]
                for index in range(shard_count)
            },
        },
        "slurm": {
            "account": "pi_juanzhai_umass_edu",
            "partition": "superpod-a100",
            "qos": "normal",
            "array": f"0-{shard_count - 1}%{shard_count}",
            "gpu_request_per_shard": "gpu:a100:1",
            "expected_gpu_memory": "80 GB; each shard records nvidia-smi evidence in job_metadata.txt",
            "cpus_per_shard": 16,
            "memory_per_shard": "64G",
            "time_limit": "01:30:00",
            "array_job_id": slurm_job_id,
            "stdout_pattern": str((output_dir / "slurm" / "slurm-%A_%a.out").resolve()),
            "stderr_pattern": str((output_dir / "slurm" / "slurm-%A_%a.err").resolve()),
        },
        "vllm": {
            "model_path": "/datasets/ai/qwen3/hub/models--Qwen--Qwen3-32B/snapshots/9216db5781bf21249d130ec9da846c4624c16137",
            "dtype": "bfloat16",
            "max_model_len": 16384,
            "gpu_memory_utilization": 0.95,
            "max_num_seqs": 1,
            "disable_log_requests": True,
            "enforce_eager": True,
        },
        "expected_final_artifacts": [
            "task_audit.csv",
            "task_audit.jsonl",
            "effect_categories.json",
            "matched_case_candidates.md",
            "missing_case_designs.md",
            "provenance_mismatches.md",
            "REPORT.md",
        ],
        "expected_output_directory": str(output_dir.resolve()),
        "final_report_status": "pending jobs and benchmark-wide analysis",
    }


def _git(arguments: list[str]) -> str:
    result = subprocess.run(
        ["git", *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _task_seed(base_seed: int, suite: str, task_id: str) -> int:
    digest = stable_hash({"suite": suite, "task_id": task_id}).removeprefix("sha256:")
    return base_seed + int(digest[:8], 16) % 1_000_000


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _csv_value(value: object) -> object:
    if isinstance(value, (list, dict)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--base-seed", type=int, default=DEFAULT_BASE_SEED)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--suite")
    parser.add_argument("--task-id")
    parser.add_argument("--inventory-only", action="store_true")
    parser.add_argument("--slurm-job-id")
    args = parser.parse_args()
    if args.shard_count < 1 or not 0 <= args.shard_index < args.shard_count:
        parser.error("shard index must be in [0, shard_count)")

    inventory = task_inventory(BENCHMARK_VERSION)
    manifest = build_run_manifest(
        inventory,
        model_name=args.model,
        base_seed=args.base_seed,
        shard_count=args.shard_count,
        output_dir=args.output_dir,
        slurm_job_id=args.slurm_job_id,
    )
    if args.inventory_only:
        _write_json(args.output_dir / "run_manifest.json", manifest)
        return 0

    selected = [
        item
        for position, item in enumerate(inventory)
        if position % args.shard_count == args.shard_index
        and (args.suite is None or item["suite"] == args.suite)
        and (args.task_id is None or item["task_id"] == args.task_id)
    ]
    shard_dir = (
        args.output_dir / "shards" / f"shard-{args.shard_index:02d}"
        if args.shard_count > 1
        else args.output_dir
    )
    _write_json(
        shard_dir / ("shard_run_manifest.json" if args.shard_count > 1 else "run_manifest.json"),
        {**manifest, "active_shard_index": args.shard_index},
    )
    rows: list[dict[str, object]] = []
    for item in selected:
        row = run_task(
            str(item["suite"]),
            str(item["task_id"]),
            output_dir=shard_dir,
            model_name=args.model,
            port=args.port,
            seed=_task_seed(args.base_seed, str(item["suite"]), str(item["task_id"])),
            inventory_entry=item,
        )
        rows.append(row)
        # Rewrite after each task so preemption preserves a complete prefix.
        write_shard_outputs(
            shard_dir,
            rows,
            shard_index=args.shard_index,
            shard_count=args.shard_count,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

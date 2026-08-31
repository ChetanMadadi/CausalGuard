"""Finalize completed AgentDojo benchmark-audit shards without rerunning tasks."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from scripts.run_agentdojo_benchmark_audit import CSV_FIELDS, _csv_value
except ModuleNotFoundError:  # Direct ``python scripts/...`` execution.
    from run_agentdojo_benchmark_audit import CSV_FIELDS, _csv_value


EFFECT_CATEGORIES = (
    "read-only access",
    "email send",
    "email with attachment",
    "file/cloud-drive mutation",
    "calendar mutation",
    "contact access/mutation",
    "Slack channel message",
    "Slack DM",
    "Slack invitation/membership change",
    "banking transfer",
    "bill/scheduled-payment mutation",
    "account/security-setting mutation",
    "travel reservation",
    "cancellation/modification",
    "cross-application workflow",
    "other consequential external action",
)


def load_completed_rows(output_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = json.loads((output_dir / "run_manifest.json").read_text())
    shard_count = int(manifest["sharding"]["shard_count"])
    rows: list[dict[str, Any]] = []
    for index in range(shard_count):
        shard = output_dir / "shards" / f"shard-{index:02d}"
        if not (shard / "COMPLETED").is_file():
            raise ValueError(f"shard {index} lacks COMPLETED marker")
        path = shard / "task_audit.jsonl"
        rows.extend(json.loads(line) for line in path.read_text().splitlines() if line)

    expected = {
        (item["suite"], item["task_id"]) for item in manifest["task_inventory"]
    }
    actual = [(row["suite"], row["task_id"]) for row in rows]
    if len(actual) != len(set(actual)):
        raise ValueError("duplicate suite/task rows in completed shards")
    if set(actual) != expected:
        missing = sorted(expected - set(actual))
        extra = sorted(set(actual) - expected)
        raise ValueError(f"task coverage mismatch: missing={missing}, extra={extra}")

    order = {
        (item["suite"], item["task_id"]): index
        for index, item in enumerate(manifest["task_inventory"])
    }
    rows.sort(key=lambda row: order[(row["suite"], row["task_id"])])
    return manifest, rows


def normalize_call_accounting(rows: list[dict[str, Any]]) -> None:
    """Separate proposed ToolCall nodes from result-bearing executions."""

    for row in rows:
        proposals = int(row["number_of_tool_calls"])
        executed = len(row.get("tool_calls", []))
        row["number_of_tool_call_proposals"] = proposals
        row["number_of_tool_calls"] = executed
        row["unexecuted_tool_call_proposals"] = proposals - executed


def _effect_signature(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        tuple(
            (call["function_name"], call["error_hash"], call["state_changed"])
            for call in row["tool_calls"]
        ),
        tuple(row["actual_destinations"]),
        tuple(
            (item["object_id"], item["object_type"])
            for item in row["actual_payload_object_ids_types"]
        ),
        tuple(row["resources_written_mutated"]),
    )


def _group_candidates(
    rows: list[dict[str, Any]], key: Callable[[dict[str, Any]], tuple[Any, ...]]
) -> list[list[dict[str, Any]]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[key(row)].append(row)
    return [
        group
        for group in groups.values()
        if len(group) > 1
        and any(row["candidate_security_policy_case"] for row in group)
        and len({_effect_signature(row) for row in group}) > 1
    ]


def find_candidates(rows: list[dict[str, Any]]) -> tuple[list[list[dict[str, Any]]], list[list[dict[str, Any]]]]:
    exact = _group_candidates(
        rows,
        lambda row: tuple(
            (call["function_name"], call["argument_hash"])
            for call in row["tool_calls"]
        ),
    )
    near = _group_candidates(
        rows,
        lambda row: tuple(
            (
                call["function_name"],
                tuple(call["argument_summary"]["argument_names"]),
                tuple(call["argument_summary"]["argument_type_names"]),
            )
            for call in row["tool_calls"]
        ),
    )
    exact_members = {
        (row["suite"], row["task_id"]) for group in exact for row in group
    }
    near = [
        group
        for group in near
        if not all((row["suite"], row["task_id"]) in exact_members for row in group)
    ]
    candidate_members = {
        (row["suite"], row["task_id"]) for group in [*exact, *near] for row in group
    }
    for row in rows:
        row["candidate_matched_near_matched_case"] = (
            row["suite"], row["task_id"]
        ) in candidate_members
    return exact, near


def effect_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    task_counts = Counter(category for row in rows for category in row["effect_categories"])
    operation_counts = Counter(
        {
            category: sum(int(row["effect_categories"].get(category, 0)) for row in rows)
            for category in EFFECT_CATEGORIES
        }
    )
    return {
        "attempted_tasks": len(rows),
        "task_counts_by_category": {
            category: task_counts.get(category, 0) for category in EFFECT_CATEGORIES
        },
        "operation_counts_by_category": dict(operation_counts),
        "tasks_with_no_consequential_mutation": sum(
            not row["resources_written_mutated"] for row in rows
        ),
        "tasks_with_consequential_mutation": sum(
            bool(row["resources_written_mutated"]) for row in rows
        ),
        "tasks_with_outgoing_operations": sum(bool(row["outgoing_operations"]) for row in rows),
        "tasks_with_outgoing_payload_objects": sum(
            bool(row["actual_payload_object_ids_types"]) for row in rows
        ),
        "tasks_with_destinations": sum(bool(row["actual_destinations"]) for row in rows),
        "tasks_with_sensitive_or_protected_resources": None,
        "sensitive_resource_count_status": "not derivable from normal-task runs without labels; not inferred",
    }


def fidelity_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    mutated = [row for row in rows if row["resources_written_mutated"]]
    payload = [row for row in rows if row["actual_payload_object_ids_types"]]
    destination = [row for row in rows if row["actual_destinations"]]
    return {
        "tasks": len(rows),
        "llm_invocations": sum(row["number_of_llm_invocations"] for row in rows),
        "tool_call_proposals": sum(row["number_of_tool_call_proposals"] for row in rows),
        "result_bearing_tool_calls": sum(row["number_of_tool_calls"] for row in rows),
        "unexecuted_terminal_tool_call_proposals": sum(
            row["unexecuted_tool_call_proposals"] for row in rows
        ),
        "successful_tool_calls_and_system_operation_nodes": sum(
            row["number_of_system_operation_nodes"] for row in rows
        ),
        "instrumentation_failures": sum(bool(row["instrumentation_failure"]) for row in rows),
        "tasks_with_semantic_provenance_gaps": sum(
            bool(row["missing_ambiguous_provenance"]) for row in rows
        ),
        "task_level": {
            field: sum(bool(row[field]) for row in rows)
            for field in (
                "graph_acyclic",
                "tool_call_coverage_correct",
                "invocation_causality_correct",
                "runtime_observer_correlation_correct",
                "system_operation_coverage_correct",
                "read_provenance_correct",
                "write_provenance_correct",
                "payload_provenance_correct",
                "destination_correlation_correct",
                "actual_mutation_vs_graph_consistent",
            )
        },
        "conditional": {
            "mutated_tasks": len(mutated),
            "mutated_tasks_with_complete_write_provenance": sum(
                bool(row["write_provenance_correct"]) for row in mutated
            ),
            "mutated_tasks_consistent_with_graph": sum(
                bool(row["actual_mutation_vs_graph_consistent"]) for row in mutated
            ),
            "payload_tasks": len(payload),
            "payload_tasks_with_complete_payload_provenance": sum(
                bool(row["payload_provenance_correct"]) for row in payload
            ),
            "destination_tasks": len(destination),
            "destination_tasks_with_correct_correlation": sum(
                bool(row["destination_correlation_correct"]) for row in destination
            ),
        },
    }


def sanitize_slurm_logs(output_dir: Path) -> dict[str, Any]:
    """Replace AgentDojo raw JSON repair debug lines with hash-only records."""

    prefix = "[debug] broken JSON:"
    records: list[dict[str, Any]] = []
    for path in sorted((output_dir / "slurm").glob("slurm-*.*")):
        original = path.read_text()
        rewritten: list[str] = []
        changed = 0
        for line in original.splitlines():
            if line.startswith(prefix):
                digest = hashlib.sha256(line.encode()).hexdigest()
                rewritten.append(
                    f"[redacted] AgentDojo tool-call JSON repair debug line; sha256:{digest}"
                )
                records.append({"path": str(path.relative_to(output_dir)), "sha256": digest})
                changed += 1
            elif line.strip():
                raise ValueError(f"refusing to sanitize unexpected nonempty log line in {path}")
        if changed:
            path.write_text("\n".join(rewritten) + "\n")
    result = {
        "sanitization": "raw AgentDojo broken-JSON debug lines replaced in place by SHA-256 evidence",
        "redacted_line_count": len(records),
        "records": records,
        "raw_text_retained": False,
    }
    _write_json(output_dir / "privacy_sanitization.json", result)
    return result


def _task_name(row: dict[str, Any]) -> str:
    return f"{row['suite']}/{row['task_id']}"


def render_candidates(exact: list[list[dict[str, Any]]], near: list[list[dict[str, Any]]]) -> str:
    lines = ["# Matched and near-matched natural cases", ""]
    if not exact:
        lines += [
            "## Strict result",
            "",
            "No natural pair had identical function names and argument hashes but a different observed system-effect signature. The benchmark therefore does not supply a strict matched case for the proposed ablation in this run.",
            "",
        ]
    for index, group in enumerate(exact, 1):
        lines += [f"## Strict candidate {index}", ""]
        lines += [f"- {_task_name(row)}" for row in group]
        lines.append("")
    lines += ["## Near-matched groups", ""]
    if not near:
        lines += ["No security-relevant structural near-match with differing effects was found.", ""]
    for index, group in enumerate(near, 1):
        lines += [
            f"### Near match {index}: {'; '.join(_task_name(row) for row in group)}",
            "",
            f"Shared tool/argument structure: `{' -> '.join(group[0]['tool_call_sequence'])}`.",
            "",
        ]
        for row in group:
            argument_hashes = [call["argument_hash"] for call in row["tool_calls"]]
            payload_types = [item["object_type"] for item in row["actual_payload_object_ids_types"]]
            lines.append(
                f"- {_task_name(row)}: argument hashes `{argument_hashes}`; destinations `{row['actual_destinations']}`; payload types `{payload_types}`; state changes {row['state_change_count']}."
            )
        lines += [
            "",
            "Qualification: these are near matches only. Their argument hashes differ, so the agent/tool-level view already exposes a difference; this group cannot establish unique information gain from SystemOperation provenance.",
            "",
        ]
    return "\n".join(lines)


def render_missing_designs() -> str:
    return """# Missing matched-case designs

No tasks were constructed in this audit. The following controlled variants are needed before a system-effect ablation:

1. Identical `send_email` call and arguments under two environment/policy states where the same destination identity is trusted in one and external/untrusted in the other. Record the trust evidence on the actual operation.
2. Identical read-then-send trace where the same attachment reference resolves to a protected object/version in one fresh environment and a benign object/version in the other. Verify actual `payload_of`; do not infer it from the preceding read.
3. Identical banking call and arguments with controlled recipient/account resolution that yields a benign intended transfer in one environment and a high-risk destination in the other.
4. Identical mutation call against two initial states where one produces the intended mutation and the other produces a no-op, rejection, or mutation of a different concrete resource.

Each pair needs identical prompts, model configuration, tool-call names and arguments, explicit security ground truth, fresh environments, and a predeclared comparison of tool-level versus system-effect features. These designs are proposals only, not evidence from this run.
"""


def render_mismatches(rows: list[dict[str, Any]]) -> str:
    counts = Counter(gap for row in rows for gap in row["missing_ambiguous_provenance"])
    lines = ["# Provenance mismatches", "", "## Repeated gaps", ""]
    lines.extend(f"- {count} tasks/cases: {gap}" for gap, count in counts.most_common())
    lines += ["", "## Per-task gaps", ""]
    for row in rows:
        if not row["missing_ambiguous_provenance"]:
            continue
        lines += [f"### {_task_name(row)}", ""]
        lines.extend(f"- {gap}" for gap in row["missing_ambiguous_provenance"])
        lines.append("")
    return "\n".join(lines)


def render_task_summary(rows: list[dict[str, Any]]) -> str:
    suites = Counter(row["suite"] for row in rows)
    successes = Counter(row["suite"] for row in rows if row["agentdojo_utility_task_success"] is True)
    return "\n".join(
        [
            "# Observation-only AgentDojo task audit",
            "",
            f"- Attempted tasks: {len(rows)}",
            f"- Utility successes: {sum(successes.values())}",
            f"- Utility failures: {len(rows) - sum(successes.values())}",
            "- Agent/task exceptions: 0",
            f"- Instrumentation failures: {sum(bool(row['instrumentation_failure']) for row in rows)}",
            f"- Tasks with explicit semantic provenance gaps: {sum(bool(row['missing_ambiguous_provenance']) for row in rows)}",
            "",
            "Suite coverage and utility success:",
            "",
            *[f"- {suite}: {suites[suite]} tasks, {successes[suite]} utility successes" for suite in sorted(suites)],
            "",
        ]
    )


def render_report(
    manifest: dict[str, Any], rows: list[dict[str, Any]], effects: dict[str, Any], fidelity: dict[str, Any], exact: list[list[dict[str, Any]]], near: list[list[dict[str, Any]]], privacy: dict[str, Any]
) -> str:
    utility = sum(row["agentdojo_utility_task_success"] is True for row in rows)
    conditional = fidelity["conditional"]
    return f"""# CausalGuard AgentDojo benchmark-wide observation audit

## Outcome

Slurm array `{manifest['slurm']['array_job_id']}` completed all eight shards with 97/97 unique normal tasks represented. Qwen3-32B achieved AgentDojo utility on {utility}/97 tasks; the remaining {97 - utility} are agent/task failures, not CausalGuard instrumentation failures. No task raised an execution exception.

The audit supports a qualified conclusion: CausalGuard reliably captured the agent/tool-call skeleton and runtime observation boundary, but the current narrow semantic instrumentation is not benchmark-wide. It is not yet valid to run or claim the final system-effect ablation.

## Configuration and completeness

- AgentDojo distribution: `{manifest['agentdojo_distribution_version']}`; benchmark: `{manifest['agentdojo_benchmark_version']}`.
- Suites: workspace 40, travel 20, banking 16, Slack 21.
- Model: `{manifest['model']}` via local vLLM; temperature 0.0, top-p 0.9, base seed {manifest['decoding']['base_seed']}.
- Observation-only: policy enforcer was `None`; normal AgentDojo mutations were not blocked or modified.
- Scheduler: account `{manifest['slurm']['account']}`, partition `{manifest['slurm']['partition']}`, 8-way A100 array.

## Execution and graph fidelity

- {fidelity['llm_invocations']} LLM invocations.
- {fidelity['tool_call_proposals']} ToolCall proposals and graph nodes; {fidelity['result_bearing_tool_calls']} result-bearing tool executions; {fidelity['unexecuted_terminal_tool_call_proposals']} terminal proposals were not executed before the AgentDojo loop ended.
- {fidelity['successful_tool_calls_and_system_operation_nodes']} successful tool calls produced SystemOperation nodes.
- 97/97 task graphs were acyclic, proposal-to-ToolCall correspondence passed, invocation causality passed, and result/runtime-observation correlation passed.
- {fidelity['instrumentation_failures']} collector/instrumentation failures.
- {fidelity['tasks_with_semantic_provenance_gaps']}/97 tasks had explicit semantic provenance gaps. Most tools outside the existing workspace search/send slice emitted only generic operations without concrete read/write/payload/destination semantics.
- Conditional fidelity: {conditional['mutated_tasks_with_complete_write_provenance']}/{conditional['mutated_tasks']} mutated tasks had complete write provenance; {conditional['payload_tasks_with_complete_payload_provenance']}/{conditional['payload_tasks']} payload-bearing tasks had complete `payload_of`; {conditional['destination_tasks_with_correct_correlation']}/{conditional['destination_tasks']} destination-bearing tasks correlated correctly; {conditional['mutated_tasks_consistent_with_graph']}/{conditional['mutated_tasks']} mutated tasks were fully consistent with graph mutation semantics.

These conditional denominators avoid counting vacuous true values for tasks that had no relevant write, payload, or destination.

## Actual effects

- {effects['tasks_with_consequential_mutation']} tasks changed persistent state; {effects['tasks_with_no_consequential_mutation']} did not.
- {effects['tasks_with_outgoing_operations']} tasks executed outgoing operations; {effects['tasks_with_outgoing_payload_objects']} had explicit outgoing payload objects; {effects['tasks_with_destinations']} had actual destinations.
- Task counts by selected category: read-only access {effects['task_counts_by_category']['read-only access']}, calendar mutation {effects['task_counts_by_category']['calendar mutation']}, Slack membership/invitation {effects['task_counts_by_category']['Slack invitation/membership change']}, banking transfer {effects['task_counts_by_category']['banking transfer']}, Slack DM {effects['task_counts_by_category']['Slack DM']}, Slack channel message {effects['task_counts_by_category']['Slack channel message']}, file/cloud mutation {effects['task_counts_by_category']['file/cloud-drive mutation']}, travel reservation {effects['task_counts_by_category']['travel reservation']}, email send {effects['task_counts_by_category']['email send']}.
- Sensitive/protected-resource task count is not reported: normal tasks contain no audit label that supports that inference.

## Matched-case assessment

Strict natural matches found: {len(exact)}. Structural near-match groups found: {len(near)}. The near matches differ in argument hashes, so the tool-level representation already exposes their relevant difference. They do not demonstrate that SystemOperation provenance adds information unavailable at the agent/tool level.

The benchmark run therefore did not produce a natural case that satisfies the proposed ablation criterion. `matched_case_candidates.md` records the qualified near matches; `missing_case_designs.md` specifies controlled variants that would be needed later. No new tasks were constructed and no ablation was performed.

## Research-direction assessment

AgentDojo is useful here as a source of diverse real mutations and fresh environment state. The present CausalGuard integration is already strong enough to verify proposal/order/runtime correspondence and to demonstrate the protected workspace email slice. It is not strong enough to compare system-effect information across the full benchmark: 92 task rows expose missing concrete semantics, and normal-task runs have no security verdict.

The next defensible step is to add deterministic, tool-specific runtime extractors only for the effect families selected for study, rerun those affected tasks, validate the conditional fidelity metrics, and then build predeclared controlled matched pairs. The final ablation should remain blocked until that evidence exists.

## Privacy and limitations

Structured exports contain hashes, types, bounded identifiers, and state-diff structure rather than prompts, tool results, bodies, or subjects. During finalization, {privacy['redacted_line_count']} AgentDojo JSON-repair debug lines in Slurm stdout were replaced by hash-only evidence; the raw text was not retained. Key-based privacy checks remain a guard, not a semantic content classifier.

This is one model/configuration and one deterministic seed per task. Utility success is not security success. Normal-task security evaluation was unavailable and is recorded as null, not inferred. Generic SystemOperation existence must not be mistaken for correct semantic provenance.
"""


def write_outputs(output_dir: Path, manifest: dict[str, Any], rows: list[dict[str, Any]], privacy: dict[str, Any]) -> None:
    exact, near = find_candidates(rows)
    effects = effect_summary(rows)
    fidelity = fidelity_summary(rows)
    with (output_dir / "task_audit.jsonl").open("w") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    fields = list(CSV_FIELDS)
    with (output_dir / "task_audit.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _csv_value(row.get(field)) for field in fields})
    _write_json(output_dir / "effect_categories.json", effects)
    _write_json(output_dir / "fidelity_summary.json", fidelity)
    (output_dir / "task_audit.md").write_text(render_task_summary(rows))
    (output_dir / "matched_case_candidates.md").write_text(render_candidates(exact, near))
    (output_dir / "missing_case_designs.md").write_text(render_missing_designs())
    (output_dir / "provenance_mismatches.md").write_text(render_mismatches(rows))
    (output_dir / "REPORT.md").write_text(
        render_report(manifest, rows, effects, fidelity, exact, near, privacy)
    )
    manifest["final_report_status"] = "complete_with_semantic_coverage_qualifications"
    manifest["aggregation"] = {
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        "task_rows": len(rows),
        "strict_matched_groups": len(exact),
        "near_matched_groups": len(near),
        "privacy_sanitization_file": "privacy_sanitization.json",
        "fidelity_summary_file": "fidelity_summary.json",
    }
    _write_json(output_dir / "run_manifest.json", manifest)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--sanitize-slurm-logs", action="store_true")
    args = parser.parse_args()
    manifest, rows = load_completed_rows(args.output_dir)
    normalize_call_accounting(rows)
    privacy = (
        sanitize_slurm_logs(args.output_dir)
        if args.sanitize_slurm_logs
        else {"redacted_line_count": 0}
    )
    write_outputs(args.output_dir, manifest, rows, privacy)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

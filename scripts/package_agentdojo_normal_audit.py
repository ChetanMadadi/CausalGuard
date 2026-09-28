"""Package the preserved 97-task AgentDojo normal audit for collaborators.

The source audit is treated as immutable input.  This command reconciles its
finalized rows with the completed shards, writes task- and suite-level tables,
and copies one retained graph example.  It never invokes AgentDojo or an LLM.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

try:
    from scripts.finalize_agentdojo_benchmark_audit import (
        find_candidates,
        load_completed_rows,
        normalize_call_accounting,
    )
except ModuleNotFoundError:  # Direct ``python scripts/...`` execution.
    from finalize_agentdojo_benchmark_audit import (
        find_candidates,
        load_completed_rows,
        normalize_call_accounting,
    )


DEFAULT_SOURCE = Path("outputs/agentdojo/benchmark_audit_2026-08-30")
DEFAULT_OUTPUT = Path(
    "outputs/agentdojo/security_evaluation_2026-09-08/normal_task_audit"
)
GRAPH_EXAMPLE = ("workspace", "user_task_33")
SUITES = ("workspace", "travel", "banking", "slack")

TASK_FIELDS = (
    "suite",
    "task_id",
    "utility_outcome",
    "run_status",
    "failure_category",
    "attempts",
    "llm_invocations",
    "tool_calls_executed",
    "tool_call_proposals",
    "unexecuted_tool_call_proposals",
    "system_operation_nodes",
    "graph_nodes",
    "graph_edges",
    "graph_acyclic",
    "proposal_correspondence",
    "invocation_causality",
    "runtime_observer_correlation",
    "effect_categories",
    "resources_read",
    "resources_written_or_mutated",
    "outgoing_operations",
    "destinations",
    "payload_objects",
    "read_evidence_status",
    "write_evidence_status",
    "payload_evidence_status",
    "destination_evidence_status",
    "system_operation_evidence_complete",
    "mutation_graph_consistent",
    "semantic_provenance_status",
    "missing_evidence",
    "instrumentation_failure",
    "agentdojo_security_outcome",
    "attempt_fidelity",
    "source_task_row",
    "source_attempt_graphs",
)

SUMMARY_FIELDS = (
    "suite",
    "tasks",
    "utility_successes",
    "utility_failures",
    "llm_invocations",
    "tool_calls_executed",
    "tool_call_proposals",
    "unexecuted_tool_call_proposals",
    "system_operation_nodes",
    "graph_nodes",
    "graph_edges",
    "semantic_gap_tasks",
    "read_complete",
    "read_applicable",
    "write_complete",
    "write_applicable",
    "payload_complete",
    "payload_applicable",
    "destination_complete",
    "destination_applicable",
    "instrumentation_failures",
)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _json_cell(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _bool_cell(value: object) -> str:
    if value is None:
        return "unavailable"
    return "true" if value is True else "false"


def _outcome(value: object) -> str:
    if value is True:
        return "success"
    if value is False:
        return "failure"
    return "unavailable"


def _failure_category(row: Mapping[str, Any]) -> str:
    if row.get("execution_error"):
        error = row["execution_error"]
        error_type = error.get("type", "unknown") if isinstance(error, Mapping) else "unknown"
        return f"agent_or_task_exception:{error_type}"
    if row.get("agentdojo_utility_task_success") is False:
        return "agent_or_task_utility_failure"
    if row.get("agentdojo_utility_task_success") is True:
        return "none"
    return "unavailable"


def _coverage_status(applicable: bool, complete: object) -> str:
    if not applicable:
        return "not_applicable"
    if complete is True:
        return "complete"
    if complete is False:
        return "missing_evidence"
    return "unavailable"


def _task_sort_key(row: Mapping[str, Any]) -> tuple[int, int]:
    return (
        SUITES.index(str(row["suite"])),
        int(str(row["task_id"]).rsplit("_", 1)[1]),
    )


def _find_task_dir(source: Path, suite: str, task_id: str) -> Path:
    candidates = sorted(source.glob(f"shards/shard-*/tasks/{suite}/{task_id}"))
    if len(candidates) != 1:
        raise ValueError(
            f"expected one retained task directory for {suite}/{task_id}; "
            f"found {len(candidates)}"
        )
    return candidates[0]


def _coverage(rows: Iterable[Mapping[str, Any]], kind: str) -> tuple[int, int]:
    rules = {
        "read": ("resources_read", "read_provenance_correct"),
        "write": ("resources_written_mutated", "write_provenance_correct"),
        "payload": ("actual_payload_object_ids_types", "payload_provenance_correct"),
        "destination": ("actual_destinations", "destination_correlation_correct"),
    }
    applicable_field, result_field = rules[kind]
    applicable = [row for row in rows if row[applicable_field]]
    return sum(row[result_field] is True for row in applicable), len(applicable)


def _summarize(name: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "suite": name,
        "tasks": len(rows),
        "utility_successes": sum(
            row["agentdojo_utility_task_success"] is True for row in rows
        ),
        "utility_failures": sum(
            row["agentdojo_utility_task_success"] is False for row in rows
        ),
        "llm_invocations": sum(row["number_of_llm_invocations"] for row in rows),
        "tool_calls_executed": sum(row["number_of_tool_calls"] for row in rows),
        "tool_call_proposals": sum(
            row["number_of_tool_call_proposals"] for row in rows
        ),
        "unexecuted_tool_call_proposals": sum(
            row["unexecuted_tool_call_proposals"] for row in rows
        ),
        "system_operation_nodes": sum(
            row["number_of_system_operation_nodes"] for row in rows
        ),
        "graph_nodes": sum(row["graph_node_count"] for row in rows),
        "graph_edges": sum(row["graph_edge_count"] for row in rows),
        "semantic_gap_tasks": sum(
            bool(row["missing_ambiguous_provenance"]) for row in rows
        ),
        "read_complete": _coverage(rows, "read")[0],
        "read_applicable": _coverage(rows, "read")[1],
        "write_complete": _coverage(rows, "write")[0],
        "write_applicable": _coverage(rows, "write")[1],
        "payload_complete": _coverage(rows, "payload")[0],
        "payload_applicable": _coverage(rows, "payload")[1],
        "destination_complete": _coverage(rows, "destination")[0],
        "destination_applicable": _coverage(rows, "destination")[1],
        "instrumentation_failures": sum(bool(row["instrumentation_failure"]) for row in rows),
    }


def _reconcile(source: Path, finalized: list[dict[str, Any]]) -> dict[str, Any]:
    manifest, shard_rows = load_completed_rows(source)
    normalize_call_accounting(shard_rows)
    find_candidates(shard_rows)
    finalized_by_task = {
        (row["suite"], row["task_id"]): row for row in finalized
    }
    shard_by_task = {(row["suite"], row["task_id"]): row for row in shard_rows}
    key_mismatch = sorted(
        set(finalized_by_task).symmetric_difference(shard_by_task)
    )
    mismatches: list[dict[str, object]] = []
    for task_key in sorted(set(finalized_by_task).intersection(shard_by_task)):
        canonical = finalized_by_task[task_key]
        shard = shard_by_task[task_key]
        differing = sorted(
            key
            for key in set(canonical) | set(shard)
            if canonical.get(key) != shard.get(key)
        )
        if differing:
            mismatches.append(
                {"suite": task_key[0], "task_id": task_key[1], "fields": differing}
            )
    return {
        "manifest_inventory_tasks": len(manifest["task_inventory"]),
        "completed_shard_rows": len(shard_rows),
        "finalized_rows": len(finalized),
        "unique_finalized_tasks": len(finalized_by_task),
        "completed_markers": sum(
            path.is_file() for path in source.glob("shards/shard-*/COMPLETED")
        ),
        "key_mismatch": [list(item) for item in key_mismatch],
        "normalized_shards_equal_finalized": not key_mismatch and not mismatches,
        "mismatches": mismatches,
    }


def _task_row(
    row: Mapping[str, Any], source: Path, repo_root: Path
) -> dict[str, object]:
    suite = str(row["suite"])
    task_id = str(row["task_id"])
    task_dir = _find_task_dir(source, suite, task_id)
    attempts = int(row["number_of_attempts"])
    graph_paths = [
        str(
            (task_dir / "attempts" / f"attempt-{attempt:02d}" / "graph.json")
            .relative_to(repo_root)
        )
        for attempt in range(attempts)
    ]
    read_applicable = bool(row["resources_read"])
    write_applicable = bool(row["resources_written_mutated"])
    payload_applicable = bool(row["actual_payload_object_ids_types"])
    destination_applicable = bool(row["actual_destinations"])
    return {
        "suite": suite,
        "task_id": task_id,
        "utility_outcome": _outcome(row["agentdojo_utility_task_success"]),
        "run_status": row["execution_run_status"],
        "failure_category": _failure_category(row),
        "attempts": attempts,
        "llm_invocations": row["number_of_llm_invocations"],
        "tool_calls_executed": row["number_of_tool_calls"],
        "tool_call_proposals": row["number_of_tool_call_proposals"],
        "unexecuted_tool_call_proposals": row["unexecuted_tool_call_proposals"],
        "system_operation_nodes": row["number_of_system_operation_nodes"],
        "graph_nodes": row["graph_node_count"],
        "graph_edges": row["graph_edge_count"],
        "graph_acyclic": _bool_cell(row["graph_acyclic"]),
        "proposal_correspondence": _bool_cell(row["tool_call_coverage_correct"]),
        "invocation_causality": _bool_cell(row["invocation_causality_correct"]),
        "runtime_observer_correlation": _bool_cell(
            row["runtime_observer_correlation_correct"]
        ),
        "effect_categories": _json_cell(row["effect_categories"]),
        "resources_read": _json_cell(row["resources_read"]),
        "resources_written_or_mutated": _json_cell(row["resources_written_mutated"]),
        "outgoing_operations": _json_cell(row["outgoing_operations"]),
        "destinations": _json_cell(row["actual_destinations"]),
        "payload_objects": _json_cell(row["actual_payload_object_ids_types"]),
        "read_evidence_status": _coverage_status(
            read_applicable, row["read_provenance_correct"]
        ),
        "write_evidence_status": _coverage_status(
            write_applicable, row["write_provenance_correct"]
        ),
        "payload_evidence_status": _coverage_status(
            payload_applicable, row["payload_provenance_correct"]
        ),
        "destination_evidence_status": _coverage_status(
            destination_applicable, row["destination_correlation_correct"]
        ),
        "system_operation_evidence_complete": _bool_cell(
            row["system_operation_coverage_correct"]
        ),
        "mutation_graph_consistent": _bool_cell(
            row["actual_mutation_vs_graph_consistent"]
        ),
        "semantic_provenance_status": (
            "missing_or_ambiguous"
            if row["missing_ambiguous_provenance"]
            else "no_explicit_gap_recorded"
        ),
        "missing_evidence": _json_cell(row["missing_ambiguous_provenance"]),
        "instrumentation_failure": _bool_cell(row["instrumentation_failure"]),
        "agentdojo_security_outcome": "unavailable",
        "attempt_fidelity": _json_cell(row["attempt_fidelity"]),
        "source_task_row": (
            f"{(source / 'task_audit.jsonl').relative_to(repo_root)}#{suite}/{task_id}"
        ),
        "source_attempt_graphs": _json_cell(graph_paths),
    }


def _ratio(summary: Mapping[str, Any], kind: str) -> str:
    return f"{summary[f'{kind}_complete']}/{summary[f'{kind}_applicable']}"


def _render_report(
    manifest: Mapping[str, Any],
    summaries: list[dict[str, Any]],
    reconciliation: Mapping[str, Any],
    source_relative: Path,
    attempt_distribution: Mapping[int, int],
) -> str:
    overall = summaries[-1]
    table = []
    for item in summaries:
        name = "**overall**" if item["suite"] == "overall" else item["suite"]
        table.append(
            "| "
            + " | ".join(
                (
                    name,
                    str(item["tasks"]),
                    f"{item['utility_successes']}/{item['tasks']}",
                    str(item["llm_invocations"]),
                    f"{item['tool_calls_executed']}/{item['tool_call_proposals']}",
                    f"{item['graph_nodes']}/{item['graph_edges']}",
                    str(item["semantic_gap_tasks"]),
                    _ratio(item, "read"),
                    _ratio(item, "write"),
                    _ratio(item, "payload"),
                    _ratio(item, "destination"),
                )
            )
            + " |"
        )
    return f"""# AgentDojo 97-task normal-run results

## Shareable result

The preserved observation-only run represents all {overall['tasks']}/97 normal
AgentDojo v1.2.2 tasks. With `Qwen/Qwen3-32B`, AgentDojo utility succeeded on
{overall['utility_successes']}/97 tasks and failed on
{overall['utility_failures']}/97. All runs completed without an execution
exception or collector/instrumentation failure. Every task graph passed
acyclicity, proposal correspondence, invocation causality, and runtime-observer
correlation.

This establishes reliable capture of the run skeleton and runtime boundary. It
does **not** establish security success: normal tasks have no injection task, so
their AgentDojo security outcome is `unavailable`. It also does not establish
benchmark-wide semantic provenance. Generic execution recording alone does not
prove concrete read, write, payload, or destination relationships;
{overall['semantic_gap_tasks']}/97 tasks have explicit missing or ambiguous
semantic evidence.

## Per-suite and overall summary

`Executed/proposed` distinguishes result-bearing executions from ToolCall graph
proposals. `Nodes/edges` are sums of separate task graphs. Evidence coverage is
`complete/applicable`; non-applicable tasks are excluded from each denominator.

| Suite | Tasks | Utility | LLM calls | Executed/proposed | Nodes/edges | Gap tasks | Read | Write | Payload | Destination |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
{chr(10).join(table)}

There were {overall['system_operation_nodes']} SystemOperation nodes from
successful tool calls. {overall['unexecuted_tool_call_proposals']} terminal
ToolCall proposals had no result-bearing execution.

## Task-level and attempt-level semantics

`normal_tasks.csv` has exactly one row per suite/task. Task-level counts sum all
attempts; task-level fidelity booleans are the logical AND across attempts;
missing-evidence messages are deduplicated; and graph node/edge counts are sums
over separate attempt graphs. The preserved run had attempt distribution
`{json.dumps(dict(attempt_distribution), sort_keys=True)}`--all 97 tasks had one
attempt--so retry aggregation does not change these totals. Each row retains the
attempt-level fidelity record and source graph path.

The finalized aggregate is canonical for call accounting. Shard rows used the
older ambiguous `number_of_tool_calls`; finalization split ToolCall proposals
from result-bearing executions and marked four qualified near-match members.
After applying only those documented normalizations, completed shards and the
finalized rows compare equal:
`{str(reconciliation['normalized_shards_equal_finalized']).lower()}`.

## Concrete retained graph

`graph_example/GRAPH_EXAMPLE.md` explains the retained
`workspace/user_task_33` attempt graph, copied byte-for-byte from the original
shard. It demonstrates explicit file reads, attachment/content payload edges,
the actual destination, and the committed email write. This is one
well-instrumented Workspace slice, not evidence of full-suite coverage.

## Configuration and provenance

- Source audit: `{source_relative}`.
- Original Slurm array: `{manifest['slurm']['array_job_id']}`; eight completed shards.
- AgentDojo distribution: `{manifest['agentdojo_distribution_version']}`.
- AgentDojo benchmark: `{manifest['agentdojo_benchmark_version']}`.
- Model/provider: `{manifest['model']}` / `{manifest['provider']}`.
- Decoding: temperature `{manifest['decoding']['temperature']}`, top-p
  `{manifest['decoding']['top_p']}`, base seed `{manifest['decoding']['base_seed']}`.
- Original repository revision: `{manifest['git_commit']}`; the original
  manifest records worktree clean as `{str(manifest['worktree_clean']).lower()}`.
- Reconciliation: {reconciliation['unique_finalized_tasks']} unique finalized
  rows, {reconciliation['completed_shard_rows']} completed shard rows, and
  {reconciliation['completed_markers']} completion markers. No benchmark rerun.

The exact source pointer is recorded on every CSV row. `source_integrity.json`
records hashes for the canonical source artifacts and copied graph.

## Interpretation limits

- Utility failure is an agent/task outcome, not a CausalGuard failure.
- Graph validity, correspondence, and lack of exceptions are not security outcomes.
- `no_explicit_gap_recorded` means only that this audit emitted no gap for that
  row; it is not blanket proof of semantic completeness or safety.
- Sensitive/protected-resource counts are unavailable for these normal runs and
  are not inferred.
- The absence of strict natural matches applies to this completed normal-task
  audit, not to every attack configuration. The two structural near-match
  groups differ in argument hashes and do not establish unique information gain
  from runtime effects.
- This audit used one model/configuration and one deterministic seed per task.
"""


def _render_graph_report(
    row: Mapping[str, Any], source_graph: Path, graph: Mapping[str, Any]
) -> str:
    node_counts = Counter(node["node_type"] for node in graph["nodes"])
    edge_counts = Counter(edge["edge_type"] for edge in graph["edges"])
    return f"""# Retained graph example: workspace/user_task_33

This is attempt 0 from the completed 97-task observation-only audit. The JSON
and DOT files in this directory are byte-for-byte copies of `{source_graph}`;
they were not reconstructed or rerun.

- Utility: `{_outcome(row['agentdojo_utility_task_success'])}`.
- Tool sequence: `{json.dumps(row['tool_call_sequence'])}`.
- Graph: {len(graph['nodes'])} nodes and {len(graph['edges'])} edges; acyclic
  `{str(row['graph_acyclic']).lower()}`.
- Node types: `{json.dumps(dict(sorted(node_counts.items())), sort_keys=True)}`.
- Edge types: `{json.dumps(dict(sorted(edge_counts.items())), sort_keys=True)}`.
- Observed categories: `{json.dumps(row['effect_categories'], sort_keys=True)}`.
- Destination: `{json.dumps(row['actual_destinations'])}`.
- Payload objects:
  `{json.dumps(row['actual_payload_object_ids_types'], sort_keys=True)}`.
- Explicit semantic gaps: `{json.dumps(row['missing_ambiguous_provenance'])}`.

The graph records `search_files_by_filename` triggering a committed `file_read`,
concrete file versions connected by `read`, and `send_email` triggering a
committed `email_send`. The attachment and hashed email-content objects connect
to that send through `payload_of`; the operation connects to the created email
version through `write`; and the destination is retained on the call and
operation. This demonstrates the intended collector -> runtime effect ->
normalized graph path for the implemented Workspace search/send slice.

It does not establish benchmark-wide coverage: Travel, Banking, Slack, and most
Workspace tools were recorded generically. It also does not establish security
success because this was a normal task with no attack or injection evaluator.
"""


def package(source: Path, output_dir: Path) -> None:
    repo_root = Path.cwd().resolve()
    source = source.resolve()
    output_dir = output_dir.resolve()
    source_relative = source.relative_to(repo_root)
    output_dir.relative_to(repo_root)

    source_files = (
        source / "task_audit.jsonl",
        source / "run_manifest.json",
        source / "fidelity_summary.json",
        source / "effect_categories.json",
    )
    before_hashes = {path: _sha256(path) for path in source_files}
    manifest = json.loads((source / "run_manifest.json").read_text(encoding="utf-8"))
    rows = _load_jsonl(source / "task_audit.jsonl")
    rows.sort(key=_task_sort_key)
    reconciliation = _reconcile(source, rows)
    if len(rows) != 97 or len({(r["suite"], r["task_id"]) for r in rows}) != 97:
        raise ValueError("canonical audit does not contain 97 unique task rows")
    if not reconciliation["normalized_shards_equal_finalized"]:
        raise ValueError(f"completed shards do not reconcile: {reconciliation}")

    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "normal_tasks.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=TASK_FIELDS)
        writer.writeheader()
        writer.writerows(_task_row(row, source, repo_root) for row in rows)

    summaries = [
        _summarize(suite, [row for row in rows if row["suite"] == suite])
        for suite in SUITES
    ]
    summaries.append(_summarize("overall", rows))
    with (output_dir / "suite_summary.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        writer.writerows(summaries)
    (output_dir / "suite_summary.json").write_text(
        json.dumps(summaries, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    attempt_distribution = Counter(int(row["number_of_attempts"]) for row in rows)
    (output_dir / "REPORT.md").write_text(
        _render_report(
            manifest,
            summaries,
            reconciliation,
            source_relative,
            attempt_distribution,
        ),
        encoding="utf-8",
    )

    example = next(
        row for row in rows if (row["suite"], row["task_id"]) == GRAPH_EXAMPLE
    )
    task_dir = _find_task_dir(source, *GRAPH_EXAMPLE)
    source_graph = task_dir / "attempts" / "attempt-00" / "graph.json"
    source_dot = task_dir / "attempts" / "attempt-00" / "graph.dot"
    graph_dir = output_dir / "graph_example"
    graph_dir.mkdir(exist_ok=True)
    copied_graph = graph_dir / "workspace_user_task_33.graph.json"
    copied_dot = graph_dir / "workspace_user_task_33.graph.dot"
    shutil.copyfile(source_graph, copied_graph)
    shutil.copyfile(source_dot, copied_dot)
    graph = json.loads(source_graph.read_text(encoding="utf-8"))
    (graph_dir / "GRAPH_EXAMPLE.md").write_text(
        _render_graph_report(
            example, source_graph.relative_to(repo_root), graph
        ),
        encoding="utf-8",
    )

    all_sources = (*source_files, source_graph, source_dot)
    after_hashes = {path: _sha256(path) for path in all_sources}
    for path, digest in before_hashes.items():
        if after_hashes[path] != digest:
            raise RuntimeError(f"source artifact changed while packaging: {path}")
    integrity = {
        "source_audit": str(source_relative),
        "source_outputs_modified": False,
        "benchmark_rerun": False,
        "reconciliation": reconciliation,
        "source_hashes": {
            str(path.relative_to(repo_root)): after_hashes[path]
            for path in all_sources
        },
        "copied_graph_hashes": {
            str(path.relative_to(repo_root)): _sha256(path)
            for path in (copied_graph, copied_dot)
        },
    }
    (output_dir / "source_integrity.json").write_text(
        json.dumps(integrity, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    package(args.source, args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

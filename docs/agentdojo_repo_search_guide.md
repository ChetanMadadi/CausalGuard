# AgentDojo repository and package search guide

This guide is for the exact CausalGuard checkout and AgentDojo installation used by the
August 30, 2026 benchmark audit. Every command is read-only. Run commands from the
CausalGuard repository root unless a command says otherwise.

## 1. Establish the two roots

```bash
cd /work/pi_juanzhai_umass_edu/chetan/CausalGuard
CAUSALGUARD_ROOT=$PWD
PY="$CAUSALGUARD_ROOT/.venv/bin/python"
AGENTDOJO_ROOT=$($PY -c 'import agentdojo,pathlib; print(pathlib.Path(agentdojo.__file__).resolve().parent)')
AGENTDOJO_DIST=$($PY -c 'import importlib.metadata as m; print(m.distribution("agentdojo")._path)')
AUDIT_ROOT="$CAUSALGUARD_ROOT/outputs/agentdojo/benchmark_audit_2026-08-30"
printf '%s\n' "$CAUSALGUARD_ROOT" "$PY" "$AGENTDOJO_ROOT" "$AGENTDOJO_DIST" "$AUDIT_ROOT"
```

Do not use bare `python` for this package: in this checkout it does not import
AgentDojo. Confirm that yourself:

```bash
python -c 'import sys; print(sys.executable); import agentdojo; print(agentdojo.__file__)'
$PY -c 'import sys,agentdojo; print(sys.executable); print(agentdojo.__file__)'
```

Show installed distribution metadata:

```bash
$PY -m pip show agentdojo
$PY -c 'import importlib.metadata as m; print(m.version("agentdojo")); print(m.distribution("agentdojo")._path)'
find "$AGENTDOJO_DIST" -maxdepth 1 -type f -printf '%f\n' | sort
test -f "$AGENTDOJO_DIST/direct_url.json" && sed -n '1,120p' "$AGENTDOJO_DIST/direct_url.json" || echo 'no direct_url.json: ordinary site-packages install, not editable'
sed -n '1,20p' "$AGENTDOJO_DIST/INSTALLER"
```

The expected answers are distribution `0.1.35`, package root
`$CAUSALGUARD_ROOT/.venv/lib/python3.12/site-packages/agentdojo`, installer `pip`, and
no editable-project metadata.

## 2. Choose the right navigation tool

- Use `find` when you do not yet know a filename or want a tree/inventory.
- Use `rg` when you know a class, function, tool, task number, literal, or field name.
- Use Python introspection after imports/version overlays have selected objects at runtime.
  It is the most reliable way to locate a task in benchmark `v1.2.2`, especially for
  dynamically combined tasks.
- Follow imports from `task_suite.py` to learn which state models and tool functions a
  suite actually registers.
- Use `sed` or `nl -ba | sed` after `rg` has found the relevant line.

Generic navigation:

```bash
find "$AGENTDOJO_ROOT" -maxdepth 2 -type d | sort
find "$AGENTDOJO_ROOT" -type f | sort | less
rg -n 'SEARCH_TERM' "$AGENTDOJO_ROOT"
rg -n 'SEARCH_TERM' "$CAUSALGUARD_ROOT"
```

Open a useful line window after a hit:

```bash
nl -ba "$AGENTDOJO_ROOT/functions_runtime.py" | sed -n '246,309p'
```

## 3. Find all suites and benchmark-version selection

```bash
nl -ba "$AGENTDOJO_ROOT/task_suite/load_suites.py" | sed -n '1,90p'
$PY - <<'PY'
from agentdojo.task_suite.load_suites import get_suites
for name, suite in get_suites("v1.2.2").items():
    print(name, suite.benchmark_version)
PY
```

`v1.2.2` selects Workspace `(1, 2, 2)`, Travel `(1, 2, 0)`, Banking
`(1, 2, 2)`, and Slack `(1, 2, 0)`. Version-compatible task selection is in:

```bash
nl -ba "$AGENTDOJO_ROOT/task_suite/task_suite.py" | sed -n '36,46p;163,206p;224,279p'
```

## 4. Find and count all normal user tasks

Find source declarations and overlays:

```bash
find "$AGENTDOJO_ROOT/default_suites" -type f -name 'user_tasks.py' | sort
rg -n '^class UserTask|create_combined_task|update_combined_task' "$AGENTDOJO_ROOT/default_suites"
```

Count the tasks actually registered for `v1.2.2`:

```bash
$PY - <<'PY'
from agentdojo.task_suite.load_suites import get_suites
for name, suite in get_suites("v1.2.2").items():
    print(f"{name}: {len(suite.user_tasks)}")
print("total:", sum(len(s.user_tasks) for s in get_suites("v1.2.2").values()))
PY
```

List IDs, selected modules, and line numbers after overlays are applied:

```bash
$PY - <<'PY'
from agentdojo.task_suite.load_suites import get_suites
import inspect
for suite_name, suite in get_suites("v1.2.2").items():
    for task_id, task in suite.user_tasks.items():
        cls = type(task)
        print(suite_name, task_id, cls.__module__, inspect.getsourcelines(cls)[1], sep="\t")
PY
```

## 5. Find one task by ID

A text search works for ordinary class-defined tasks:

```bash
rg -n 'class UserTask33|UserTask33' "$AGENTDOJO_ROOT/default_suites"
nl -ba "$AGENTDOJO_ROOT/default_suites/v1/workspace/user_tasks.py" | sed -n '1318,1361p'
```

The robust method, which also handles version overlays and generated combined tasks:

```bash
$PY - <<'PY'
from agentdojo.task_suite.load_suites import get_suite
import inspect
task = get_suite("v1.2.2", "workspace").get_user_task_by_id("user_task_33")
print(type(task), inspect.getsourcefile(type(task)), inspect.getsourcelines(type(task))[1])
print(inspect.getsource(type(task)))
PY
```

If introspection points to `task_suite/task_combinators.py`, find where the generated
task was requested:

```bash
rg -n 'UserTask23|create_combined_task|update_combined_task' "$AGENTDOJO_ROOT/default_suites" "$AGENTDOJO_ROOT/task_suite/task_combinators.py"
```

## 6. Find all injection tasks and inspect one

```bash
find "$AGENTDOJO_ROOT/default_suites" -type f -name 'injection_tasks.py' | sort
rg -n '^class InjectionTask|GOAL =|def security\(|def security_from_traces' "$AGENTDOJO_ROOT/default_suites"
$PY - <<'PY'
from agentdojo.task_suite.load_suites import get_suites
for name, suite in get_suites("v1.2.2").items():
    print(name, len(suite.injection_tasks), *suite.injection_tasks)
PY
nl -ba "$AGENTDOJO_ROOT/default_suites/v1/slack/injection_tasks.py" | sed -n '10,33p'
```

Find the environment placeholders into which attack text is inserted:

```bash
find "$AGENTDOJO_ROOT/data/suites" -name 'injection_vectors.yaml' -print -exec sed -n '1,80p' {} \;
rg -n '\{[A-Za-z0-9_]+\}' "$AGENTDOJO_ROOT/data/suites"
nl -ba "$AGENTDOJO_ROOT/task_suite/task_suite.py" | sed -n '78,155p'
```

Find attack generation and the pairing loop:

```bash
nl -ba "$AGENTDOJO_ROOT/attacks/base_attacks.py" | sed -n '41,125p'
nl -ba "$AGENTDOJO_ROOT/benchmark.py" | sed -n '41,157p;160,229p'
```

The second range also shows the standalone utility check for each injection task before
normal-user-task/injection-task pairings (`benchmark.py:200-209`).

## 7. Find tools registered by each suite

```bash
for suite in workspace slack banking travel; do
  echo "### $suite"
  nl -ba "$AGENTDOJO_ROOT/default_suites/v1/$suite/task_suite.py" | sed -n '1,125p'
done
```

Runtime introspection shows the exact registered name, environment dependency, return
annotation, implementation file, and first line:

```bash
$PY - <<'PY'
from agentdojo.task_suite.load_suites import get_suites
import inspect
for suite_name, suite in get_suites("v1.2.2").items():
    print("##", suite_name)
    for tool in suite.tools:
        deps = {k: v.env_dependency for k, v in tool.dependencies.items()}
        print(tool.name, deps, tool.return_type, inspect.getsourcefile(tool.run), inspect.getsourcelines(tool.run)[1], sep="\t")
PY
```

Find every tool implementation in the shared v1 tool modules:

```bash
rg -n '^def ' "$AGENTDOJO_ROOT/default_suites/v1/tools"
```

## 8. Find mutations and what tools return

Start broad, then inspect each hit in context:

```bash
rg -n '\.append\(|\.extend\(|\.pop\(|\.remove\(|del |\[[^]]+\] =|\.[A-Za-z_]+ = ' "$AGENTDOJO_ROOT/default_suites/v1/tools"
rg -n '^def (send|create|delete|cancel|reschedule|add|append|share|reserve|schedule|update|invite|remove|post)' "$AGENTDOJO_ROOT/default_suites/v1/tools"
```

Targeted examples:

```bash
nl -ba "$AGENTDOJO_ROOT/default_suites/v1/tools/email_client.py" | sed -n '73,124p;148,220p'
nl -ba "$AGENTDOJO_ROOT/default_suites/v1/tools/calendar_client.py" | sed -n '34,125p;137,254p'
nl -ba "$AGENTDOJO_ROOT/default_suites/v1/tools/cloud_drive_client.py" | sed -n '29,63p;80,155p'
nl -ba "$AGENTDOJO_ROOT/default_suites/v1/tools/slack.py" | sed -n '25,129p'
nl -ba "$AGENTDOJO_ROOT/default_suites/v1/tools/banking_client.py" | sed -n '50,151p'
nl -ba "$AGENTDOJO_ROOT/default_suites/v1/tools/travel_booking_client.py" | sed -n '339,400p'
```

## 9. Find environment and state models

```bash
rg -n '^class .*Environment|^class (Inbox|Calendar|CloudDrive|Slack|Web|BankAccount|Filesystem|UserAccount|Reservation|Hotels|Restaurants|CarRental|Flights|User)\b' "$AGENTDOJO_ROOT/default_suites/v1"
find "$AGENTDOJO_ROOT/data/suites" -name 'environment.yaml' -o -path '*/include/*.yaml' | sort
```

Follow `Depends("...")` to see which environment component a tool receives:

```bash
rg -n 'Depends\(' "$AGENTDOJO_ROOT/default_suites/v1/tools"
nl -ba "$AGENTDOJO_ROOT/functions_runtime.py" | sed -n '24,38p;246,309p'
```

## 10. Find utility and security evaluators

```bash
rg -n 'def utility\(|def utility_from_traces' "$AGENTDOJO_ROOT/default_suites"
rg -n 'def security\(|def security_from_traces' "$AGENTDOJO_ROOT/default_suites"
nl -ba "$AGENTDOJO_ROOT/base_tasks.py" | sed -n '18,94p;97,160p'
nl -ba "$AGENTDOJO_ROOT/task_suite/task_suite.py" | sed -n '281,337p;339,420p'
```

## 11. Find benchmark execution, agent loop, and runtime

```bash
rg -n '^def (benchmark_suite|run_task)|run_task_with_pipeline' "$AGENTDOJO_ROOT/benchmark.py" "$AGENTDOJO_ROOT/task_suite/task_suite.py"
nl -ba "$AGENTDOJO_ROOT/agent_pipeline/agent_pipeline.py" | sed -n '158,220p'
nl -ba "$AGENTDOJO_ROOT/agent_pipeline/tool_execution.py" | sed -n '22,157p'
nl -ba "$AGENTDOJO_ROOT/functions_runtime.py" | sed -n '41,88p;178,217p;246,309p'
```

## 12. Find what AgentDojo natively persists

```bash
nl -ba "$AGENTDOJO_ROOT/types.py" | sed -n '38,87p'
nl -ba "$AGENTDOJO_ROOT/logging.py" | sed -n '153,278p'
```

Native `TraceLogger` persists raw `messages`, run metadata, injection values, errors,
and eventual utility/security fields when benchmark logging is enabled. It does not
persist pre/post environment snapshots. The August 30 CausalGuard audit intentionally
did not save those raw native transcripts.

## 13. Find the CausalGuard AgentDojo adapter

```bash
rg -l '(^| )(import agentdojo|from agentdojo)' "$CAUSALGUARD_ROOT" --glob '*.py' | sort
rg -n 'class AgentDojoCollector|class AgentDojoTraceMapper|class AgentDojoRuntimeObserver|class ObservingFunctionsRuntime|class WorkspaceReadSendExtractor' "$CAUSALGUARD_ROOT/causalguard/integrations/agentdojo"
nl -ba "$CAUSALGUARD_ROOT/causalguard/integrations/agentdojo/collector.py" | sed -n '31,168p'
nl -ba "$CAUSALGUARD_ROOT/causalguard/integrations/agentdojo/runtime.py" | sed -n '57,207p'
nl -ba "$CAUSALGUARD_ROOT/causalguard/integrations/agentdojo/mapper.py" | sed -n '74,254p'
```

Find SystemOperation creation and generic fallback:

```bash
rg -n 'system_operation|runtime_operation|agentdojo_function_execution' "$CAUSALGUARD_ROOT/causalguard/integrations/agentdojo/mapper.py"
nl -ba "$CAUSALGUARD_ROOT/causalguard/integrations/agentdojo/mapper.py" | sed -n '178,254p'
rg -n 'SystemOperation|_system_operation_payload' "$CAUSALGUARD_ROOT/causalguard/graph/builder.py"
```

Find payload, destination, and write-provenance creation:

```bash
rg -n 'payload_refs|payload_objects|payload_of|destination|write_refs|write_objects' "$CAUSALGUARD_ROOT/causalguard/integrations/agentdojo" "$CAUSALGUARD_ROOT/causalguard/graph/builder.py"
rg -n '_SUPPORTED|def extract|def propose|_extract_email_send|_proposal_destination' "$CAUSALGUARD_ROOT/causalguard/integrations/agentdojo/extractors/workspace.py"
```

For email attachments, compare AgentDojo's ID-only storage with CausalGuard's
environment-backed resolution to a concrete file/version:

```bash
nl -ba "$AGENTDOJO_ROOT/default_suites/v1/tools/email_client.py" | sed -n '73,102p;154,193p'
nl -ba "$CAUSALGUARD_ROOT/causalguard/integrations/agentdojo/extractors/workspace.py" | sed -n '40,58p;116,137p;207,233p'
```

To answer “why was this a generic operation?”, compare the called function with the
only registered extractor's support set:

```bash
rg -n '_SUPPORTED|WorkspaceReadSendExtractor' "$CAUSALGUARD_ROOT/causalguard/integrations/agentdojo/extractors/workspace.py" "$CAUSALGUARD_ROOT/scripts/run_agentdojo_benchmark_audit.py"
```

## 14. Inspect Workspace `user_task_33` from beginning to end

Locate the shard and list its files:

```bash
TASK=$(find "$AUDIT_ROOT/shards" -type d -path '*/tasks/workspace/user_task_33' -print -quit)
printf '%s\n' "$TASK"
find "$TASK" -type f | sort
```

Inspect the safe execution record:

```bash
jq '{attempt_count, calls: [.calls[] | {ordinal,function_name,argument_summary,error_hash,result_hash,runtime_observed,state_changed,effect_categories,destinations,payload_object_ids}]}' "$TASK/agent_tool_execution.json"
```

Inspect before/after state hashes, component counts, and safe leaf diffs:

```bash
jq . "$TASK/system_effect.json"
```

Inspect task-level utility, fidelity, and gap metadata:

```bash
jq '{suite,task_id,execution_run_status,agentdojo_utility_task_success,agentdojo_security_evaluation_available,tool_call_sequence,missing_ambiguous_provenance}' "$TASK/execution_audit.json"
```

Inspect normalized events and distinguish proposals from operations:

```bash
jq -r '[.event_type, (.attributes.tool_name // ""), (.attributes.action_class // ""), (.attributes.operation_type // ""), (.attributes.destination // "")] | @tsv' "$TASK/trace.jsonl"
```

Inspect graph nodes and semantic edges:

```bash
jq -r '.nodes[] | [.node_type,.node_id,(.tool_name // ""),(.operation_type // ""),(.destination // ""),(.resource_id // "")] | @tsv' "$TASK/graph.json"
jq -r '.edges[] | [.edge_type,.source_id,.target_id] | @tsv' "$TASK/graph.json"
less "$TASK/graph.dot"
```

The audit exports hashes and bounded identifiers, not raw prompts, raw arguments, raw
tool results, or raw environment contents. This is why `argument_summary` contains names
and types while `argument_hash` carries equality evidence.

## 15. Search all audit results

Find one task everywhere:

```bash
rg -n 'user_task_33' "$AUDIT_ROOT"
```

Find every task that executed one tool:

```bash
jq -c 'select(any(.tool_calls[]?; .function_name == "send_money")) | {suite,task_id,tool_call_sequence,missing_ambiguous_provenance}' "$AUDIT_ROOT/task_audit.jsonl"
```

List all tools observed and counts:

```bash
jq -r '.tool_calls[]?.function_name' "$AUDIT_ROOT/task_audit.jsonl" | sort | uniq -c | sort -nr
```

Search mismatches by tool or suite/task:

```bash
rg -n 'send_money|banking/user_task_0' "$AUDIT_ROOT/provenance_mismatches.md"
jq -c 'select(.suite == "banking" and (.missing_ambiguous_provenance | length > 0)) | {task_id,missing_ambiguous_provenance}' "$AUDIT_ROOT/task_audit.jsonl"
```

Reproduce conditional denominators and successes:

```bash
jq -s '{tasks:length, mutated:map(select(.state_change_count>0))|length, payload:map(select((.actual_payload_object_ids_types|length)>0))|length, destination:map(select((.actual_destinations|length)>0))|length, semantic_gaps:map(select((.missing_ambiguous_provenance|length)>0))|length}' "$AUDIT_ROOT/task_audit.jsonl"
jq . "$AUDIT_ROOT/fidelity_summary.json"
jq . "$AUDIT_ROOT/effect_categories.json"
```

## 16. Minimal search trail for representative effects

Email with attachment:

```bash
rg -n 'def send_email|class Inbox' "$AGENTDOJO_ROOT/default_suites/v1/tools/email_client.py"
rg -n 'attachments|self\.emails\[' "$AGENTDOJO_ROOT/default_suites/v1/tools/email_client.py"
rg -n 'class UserTask33' "$AGENTDOJO_ROOT/default_suites/v1/workspace/user_tasks.py"
rg -n '_extract_email_send|_attachment_evidence|_email_destination' "$CAUSALGUARD_ROOT/causalguard/integrations/agentdojo/extractors/workspace.py"
```

Slack message or membership:

```bash
rg -n 'def send_direct_message|def send_channel_message|def invite_user_to_slack|def add_user_to_channel|def remove_user_from_slack' "$AGENTDOJO_ROOT/default_suites/v1/tools/slack.py"
rg -n 'user_inbox|channel_inbox|user_channels|users' "$AGENTDOJO_ROOT/default_suites/v1/tools/slack.py"
```

Bank transfer:

```bash
rg -n 'class Transaction|class BankAccount|def send_money|def schedule_transaction|def update_scheduled_transaction' "$AGENTDOJO_ROOT/default_suites/v1/tools/banking_client.py"
rg -n 'transactions\.append|scheduled_transactions\.append|transaction\.' "$AGENTDOJO_ROOT/default_suites/v1/tools/banking_client.py"
```

Travel reservation:

```bash
rg -n 'class Reservation|def reserve_hotel|def reserve_restaurant|def reserve_car_rental' "$AGENTDOJO_ROOT/default_suites/v1/tools/travel_booking_client.py"
rg -n 'reservation\.' "$AGENTDOJO_ROOT/default_suites/v1/tools/travel_booking_client.py"
```

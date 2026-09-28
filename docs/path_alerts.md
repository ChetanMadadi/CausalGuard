# Configurable path alerts — implementation and validation

General configurable path alerts are implemented as an opt-in observational capability. Existing precise attachment and verified-copy enforcement remains available and unchanged in meaning. Alerts are not enforcement decisions.

## Changed files

- `causalguard/policy/alerts.py`: strict path-policy contract, graph-view interface, bounded BFS, evaluation records, attempt-scoped monitor and JSONL emission.
- `causalguard/policy/alert_views.py`: existing GraphStore view and small NetworkX synthetic conformance adapter.
- `causalguard/integrations/agentdojo/alerts.py`: post-executor tool-result callback.
- `causalguard/integrations/agentdojo/collector.py`: optional monitor, prefix rebuild and alert exports.
- `policies/path_alert_P001.json`: reference File/Process/Socket policy.
- `policies/path_alert_workspace_context.json`: separately named application-level APP001 policy.
- `scripts/run_path_alert_demo.py`, `tests/test_path_alerts.py`, this report.
- Generated demonstrations: `outputs/path_alerts_2026-09-08/{synthetic,application,denied}/`.

Pre-existing dirty changes, precise policy engine and Phase 2 traversal were preserved. The new evaluator uses the existing GraphStore/NetworkX infrastructure and bounded backward BFS approach, but keeps its possible-influence contract separate from verified-copy matching.

## Contract and search semantics

Load JSON with `PathAlertPolicy.model_validate_json(...)`. The reference contract supports only type=path and action.type=alert; unknown fields, unsupported view node/edge types, nested attribute expressions and event/deny actions are rejected. Scalar equality is exact (Boolean true is not numeric 1); a configured list means any accepted member. An actual list matches when it contains an accepted scalar. All configured attributes must match; missing values do not match.

Search enumerates deterministic simple directed paths, shortest first, preserving multiedge IDs. Hops are inclusive (five allowed, six excluded for P001). `max_search_states` is an optional resource bound (default 10,000); exhaustion returns incomplete even if some witnesses were found. Complete no-match is explicitly named no_match_within_bounds. Coverage issues, such as absent attributes, unsupported evidence and partial adapter coverage, are separate from search completeness.

A seconds window requires every traversed edge to have the same seconds clock domain and lie in the closed interval [current_time - window, current_time]. The bound is anchored to evaluation time, not the sink timestamp. Ordinals are never converted to seconds. P001 is rejected on the production application view; APP001 has no seconds window and uses transcript ordinals.

The monitor is called with the current view for every update, including attribute-only updates. Deduplication keys include policy/source/sink/ordered edge IDs within an attempt; new witnesses and new attempts emit separately. Update IDs are independent of witness IDs.

## Runtime and evidence mapping

AgentDojoCollector accepts an optional `alert_monitor=PathAlertMonitor([...])` alongside the existing runtime observer. It validates configuration before running and installs an observational wrapper around the existing tool executor. After that executor returns, each newly returned tool-result prefix is mapped and built through the normal mapper and GraphBuilder. A batch may already be fully executed before its result prefixes are evaluated: this is post-execution observation, not a precommit guard or incremental graph-maintenance engine. Prefix rebuilding includes supported captured attribute changes. Standalone monitor callers must explicitly invoke on_update after their own graph/attribute changes.

The production view admits evidence-referenced runtime observations, explicit data references and parent-event relationships; it excludes session/temporal/context-inferred edges. Evidence basis identifies observed reads/effects separately from input/context availability. Existing adapter-attested identity-copy operation metadata is labeled explicitly; the general evaluator does not independently reverify copy bytes.

APP001 matches trusted-configured file resource 19 to an actual email_send operation with the configured destination mailto:outside@example.org. It does not create an OS Socket or infer an external zone from attacker assertions. Its paths use real DataObjects, operation results, LLM invocation context and calls. Generic extraction remains partial.

Alerts contain IDs and relationship categories, not file/body contents. They include clock, attempt, triggering update, ordered witness, policy and severity. Alert evaluations are exported separately. No alert is routed into PolicyDecision or changes allow/deny/request_approval behavior.

## Reproduce without models or scheduler jobs

Run from the repository root, using a fresh output directory to preserve earlier demonstration artifacts:

```bash
.venv/bin/python -m scripts.run_path_alert_demo --mode synthetic --output /tmp/cg-alert-synthetic-reproduction
.venv/bin/python -m scripts.run_path_alert_demo --mode application --output /tmp/cg-alert-application-reproduction
.venv/bin/python -m scripts.run_path_alert_demo --mode denied --output /tmp/cg-alert-denied-reproduction
.venv/bin/python -m pytest -q tests/test_path_alerts.py tests/test_policy_engine.py tests/test_path_policy.py tests/test_agentdojo_policy_enforcement.py
.venv/bin/python -m pytest -q
git diff --check
```

## Expected and actual outcomes (2026-09-08)

| Validation | Expected | Actual |
| --- | --- | --- |
| Focused alert and precise-policy regression tests | Pass | 128 passed |
| Full repository suite | Pass | 230 passed |
| Synthetic 25-edge stream | One P001 witness | One alert |
| Public/internal/disconnected/reversed/disallowed cases | No match | Passed |
| Five vs six hops, expired/future and boundary times | Scoped matching | Passed |
| Ordinal/incompatible clocks, unsupported configurations | Explicit rejection | Passed |
| Parallel edges, budget exhaustion, repeated prefixes | Correct edge / incomplete / dedup | Passed |
| Attribute update and distinct attempts | New update witness / separate attempts | Passed |
| Real runtime protected read + independently public email | Email allowed, coarse alert | One committed email, allow, two distinct five-edge alerts |
| Real runtime protected attachment to unauthorized recipient | Deny, no committed email or successful-send sink | Deny, zero email delta, zero alerts |
| Whitespace validation | Clean | git diff --check passed |

### Readable alert examples

Synthetic exact witness: `secret-file --read [read-secret]--> reader --send [send-public]--> external`. P001 critical; seconds timestamp 100, domain synthetic-clock, triggering update synthetic-update-1. Both edges are labeled synthetic_coarse_observation. The independently public send is intentionally connected through the same process: possible information flow is expected, not proof of secret disclosure.

One production witness (display aliases; exact IDs are in application/alerts.jsonl):

`file 19 --read--> runtime read operation --produces--> tool result --input_to--> LLM invocation --invokes--> proposed call --triggers--> committed email operation`.

The exact suffixes are runtime_operation:3:0, tool_result:3, llm:4, tool:4:0, and runtime_operation:5:0. APP001 high; ordinal timestamp 7, clock domain agentdojo-transcript:application-alert, triggering update tool-result:5. The read is observed; the remaining relationships express captured result/context/call availability, not verified payload derivation. A second five-edge witness passes through the generated email-content DataObject and committed payload_of relationship. Neither proves that the outgoing public body contains protected bytes.

See `outputs/path_alerts_2026-09-08/application/alerts.jsonl` and its `graph.json` for exact ordered node and edge IDs. The denied demonstration has an empty alerts.jsonl and no successful email mutation.

## Limitations and project update

This is bounded graph matching, not semantic body analysis, whole-system taint tracking, OS telemetry collection, or a new prevention claim. Synthetic File/Process/Socket tests are conformance fixtures only. Production application clocks remain ordinal; wall-clock window policies require another compatible source. Search and collection coverage are separate; missing evidence can hide flows. Prefix rebuild cost and witness enumeration are bounded only by graph size and the configured search-state budget; no large-scale performance claim is made. Event policies and broader suite adapters remain unsupported.

Project update: CausalGuard now supports opt-in configurable bounded path alerts with deterministic evidence witnesses, explicit clock and search-completeness semantics, and attempt-level deduplication. Synthetic conformance and model-free AgentDojo runtime demonstrations passed, while precise attachment/copy enforcement retained its existing meaning and denied sends produced no committed email effect. These alerts identify possible influence, not proven disclosure; this work demonstrates neither real-model prevention nor an information advantage over a full-history baseline. No inference, scheduler jobs, completed audit reruns or six-trajectory reruns were performed.

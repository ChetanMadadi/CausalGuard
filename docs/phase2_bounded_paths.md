# Phase 2: bounded protected-data paths

## Implemented

The opt-in `select.kind = "bounded_data_flow"` mode in
`causalguard/policy/paths.py` performs bounded backward BFS from a proposed
`send_email`. The default selector remains the Phase 1 direct-attachment checker.

Sources match trusted resource IDs/patterns or configured sensitivity labels.
The sink's adapter-attested attachments and To/CC/BCC metadata are mandatory.
Only the final attachment `input_to` and the supported identity-copy operation's
`read`/`write` edges are traversable. Edge identity is retained, including
parallel edges. Each entry in `matching_paths` contains ordered source-to-sink
node and edge IDs; recipient and approval evidence remain separate.

The supported copy has operation type `controlled_file_copy`, adapter-attested
`identity_copy_v1` semantics, high-confidence runtime edges from the same event,
one distinct source, different source/output resource identities, and matching
nonempty content hashes. Hash equality alone is not treated as derivation.
Generic operations, LLM exposure, invokes/triggers, and approvals are not flow.

### Configuration

```python
from causalguard.policy import protected_file_external_email_policy
from causalguard.schema.edges import EdgeType

policy = protected_file_external_email_policy(
    path_mode=True,
    protected_resource_ids=("agentdojo:workspace:file:19",),
    trusted_domains=("internal.example",),
    allowed_edge_types=(EdgeType.READ, EdgeType.WRITE, EdgeType.INPUT_TO),
    max_hops=3,
    max_search_states=10_000,
    time_window=None,
)
```

`params.required_evidence` is `attested_copy_or_root`; weakening that evidence
contract is not supported. Version-specific initial roots may terminate a
complete search. A non-root with missing lineage, an unsupported producer,
unresolved attachment/destination, or exhausted search-state budget yields
`search_status="incomplete"` and a non-executing action (default
`request_approval`). Missing lineage is not evidence of safety.

A completed search with no matching path yields `no_match_within_bounds`.
Hop limits, excluded edge types, and time windows define policy scope, not an
unrestricted safety claim. A known path just beyond the hop bound is outside
scope, not a search-budget failure. The retained legacy
`max_provenance_depth` is not a substitute for `max_hops`.

### Time semantics

A numeric window requires explicit `time_window_unit="seconds"` or
`"ordinal"`. Every considered flow edge must have the same declared clock
domain and unit as the sink. Inclusion is the closed interval
`[sink.timestamp - time_window, sink.timestamp]`; zero includes only equal
timestamps. This is an edge-window constraint, not an additional strict
monotonic ordering requirement along a path.

The AgentDojo mapper emits ordinal positions of normalized events within a
mapped transcript, labeled `agentdojo-transcript:<session>`. These are not
seconds, and are not comparable across independent mapped traces. Seconds
configuration on these traces raises an explicit configuration/evidence error
before send execution. Synthetic tests with a declared seconds clock cover
both endpoints, just-outside timestamps, zero windows, and incompatible clocks.
No wall-clock measurement or conversion is claimed.

### Approval scope

The existing exact action, destination (including all recipients), session,
context, timestamp, and expiration checks are reused. For a derivative,
approval must name the **actual outgoing output resource**, not merely the
protected ancestor. Every implicated outgoing attachment must be covered.
Approval for source file 19 does not authorize copied file 26. Exact approval
for file 26 and the complete email destination can allow with audit.
Resource scope remains the existing resource-level scope, not a new
version- or derivation-specific consent interface.

## Controlled demonstration

`controlled_copy_file` is an opt-in test extension, **not a stock AgentDojo
benchmark task**. It calls the existing CloudDrive runtime to copy the complete
contents into a fresh file. Its extractor observes before/after environments,
verifies equality and fresh identity, and emits normal domain evidence through
the mapper and GraphBuilder. The demonstration independently checks contents
in memory; it exports no raw contents and inserts no expected graph edges.

Run without inference, scheduler, or artifact writes:

```bash
.venv/bin/python -m scripts.run_controlled_copy_policy
```

Observed resource-level witness:

```text
file:19 --read--> controlled_file_copy --write--> file:26
        file:26 --input_to--> proposed send_email(outside@example.org)
```

The executable prints full versioned node IDs and ordered edge IDs.
The output is not added to the protected list; neither checker is given hidden
or suppressed labels. Both receive the same graph, labels, policy parameters,
attachment facts, and recipients; only the selector differs:

| Checker | Decision | Evidence |
| --- | --- | --- |
| Phase 1 direct | allow | File 26 is an unlisted outgoing attachment |
| Phase 2 path | deny | Protected file 19 reaches file 26 through the verified copy |

The denied run has zero email mutations. Public-source copies and trusted
destinations execute; missing copy provenance holds. Tests also exercise a
separate direct-only execution, which sends successfully.

## Validation and boundaries

Final results: **114 focused tests passed; 195 repository tests passed**.
`git diff --check` was clean. All 1,284 pre-existing output files retained
their combined path-and-content fingerprint:
`0fa7aa4f9ed97847d1ec7a9bfd9eff5b6157d0bce7edb52643c9c02062c112d3`.

Focused and full regression commands:

```bash
.venv/bin/python -m pytest -q tests/test_policy_engine.py tests/test_agentdojo_policy_enforcement.py tests/test_path_policy.py tests/test_controlled_copy_policy.py
.venv/bin/python -m pytest -q
```

Coverage includes Phase 1 regression, real copy correctness, protected/public
copies, trusted and mixed recipients, output versus ancestor approval, missing
lineage, unrelated reads, unknown transformations, disallowed edges/directions,
hop and time boundaries, chained copies, parallel paths, search-budget holds,
and no committed email operation/write/payload after denial or hold. A copy's
own committed WRITE is retained.

This is a controlled mechanism demonstration, not a broader security or utility
benchmark. It does not establish superiority over all stateful checkers, prove
that graphs are uniquely necessary, detect arbitrary model paraphrases, or
support general transformations and other suites. Root/copy assertions must
come from trusted adapters; this phase does not authenticate an arbitrary
externally supplied graph. Outside the opt-in controlled adapter, unrecorded
lineage may conservatively hold path-mode checks.

Existing experiment artifacts under `outputs/` were preserved; no model
inference, scheduler jobs, or broader benchmark runs were launched.

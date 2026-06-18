# CausalGuard

CausalGuard is a foundation prototype for a typed, privacy-preserving provenance DAG for agentic AI execution.

This repository currently implements Weeks 1-2 from `guide.pdf`: validated schemas for normalized events, typed nodes, and typed edges, plus an in-memory typed DAG store with export support. It intentionally does not include the graph builder, queries, traces, or visualization scripts yet.

## Setup

```bash
python3 -m pip install -e ".[dev]"
```

## Run Tests

```bash
python3 -m pytest -q
```

## Week 1 Scope

- `causalguard/schema/events.py`: normalized collector event schema.
- `causalguard/schema/nodes.py`: five typed graph node schemas.
- `causalguard/schema/edges.py`: typed graph edge schema.
- Strict key-based privacy validation for obvious raw sensitive content fields.

## Week 2 Scope

- `causalguard/graph/store.py`: `networkx.DiGraph` backed typed DAG store.
- Validated node and edge insertion with duplicate, missing endpoint, and cycle rejection.
- Metadata-preserving JSON export through `GraphStore.to_dict()` and `GraphStore.to_json()`.
- Compact DOT export through `GraphStore.to_dot()` for early graph inspection.

## Design Decisions

- Pydantic v2 is used for validation.
- Models reject unknown top-level fields so schema drift fails early.
- Timestamps must be finite and non-negative.
- IDs and required string fields must be non-empty after trimming whitespace.
- Privacy validation recursively rejects obvious raw-content keys such as `prompt`, `output`, `message`, `content`, `password`, `token`, `api_key`, `secret`, keys beginning with `raw_`, and keys ending with `_raw`.
- Hashes, stable IDs, labels, scopes, and summaries are allowed.
- The Week 2 store uses a simple `networkx.DiGraph`, so it allows one edge per source/target pair and keeps `edge_id` as validated edge metadata.
- DOT export uses compact node and edge labels and does not include prompt, output, argument, or file contents.

## Assumptions

- Week 1 was schema-only.
- Week 2 is storage-only; event-to-node conversion starts in Week 3.
- Key-based privacy checks are a conservative first guard, not a full content classifier.
- `user_input` events are represented as normalized events but do not yet create graph nodes; node conversion starts in Week 3.

## Next Steps

Week 3 should add the graph builder that converts normalized synthetic traces into graph nodes and causal edges.

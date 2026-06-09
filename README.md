# CausalGuard

CausalGuard is a foundation prototype for a typed, privacy-preserving provenance DAG for agentic AI execution.

This repository currently implements Week 1 from `guide.pdf`: validated schemas for normalized events, typed nodes, and typed edges. It intentionally does not include the graph store, graph builder, queries, traces, or visualization yet.

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

## Design Decisions

- Pydantic v2 is used for validation.
- Models reject unknown top-level fields so schema drift fails early.
- Timestamps must be finite and non-negative.
- IDs and required string fields must be non-empty after trimming whitespace.
- Privacy validation recursively rejects obvious raw-content keys such as `prompt`, `output`, `message`, `content`, `password`, `token`, `api_key`, `secret`, keys beginning with `raw_`, and keys ending with `_raw`.
- Hashes, stable IDs, labels, scopes, and summaries are allowed.

## Assumptions

- Week 1 is schema-only.
- Key-based privacy checks are a conservative first guard, not a full content classifier.
- `user_input` events are represented as normalized events but do not yet create graph nodes; node conversion starts in Week 3.

## Next Steps

Week 2 should add the in-memory `networkx.DiGraph` graph store, duplicate and cycle rejection, metadata preservation, and JSON/DOT export.

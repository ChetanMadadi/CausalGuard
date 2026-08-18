# CausalGuard Progress Report: Enriched 8-Slide Meeting Version

Meeting with Prof. Juan Zhai  
Focus: Week 2 completion and Week 3 plan  
Prepared: June 18, 2026

---

## Slide 1: CausalGuard Progress Report

**Meeting with Prof. Juan Zhai**  
**Focus:** Week 2 completion and Week 3 plan

Main update:

* Week 1 schema work is complete. The project now has validated schemas for normalized events, typed graph nodes, and typed causal edges.
* Week 2 graph-store work is complete and validated. The project now has an in-memory typed DAG store that can safely hold provenance nodes and causal relationships.
* The next step is Week 3: building a trace-to-graph builder that automatically converts normalized event traces into this validated graph structure.

High-level message:

* CausalGuard has moved from defining valid data structures to storing those structures as a causal graph.
* The validated Week 2 graph store is the foundation for future trace processing, graph queries, policy checks, and visualization.

---

## Slide 2: What Has Been Completed

Completed so far:

* A typed schema layer has been implemented for provenance events, graph nodes, and causal edges. This ensures that the graph has a strict structure instead of storing arbitrary log data.
* The schemas are privacy-aware. Sensitive raw fields such as prompts, outputs, messages, credentials, and file contents are rejected, while safer metadata such as hashes, summaries, labels, and stable IDs are allowed.
* An in-memory typed provenance graph store has been implemented using `networkx.DiGraph`.
* The graph store accepts validated typed nodes and edges, preserves their metadata, rejects invalid graph mutations, and exports graph data for inspection.
* Test coverage is in place for both schema behavior and graph-store behavior.

Validated result:

```text
39 passed in 0.21s
```

Main outcome:

* CausalGuard now has a validated typed DAG foundation. This means future components can rely on a clean, acyclic, typed graph instead of working directly with raw logs.

---

## Slide 3: Week 2 Deliverable: GraphStore

Main file:

* `causalguard/graph/store.py`

Core class:

* `GraphStore`

What `GraphStore` does:

* It adds validated graph nodes, such as LLM invocation nodes or tool-call nodes, into an internal directed graph.
* It adds validated graph edges, such as an `invokes` edge from an LLM node to a tool node.
* It rejects duplicate node IDs so each graph node has one clear identity.
* It rejects duplicate edge IDs so each relationship can be referenced unambiguously.
* It rejects edges whose source or target node does not exist, preventing dangling causal links.
* It rejects edges that would create a cycle, preserving the graph as a directed acyclic graph.
* It retrieves typed node and edge objects, not just raw dictionaries.
* It exports the graph to JSON for debugging and DOT for visualization.
* It returns a safe copy of the internal NetworkX graph so outside code cannot accidentally mutate the store.

Why this matters:

* The project now has a reliable internal representation for causal provenance. Later work can focus on building and querying the graph because Week 2 already guarantees the graph structure is valid.

---

## Slide 4: Graph Safety Guarantees

The graph store is designed around causal direction:

```text
LLM invocation -> Tool call -> System/resource action
```

Allowed:

* Valid typed nodes that match the node schema.
* Valid typed causal edges that match the edge schema.
* Directed acyclic graph structure where causes come before effects.

Rejected:

* Duplicate node IDs, because one ID should not refer to multiple graph objects.
* Duplicate edge IDs, because one edge ID should not represent multiple relationships.
* Edges to missing nodes, because every causal link must connect real graph objects.
* Duplicate source/target edge pairs, because the current `DiGraph` implementation stores one relationship between a pair of nodes.
* Cycles, because a later effect should not become a cause of an earlier node.

Example:

```text
Allowed:  llm:e2 -> tool:e3
Rejected: tool:e3 -> llm:e2 if llm:e2 already reaches tool:e3
```

Importance:

* Future causal queries depend on a clean DAG.
* Topological ordering can process causes before effects only if the graph remains acyclic.
* Rejecting invalid structure early keeps later policy logic simpler and safer.

---

## Slide 5: Current Design Decisions

Current design:

* The graph store uses `networkx.DiGraph` because it is simple, well understood, and sufficient for the first validated prototype.
* Typed Pydantic node and edge objects are stored inside the graph, so the graph keeps validated structured data rather than untyped metadata.
* The store currently allows one edge per source/target pair. This keeps the Week 2 graph model simple and predictable.
* An internal `_edge_index` maps each `edge_id` to its `(source_id, target_id)` pair, making edge lookup by ID straightforward.
* JSON export is stable and useful for debugging or future scripts.
* DOT export gives a compact visualization format without exposing raw sensitive content.

Known limitation:

* `networkx.DiGraph` does not support multiple parallel edge types between the same two nodes.

Example future issue:

```text
A --invokes--> B
A --authorizes--> B
```

Possible future change:

* If future graph semantics require multiple relationships between the same two nodes, we can migrate the store to `networkx.MultiDiGraph`.
* For the validated Week 2 scope, `DiGraph` is acceptable because the priority is correctness, clarity, and testability.

---

## Slide 6: Questions To Discuss

These are not blockers for Week 2. They are alignment questions for the next design steps.

1. **Privacy boundary**

   * Should raw prompts, raw model outputs, user messages, and file contents always remain outside the graph store?
   * The current implementation enforces this by allowing hashes, stable IDs, labels, and summaries while rejecting obvious raw-content keys.
   * This makes the graph useful for reasoning without turning it into a storage location for sensitive data.

2. **Graph semantics**

   * Is one edge per source/target pair acceptable for the first prototype?
   * The current `DiGraph` design is simple and validated, but future policy logic may need multiple relationship types between the same nodes.
   * If that becomes necessary, we should plan a controlled migration to `MultiDiGraph`.

3. **Week 3 causal-link policy**

   * When explicit parent links are missing, should the builder skip the causal edge at first?
   * Or should it infer a lower-confidence edge using shared causal context or a short temporal window?
   * This affects how much uncertainty the graph records during automatic trace processing.

4. **Week 3 scope**

   * Proposed scope is builder plus tests first.
   * Example JSONL traces and mock collector support can be added afterward unless they are needed immediately for demonstration.

---

## Slide 7: Plan For Next Week

Primary goal:

* Implement the Week 3 trace-to-graph builder.

Planned work:

* Take normalized events as input, either as dictionaries or validated `NormalizedEvent` objects.
* Convert each event into the correct typed provenance node, such as an LLM node, tool-call node, system-operation node, data-object node, or approval node.
* Create causal edges using explicit `parent_event_id` links when available.
* Use `causal_context_id` as a possible fallback for lower-confidence inferred causal edges.
* Insert all generated nodes and edges into the validated Week 2 `GraphStore`.
* Add synthetic trace tests that prove the builder produces the expected typed nodes and causal edges.

Expected output:

* `causalguard/graph/builder.py`
* Tests for simple LLM-to-tool traces.
* Tests for tool-to-system-operation traces.
* Tests for data access and network-send events.
* Tests for missing or invalid causal references.
* Tests confirming the constructed graph remains acyclic.

Main transition:

* Week 2 required manually adding nodes and edges.
* Week 3 should automatically construct those nodes and edges from normalized event traces.

---

## Slide 8: Next Meeting Target

By the next meeting, the target is to show:

* A working trace-to-graph builder that consumes normalized synthetic events.
* Synthetic trace examples showing a simple causal chain such as:

```text
LLM invocation -> Tool call -> Data access
```

* Passing tests for the Week 3 builder.
* Updated README or report notes explaining the builder assumptions.
* A clear explanation of how causal edges are created from parent IDs, shared causal context, or other approved rules.

Main milestone:

* Move from manually adding graph nodes and edges to automatically constructing a provenance graph from normalized traces.

Why this matters:

* Once the graph can be built automatically, the next major step will be graph queries, such as finding sensitive data access, untrusted network sends, or missing approvals.

# CausalGuard Week 2 Slide Report

Prepared: June 18, 2026  
Main focus: Week 2 graph store  
Validation status: Week 2 is fully validated by the project owner.

---

## Slide 1: Title

**CausalGuard Foundation Prototype**  
**Week 2 Report: In-Memory Typed Provenance DAG Store**

Main message:
- Week 2 turns the validated schemas from Week 1 into an actual graph store.
- The graph store keeps provenance data typed, acyclic, inspectable, and safe from basic structural corruption.
- Week 2 has been validated by the project owner.

---

## Slide 2: Week 2 Objective

Week 2 objective:
- Implement an in-memory graph store for typed provenance nodes and typed provenance edges.

The graph store must:
- Accept validated typed nodes.
- Accept validated typed edges.
- Reject duplicate nodes.
- Reject duplicate edges.
- Reject edges with missing endpoints.
- Reject edges that create cycles.
- Preserve node and edge metadata.
- Export graph data for debugging and visualization.

Simple target shape:

```text
LLM invocation -> Tool call
```

Example meaning:
- An LLM invocation caused a tool call.

---

## Slide 3: Why Week 2 Matters

Week 1 answered:
- What is a valid event?
- What is a valid node?
- What is a valid edge?
- What metadata is safe to store?

Week 2 answers:
- Can these validated objects be stored as a graph?
- Can we preserve causal direction?
- Can we reject graph structures that break provenance reasoning?
- Can future code inspect and export the graph?

Communication point:
- Week 2 is the bridge between schema validation and future causal/security queries.

---

## Slide 4: Week 2 Deliverable

Main deliverable:
- `causalguard/graph/store.py`

Main class:
- `GraphStore`

Supporting error type:
- `GraphStoreError`

Backing implementation:
- `networkx.DiGraph`

Week 2 test file:
- `tests/test_graph_store.py`

Validated test result for Week 1 + Week 2 context:

```text
39 passed in 0.21s
```

---

## Slide 5: Graph Store Data Model

The graph store uses `networkx.DiGraph`.

Each graph node stores:
- The typed Pydantic node object under `node`.
- The node type string under `node_type`.

Each graph edge stores:
- The typed Pydantic edge object under `edge`.
- The edge ID under `edge_id`.
- The edge type string under `edge_type`.

Why this matters:
- The graph is not just storing raw dictionaries.
- Code can retrieve typed objects later.
- Export functions can still produce JSON-friendly output.

---

## Slide 6: Example Typed Node Stored In Week 2

Example LLM node:

```python
{
    "node_id": "llm:e2",
    "node_type": "llm_invocation",
    "model_name": "gpt-4o",
    "prompt_hash": "sha256:p",
    "output_hash": "sha256:o",
    "token_count": 120,
    "timestamp": 2.0,
    "agent_id": "agent1",
    "session_id": "s1",
    "causal_context_id": "ctx17",
}
```

What Week 2 does with it:
- Parses it as a typed provenance node.
- Rejects it if invalid.
- Stores it under node ID `llm:e2`.
- Preserves the typed node object for later retrieval.

Privacy point:
- The graph stores `prompt_hash` and `output_hash`, not raw prompt/output content.

---

## Slide 7: Example Typed Edge Stored In Week 2

Example edge:

```python
{
    "edge_id": "edge:e2:e3",
    "source_id": "llm:e2",
    "target_id": "tool:e3",
    "edge_type": "invokes",
    "timestamp": 3.0,
    "derivation": "parent_event",
    "confidence": "high",
    "evidence_ref": "e3",
}
```

Meaning:

```text
llm:e2 --invokes--> tool:e3
```

What Week 2 checks before storing:
- Edge ID is unique.
- Source node exists.
- Target node exists.
- Source and target pair is not already connected.
- The edge will not create a cycle.

---

## Slide 8: `GraphStore.__init__`

Purpose:
- Creates an empty in-memory directed graph.
- Creates an edge lookup index.

Internal state:

```python
self._graph = nx.DiGraph()
self._edge_index = {}
```

Why `_graph` exists:
- Stores actual nodes and edges.
- Uses NetworkX graph operations like cycle/path checking and topological sorting.

Why `_edge_index` exists:
- Makes edge lookup by `edge_id` simple.
- Stores `edge_id -> (source_id, target_id)`.

Example after initialization:

```text
node_count = 0
edge_count = 0
```

---

## Slide 9: `node_count` And `edge_count`

`node_count`:
- Returns the number of nodes currently stored.
- Internally calls `self._graph.number_of_nodes()`.

`edge_count`:
- Returns the number of edges currently stored.
- Internally calls `self._graph.number_of_edges()`.

Example:

```python
store = GraphStore()
store.node_count  # 0
store.edge_count  # 0
```

After adding two nodes and one edge:

```text
node_count = 2
edge_count = 1
```

Why this matters:
- Tests and debugging can quickly confirm graph size.

---

## Slide 10: `add_node(node)`

Purpose:
- Validates and inserts one provenance node.

Input accepted:
- A typed provenance node object.
- A dictionary that can be validated into a typed provenance node.

Main steps:
- Parse the input using `parse_provenance_node`.
- Check whether `node_id` already exists.
- If duplicate, raise `GraphStoreError`.
- Add node to the NetworkX graph.
- Store typed node object and `node_type` metadata.
- Return the parsed typed node.

Example:

```python
node = store.add_node(llm_node("llm:e2"))
```

Result:
- `llm:e2` is now in the graph.
- `node` is a typed LLM invocation node.

---

## Slide 11: `add_edge(edge)`

Purpose:
- Validates and inserts one provenance edge.

Input accepted:
- A typed `ProvenanceEdge`.
- A dictionary that can be validated into `ProvenanceEdge`.

Main safety checks:
- Reject duplicate `edge_id`.
- Reject missing source node.
- Reject missing target node.
- Reject duplicate source/target pair.
- Reject cycle creation.

Example:

```python
store.add_node(llm_node("llm:e2"))
store.add_node(tool_node("tool:e3"))
edge = store.add_edge(edge_payload("edge:e2:e3", "llm:e2", "tool:e3"))
```

Result:

```text
llm:e2 --invokes--> tool:e3
```

---

## Slide 12: Why `add_edge` Checks For Cycles

Provenance should move forward through causes and effects.

Valid:

```text
LLM -> Tool -> System operation
```

Invalid:

```text
LLM -> Tool
Tool -> LLM
```

How the store checks:
- Before adding `source -> target`, it checks whether a path already exists from `target` back to `source`.
- If yes, adding the edge would complete a cycle.
- The store raises `GraphStoreError`.

Why this matters:
- Causal reasoning depends on directed, acyclic structure.
- Future topological traversal depends on this guarantee.

---

## Slide 13: `has_node(node_id)` And `has_edge(edge_id)`

`has_node(node_id)`:
- Returns `True` if the graph contains that node ID.
- Uses NetworkX node lookup.

Example:

```python
store.has_node("llm:e2")  # True or False
```

`has_edge(edge_id)`:
- Returns `True` if the edge ID exists in `_edge_index`.

Example:

```python
store.has_edge("edge:e2:e3")  # True or False
```

Why this matters:
- Useful for tests.
- Useful for later graph code that needs existence checks.

---

## Slide 14: `get_node(node_id)` And `get_edge(edge_id)`

`get_node(node_id)`:
- Returns the typed node object for a node ID.
- Raises `GraphStoreError` if the node does not exist.

Example:

```python
node = store.get_node("llm:e2")
```

`get_edge(edge_id)`:
- Looks up `(source_id, target_id)` from `_edge_index`.
- Returns the typed edge object from the graph.
- Raises `GraphStoreError` if the edge does not exist.

Example:

```python
edge = store.get_edge("edge:e2:e3")
```

Why this matters:
- Retrieval returns validated typed schema objects, not unstructured graph metadata.

---

## Slide 15: `nodes()` And `edges()`

`nodes()`:
- Iterates over typed node objects stored in the graph.

Example:

```python
for node in store.nodes():
    print(node.node_id, node.node_type)
```

`edges()`:
- Iterates over typed edge objects stored in the graph.

Example:

```python
for edge in store.edges():
    print(edge.source_id, edge.edge_type, edge.target_id)
```

Why this matters:
- Future query functions can traverse typed nodes and edges.
- Export functions can serialize the graph without reaching into NetworkX internals.

---

## Slide 16: `is_acyclic()`

Purpose:
- Confirms the graph is currently a directed acyclic graph.

Implementation:
- Uses `networkx.is_directed_acyclic_graph`.

Example:

```python
store.is_acyclic()  # True
```

Why this matters:
- The store already rejects cycle-creating edges.
- This function gives tests and debugging code a direct way to verify the invariant.

Communication point:
- A provenance graph should not contain circular causality.

---

## Slide 17: `topological_node_ids()`

Purpose:
- Returns node IDs in topological order.

Example graph:

```text
llm:e2 -> tool:e3
```

Example result:

```python
["llm:e2", "tool:e3"]
```

Why this matters:
- Topological order lists causes before effects.
- Future analysis can process graph nodes in causal order.
- This only works because the graph is acyclic.

---

## Slide 18: `copy_networkx()`

Purpose:
- Returns a copy of the underlying NetworkX graph.

Example:

```python
graph_copy = store.copy_networkx()
```

Important behavior:
- Mutating the copy should not mutate the internal graph store.

Example tested behavior:

```python
graph_copy.remove_node("llm:e2")
store.has_node("llm:e2")  # still True
```

Why this matters:
- Future debugging or analysis code can inspect the graph without accidentally corrupting stored state.

---

## Slide 19: `to_dict()`

Purpose:
- Exports the graph into a JSON-friendly Python dictionary.

Output shape:

```python
{
    "nodes": [...],
    "edges": [...],
}
```

Important details:
- Nodes are sorted by `node_id`.
- Edges are sorted by `edge_id`.
- Pydantic objects are dumped using JSON mode.

Why sorting matters:
- Stable output is easier to test.
- Stable output is easier to compare during debugging.

---

## Slide 20: `to_json(indent=2)`

Purpose:
- Converts `to_dict()` output into a JSON string.

Example:

```python
json_text = store.to_json()
```

Implementation detail:
- Uses `json.dumps`.
- Sorts keys for deterministic output.
- Default indentation is 2 spaces.

Why this matters:
- JSON output is useful for debugging.
- JSON output can later support simple scripts or trace inspection tools.

---

## Slide 21: `to_dot()`

Purpose:
- Exports the graph in DOT format for visualization.

Example output:

```text
digraph causalguard {
  rankdir=LR;
  "llm:e2" [label="llm_invocation\nllm:e2"];
  "tool:e3" [label="tool_call\ntool:e3"];
  "llm:e2" -> "tool:e3" [label="invokes\nparent_event/high"];
}
```

What labels include:
- Node type.
- Node ID.
- Edge type.
- Edge derivation.
- Edge confidence.

Privacy point:
- DOT export uses compact labels.
- It does not print raw prompts, outputs, arguments, or file contents.

---

## Slide 22: `_dot_escape(value)`

Purpose:
- Escapes strings before inserting them into DOT output.

It handles:
- Backslashes.
- Double quotes.
- Newlines.

Example:

```python
_dot_escape('tool:"send"\nnext')
```

Conceptual output:

```text
tool:\"send\"\nnext
```

Why this matters:
- DOT output should remain syntactically valid even when IDs or labels contain special characters.

---

## Slide 23: Validated Test Context

Validated test command:

```bash
.venv/bin/python -m pytest -q tests/test_schema.py tests/test_graph_store.py
```

Current validated result:

```text
39 passed in 0.21s
```

Breakdown:
- `tests/test_schema.py`: 31 tests for Week 1 schema and privacy behavior.
- `tests/test_graph_store.py`: 8 tests for Week 2 graph-store behavior.

How to present this:
- Schema tests prove that only valid typed objects can be created.
- Graph-store tests prove that valid typed objects can be stored safely as a DAG.
- Together, they validate the Week 2 foundation.

---

## Slide 24: Test Helper Functions

`llm_node(node_id="llm:e2")`:
- Creates a valid LLM invocation node payload for tests.
- Used to avoid repeating the same dictionary in every test.

`tool_node(node_id="tool:e3")`:
- Creates a valid tool-call node payload.
- Includes tool name, action class, argument summary, resource, destination, and context.

`edge_payload(edge_id, source_id, target_id)`:
- Creates a valid `invokes` edge payload.
- Defaults to connecting `llm:e2` to `tool:e3`.

`graph_with_llm_and_tool()`:
- Builds a small reusable graph with one LLM node and one tool node.
- Used by edge tests that need both endpoints to exist.

Why helpers matter:
- Tests stay focused on graph behavior instead of setup noise.

---

## Slide 25: Test - `test_add_node_preserves_typed_node`

What it tests:
- `GraphStore.add_node()` inserts a node correctly.
- The stored node can be retrieved by ID.
- The returned object is the same typed node stored in the graph.

Test flow:

```python
store = GraphStore()
node = store.add_node(llm_node())
```

Expected result:
- `store.node_count == 1`
- `store.get_node("llm:e2") == node`
- `store.has_node("llm:e2") is True`

Why this matters:
- Confirms the store preserves typed node data instead of losing it as raw metadata.

---

## Slide 26: Test - `test_add_node_rejects_duplicate_node_id`

What it tests:
- The store rejects two nodes with the same `node_id`.

Test flow:

```python
store.add_node(llm_node())
store.add_node(llm_node())  # should fail
```

Expected result:
- Raises `GraphStoreError`.
- Error mentions `node already exists`.

Why this matters:
- Duplicate node IDs would make future graph queries ambiguous.
- Node identity must be stable and unique.

---

## Slide 27: Test - `test_add_edge_preserves_typed_edge`

What it tests:
- `GraphStore.add_edge()` inserts an edge correctly.
- The stored edge can be retrieved by edge ID.
- The graph maintains causal ordering.

Test setup:

```text
llm:e2
tool:e3
```

Action:

```python
edge = store.add_edge(edge_payload())
```

Expected result:
- `store.edge_count == 1`
- `store.get_edge("edge:e2:e3") == edge`
- `store.has_edge("edge:e2:e3") is True`
- `store.topological_node_ids() == ["llm:e2", "tool:e3"]`

Why this matters:
- Confirms that edges are typed, indexed, and directionally meaningful.

---

## Slide 28: Test - `test_add_edge_rejects_missing_endpoint`

What it tests:
- An edge cannot be inserted if its target node is missing.

Test setup:

```text
existing node: llm:e2
missing node: tool:e3
attempted edge: llm:e2 -> tool:e3
```

Expected result:
- Raises `GraphStoreError`.
- Error mentions `missing edge target node`.

Why this matters:
- The graph should never contain dangling edges.
- Every causal relationship must point to existing graph objects.

---

## Slide 29: Test - Duplicate Edge Rejection

Test name:
- `test_add_edge_rejects_duplicate_edge_id_and_duplicate_pair`

What it tests:
- Duplicate edge IDs are rejected.
- Duplicate source/target pairs are rejected even if the new edge has a different edge ID.

Example:

```python
store.add_edge(edge_payload("edge:e2:e3"))
store.add_edge(edge_payload("edge:e2:e3"))        # duplicate edge ID
store.add_edge(edge_payload("edge:e2:e3:again")) # duplicate source/target pair
```

Expected result:
- First duplicate raises an error for existing edge ID.
- Second duplicate raises an error for existing source/target pair.

Why this matters:
- The Week 2 store uses `networkx.DiGraph`.
- `DiGraph` supports one edge per source/target pair.
- This keeps the first prototype simple and predictable.

---

## Slide 30: Test - `test_add_edge_rejects_cycle`

What it tests:
- The graph rejects an edge that would create a cycle.

Initial valid graph:

```text
llm:e2 -> tool:e3
```

Attempted invalid edge:

```text
tool:e3 -> llm:e2
```

Expected result:
- Raises `GraphStoreError`.
- Error mentions `cycle`.
- Original graph still has only one edge.
- `store.is_acyclic()` remains true.

Why this matters:
- Causality must remain directional.
- Later effects should not become causes of earlier nodes.

---

## Slide 31: Test - `test_export_graph_as_dict_json_and_dot`

What it tests:
- The same graph can be exported in multiple debugging formats.

Exports checked:
- `to_dict()`
- `to_json()`
- `to_dot()`

Expected examples:

```python
exported["nodes"][0]["node_id"] == "llm:e2"
exported["edges"][0]["edge_type"] == "invokes"
```

DOT expectations:

```text
digraph causalguard
"llm:e2" -> "tool:e3"
parent_event/high
```

Why this matters:
- Developers can inspect graph structure without custom tooling.
- Export behavior is deterministic enough to test.

---

## Slide 32: Test - `test_copy_networkx_does_not_expose_mutable_internal_graph`

What it tests:
- `copy_networkx()` returns a copy, not the internal graph object.

Test flow:

```python
graph_copy = store.copy_networkx()
graph_copy.remove_node("llm:e2")
```

Expected result:

```python
store.has_node("llm:e2")  # True
```

Why this matters:
- External code can inspect or experiment with the graph copy.
- The real `GraphStore` remains protected from accidental mutation.

---

## Slide 33: Week 2 Design Decisions

Design choices:
- Use `networkx.DiGraph` for the first graph store.
- Keep the API small.
- Validate nodes and edges before insertion.
- Store typed schema objects inside the graph.
- Maintain an edge index by `edge_id`.
- Reject cycles immediately.
- Export JSON and DOT for debugging.

Why these choices fit Week 2:
- The goal is correctness and clarity.
- Performance optimization is intentionally deferred.
- The implementation is easy to test and explain.

---

## Slide 34: Week 2 Known Limitations

Current limitation:
- `networkx.DiGraph` allows one edge per source/target pair.

Example limitation:

```text
A --invokes--> B
A --authorizes--> B
```

This would require multiple edges between the same pair.

Current Week 2 decision:
- Keep one edge per pair for simplicity.

Possible future change:
- Use `networkx.MultiDiGraph` if later graph semantics require multiple typed edges between the same nodes.

---

## Slide 35: Week 2 Final Summary

Week 2 is validated.

What was completed:
- In-memory typed graph store.
- Validated node insertion.
- Validated edge insertion.
- Duplicate protection.
- Missing endpoint protection.
- Cycle protection.
- Typed metadata preservation.
- JSON export.
- DOT export.
- Safe NetworkX copy.

Most important takeaway:
- CausalGuard now has a reliable typed DAG foundation built and validated through Week 2.

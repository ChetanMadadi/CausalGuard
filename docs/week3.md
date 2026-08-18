# Week 3 Implementation: Graph Builder

Prepared: June 30, 2026  
Scope: Convert normalized event traces into typed provenance graph nodes and edges.  
Main files: `causalguard/graph/builder.py`, `tests/test_graph_builder.py`, `causalguard/graph/__init__.py`, `README.md`.

## 1. Week 3 Goal

Week 3 implements the graph-builder layer on top of the validated Week 1 schemas and Week 2 `GraphStore`.

Before Week 3, graph nodes and edges had to be inserted manually. Week 3 adds code that takes normalized event payloads and automatically builds the typed provenance DAG.

The main transformation is:

```text
normalized events -> typed nodes -> typed causal edges -> GraphStore DAG
```

The builder does not implement policy detection, graph queries, visualization scripts, JSONL trace loading, or temporal-window inference. Those remain later work.

## 2. Public API Implemented

### `GraphBuilder`

Implemented in `causalguard/graph/builder.py`.

Purpose:
- Convert normalized events into graph structure.
- Insert typed nodes and edges into an existing `GraphStore`.
- Keep event history so edges can be created when parent/context information becomes available.

Constructor:

```python
GraphBuilder(graph_store: GraphStore, config: GraphBuilderConfig | None = None)
```

The builder receives a `GraphStore` instead of creating one internally. This keeps storage ownership explicit and makes tests simple.

### `GraphBuilder.process_event(event)`

Accepts one event:
- either a `NormalizedEvent`
- or a dictionary that validates as `NormalizedEvent`

Main steps:
- Validate the event with `NormalizedEvent.model_validate`.
- Reject duplicate event IDs.
- Convert the event into one or more typed graph nodes.
- Store internal mappings from event ID to graph node ID.
- Reconcile causal, access, and authorization edges.
- Return the same `GraphStore`.

### `GraphBuilder.process_trace(events)`

Accepts an iterable of events.

Main steps:
- Calls `process_event` for each event in order.
- Returns the same `GraphStore`.

This supports synthetic traces like:

```text
user_input -> llm_invocation -> tool_call -> data_access -> network_send
```

### `GraphBuilderConfig`

Implemented as a frozen dataclass.

```python
@dataclass(frozen=True)
class GraphBuilderConfig:
    infer_causal_context_edges: bool = True
```

Design choice:
- Context-based inference is enabled by default.
- If an event has no `parent_event_id`, the builder may infer lower-confidence causal edges from shared `causal_context_id`.

### `GraphBuilderError`

Custom error raised when an event cannot be converted into graph structure.

Examples:
- Duplicate event ID.
- Missing required conversion attribute such as `tool_name`.
- Underlying `GraphStoreError` during node or edge insertion.

## 3. Internal State Implemented

The builder maintains three internal indexes:

```python
self._events_by_id: dict[str, NormalizedEvent]
self._primary_node_by_event_id: dict[str, str]
self._node_ids_by_event_id: dict[str, list[str]]
```

Purpose:
- `_events_by_id` stores normalized events already processed.
- `_primary_node_by_event_id` maps an event to its main graph node.
- `_node_ids_by_event_id` records every graph node created from an event.

Design choice:
- An event can create more than one node. For example, `data_access` creates both a system-operation node and a data-object node.
- The primary node is used for causal edges. For `data_access`, the primary node is the system-operation node.

## 4. Deterministic Node ID Design

Week 3 uses deterministic graph node IDs.

Implemented IDs:

| Event or Object | Node ID |
|---|---|
| LLM invocation event `e2` | `llm:e2` |
| Tool call event `e3` | `tool:e3` |
| System/data/network/device event `e4` | `sys:e4` |
| Human approval event `e6` | `approval:e6` |
| Resource `contacts.csv` | `data:resource:contacts.csv` |
| Destination `external.example` | `data:destination:external.example` |

Design choice:
- IDs are predictable and easy to inspect in tests and DOT/JSON output.
- Data-object nodes are reused if the same deterministic ID already exists.
- The implementation does not currently hash or escape resource/destination IDs beyond relying on existing schema validation and DOT escaping at export time.

## 5. Event-To-Node Conversion Implemented

### `user_input`

Behavior:
- Validated and indexed in `_events_by_id`.
- Does not create a graph node.

Design choice:
- Week 3 keeps `user_input` as event context only.
- This allows an LLM event to reference a user input as `parent_event_id` without forcing a separate user-input graph node.

### `llm_invocation`

Creates:
- `LLMInvocationNode`

Node ID:

```text
llm:{event_id}
```

Mapped fields:
- `model_name`
- `prompt_hash`
- `output_hash`
- `token_count`
- `timestamp`
- `agent_id`
- `session_id`
- `causal_context_id`

Privacy choice:
- The builder maps hashes only. Raw prompts and raw outputs remain blocked by the existing event/schema privacy validation.

### `tool_call`

Creates:
- `ToolCallNode`

Node ID:

```text
tool:{event_id}
```

Mapped fields:
- `tool_name`
- `action_class`
- `argument_summary`
- `target_resource`
- `destination`
- `timestamp`
- `agent_id`
- `session_id`
- `causal_context_id`

Required conversion field:
- `tool_name`

Design choice:
- Missing `tool_name` raises `GraphBuilderError`.
- `argument_summary` defaults to `{}` if omitted.

### `data_access`

Creates:
- `SystemOperationNode`
- resource `DataObjectNode`

Primary node ID:

```text
sys:{event_id}
```

Data object ID:

```text
data:resource:{resource_id}
```

Required conversion fields:
- `operation_type`
- `resource_id`

Mapped system-operation fields:
- `operation_type`
- `resource_id`
- `destination`
- `syscall_kind`
- `byte_count`
- `timestamp`
- `agent_id`
- `session_id`
- `causal_context_id`
- `confidence`, defaulting to `"high"`

Mapped data-object fields:
- `resource_id`
- `object_kind`, defaulting to `"resource"` unless provided
- `content_hash`
- `sensitivity`, defaulting to `"unknown"` unless provided
- `trust_label`, defaulting to `"unknown"` unless provided
- `owner`

### `network_send`

Creates:
- `SystemOperationNode`
- destination `DataObjectNode`
- optionally resource `DataObjectNode` if `resource_id` is present

Primary node ID:

```text
sys:{event_id}
```

Destination node ID:

```text
data:destination:{destination}
```

Required conversion field:
- `destination`

Defaults:
- `operation_type` defaults to `"network_send"`.
- destination object kind defaults to `"destination"`.
- sensitivity defaults to `"unknown"`.

### `system_operation`

Creates:
- `SystemOperationNode`
- optional resource data object if `resource_id` is present
- optional destination data object if `destination` is present

Required conversion field:
- `operation_type`

Design choice:
- Generic system operations can touch resources or destinations, but they do not have to.

### `device_command`

Creates:
- `SystemOperationNode`
- optional resource/device data object
- optional destination data object

Defaults:
- `operation_type` defaults to `"device_command"`.
- resource can come from `resource_id` or `device_id`.

Design choice:
- Device commands are treated as system operations in the first version.

### `human_approval`

Creates:
- `HumanApprovalNode`

Node ID:

```text
approval:{event_id}
```

Required conversion fields:
- `approver_id`
- `action_class`

Mapped fields:
- `resource_scope`
- `destination_scope`
- `timestamp`
- `expiration`
- `session_id`
- `causal_context_id`

Design choice:
- Approval expiration is validated by the node schema, but the builder does not decide whether an approval is currently expired. Time-valid approval checking is left for later query/policy logic.

## 6. Data Object Handling Implemented

Data objects are represented with an internal `_DataRef` dataclass before insertion.

```python
@dataclass(frozen=True)
class _DataRef:
    node_id: str
    resource_id: str
    object_kind: str
    content_hash: str | None
    sensitivity: str
    trust_label: str | None
    owner: str | None
```

Design choices:
- A data object is created only if an event references a resource or destination.
- `_ensure_data_node` reuses an existing data-object node when the deterministic ID already exists.
- Resource nodes and destination nodes have different prefixes so `contacts.csv` as a resource and `contacts.csv` as a destination do not collide.

## 7. Edge Construction Implemented

The builder reconciles edges after each processed event.

Implemented edge families:
- parent-event causal edges
- shared-context inferred causal edges
- system-to-data access edges
- approval authorization edges

### Parent-event edges

Implemented in `_add_parent_edge`.

Rule 1:

```text
llm_invocation parent -> tool_call child
```

Creates:

```text
LLM node --invokes--> Tool node
```

Edge metadata:
- `edge_type = "invokes"`
- `derivation = "parent_event"`
- `confidence = "high"`
- `evidence_ref = child event_id`

Rule 2:

```text
tool_call parent -> system/data/network/device child
```

Creates:

```text
Tool node --triggers--> System operation node
```

Edge metadata:
- `edge_type = "triggers"`
- `derivation = "parent_event"`
- `confidence = "high"`
- `evidence_ref = child event_id`

Design choice:
- Parent edges are only created for supported causal type pairs.
- If the parent event is not processed yet or does not create a graph node, no edge is created at that moment.
- Because `_reconcile_edges` runs after every event, edges can be added later when enough events exist.

### Shared causal-context fallback edges

Implemented in `_add_causal_context_edge`.

Used only when:
- `infer_causal_context_edges` is enabled
- event has no `parent_event_id`
- event has a `causal_context_id`

Rule 1:

```text
latest prior LLM event in same context -> tool event
```

Creates:

```text
LLM node --invokes--> Tool node
```

Rule 2:

```text
latest prior tool event in same context -> system/data/network/device event
```

Creates:

```text
Tool node --triggers--> System operation node
```

Edge metadata:
- `derivation = "causal_context"`
- `confidence = "medium"`
- `evidence_ref = target event_id`

Design choice:
- Context fallback is lower confidence than explicit parent links.
- The builder selects the latest prior event of the expected type using timestamp and event ID.
- Temporal-window fallback is intentionally not implemented.

### Access edges

Implemented in `_add_access_edges`.

Used for:
- `system_operation`
- `data_access`
- `network_send`
- `device_command`

Rule:

```text
System operation node -> Data object node
```

Creates:

```text
sys:e4 --accesses--> data:resource:contacts.csv
sys:e5 --accesses--> data:destination:external.example
```

Edge metadata:
- `edge_type = "accesses"`
- `derivation = "parent_event"`
- `confidence = "high"`
- `evidence_ref = event_id`

Design choice:
- Access edges are treated as direct evidence from the system/data/network/device event.

### Authorization edges

Implemented in `_add_authorization_edges` and `_approval_matches_tool`.

Rule:

```text
Human approval node -> matching tool-call node
```

Creates:

```text
approval:e6 --authorizes--> tool:e3
```

Edge metadata:
- `edge_type = "authorizes"`
- `derivation = "explicit_approval_scope"`
- `confidence = "high"`
- `evidence_ref = approval event_id`

Approval matching requires:
- approval `action_class` equals tool `action_class`
- approval `resource_scope`, if present, equals tool `target_resource`
- approval `destination_scope`, if present, equals tool `destination`
- approval `session_id`, if present, equals tool `session_id`
- approval `causal_context_id`, if present, equals tool `causal_context_id`

Design choice:
- Authorization edges point to tool calls, not downstream system operations.
- Time-validity checking is deferred to later policy/query work.

## 8. Duplicate Edge Handling

The builder avoids adding duplicate edges during repeated reconciliation.

Implemented behavior:
- If an edge ID already exists, `_add_edge` returns without inserting.
- If the source/target pair already exists in `GraphStore`, `_add_edge` returns without inserting.
- Otherwise the builder delegates to `GraphStore.add_edge`.

Design choice:
- This respects the Week 2 `networkx.DiGraph` decision: one edge per source/target pair.
- It keeps repeated `_reconcile_edges` calls idempotent.

## 9. Error Handling Implemented

`GraphBuilderError` is used for builder-level conversion failures.

Implemented failure cases:
- duplicate event ID
- missing required conversion attribute
- graph-store insertion errors wrapped from `GraphStoreError`

Example:

```text
tool_call event without tool_name -> GraphBuilderError
```

Design choice:
- Builder errors are clear and domain-specific.
- Underlying graph-store errors are not leaked directly to callers.

## 10. Privacy Behavior

Week 3 preserves the existing privacy boundary.

How:
- Every input event is validated as `NormalizedEvent`.
- `NormalizedEvent` recursively rejects raw-content-like keys.
- Node schemas also validate sensitive metadata fields such as `argument_summary`.

Stored examples:
- `prompt_hash`
- `output_hash`
- `content_hash`
- `argument_summary`
- `resource_id`
- `trust_label`
- `sensitivity`

Rejected by existing validation:
- raw prompt text under `prompt`
- raw model output under `output`
- raw message body under `message`
- raw file content under `file_content`
- credentials/secrets such as `password`, `token`, `api_key`, `secret`
- keys beginning with `raw_`
- keys ending with `_raw`

Design choice:
- Week 3 does not add a new content classifier. It relies on the conservative key-based privacy guard from Weeks 1-2.

## 11. Public Export Update

`causalguard/graph/__init__.py` now exports:

```python
GraphBuilder
GraphBuilderConfig
GraphBuilderError
GraphStore
GraphStoreError
```

Purpose:
- Allows consumers to import builder and store primitives from `causalguard.graph`.

Example:

```python
from causalguard.graph import GraphBuilder, GraphStore
```

## 12. Tests Implemented

Week 3 adds `tests/test_graph_builder.py`.

The test file includes helper event builders:
- `event_payload`
- `llm_event`
- `tool_event`
- `data_access_event`
- `network_send_event`
- `approval_event`
- `matching_edges`

### Test 1: LLM event creates LLM node

Test:
- `test_llm_event_creates_llm_node`

Validates:
- `llm_invocation` event creates `LLMInvocationNode`
- node ID is `llm:e2`
- `model_name` and `prompt_hash` are preserved
- graph has one node

### Test 2: Tool event creates tool node and parent invokes edge

Test:
- `test_tool_event_creates_tool_node_and_parent_invokes_edge`

Validates:
- `tool_call` event creates `ToolCallNode`
- tool name is preserved
- parent LLM event creates `INVOKES` edge
- edge has `PARENT_EVENT` derivation
- edge has `HIGH` confidence
- evidence reference is the child tool event ID

Expected graph:

```text
llm:e2 --invokes--> tool:e3
```

### Test 3: Data access creates system/data nodes and edges

Test:
- `test_data_access_creates_system_data_and_edges`

Validates:
- `data_access` creates `SystemOperationNode`
- `data_access` creates `DataObjectNode`
- system node operation type is `file_read`
- data node sensitivity is `PII`
- tool triggers system operation
- system operation accesses data object

Expected graph:

```text
tool:e3 --triggers--> sys:e4
sys:e4 --accesses--> data:resource:contacts.csv
```

### Test 4: Network send creates system/destination nodes and access edge

Test:
- `test_network_send_creates_system_destination_and_access_edge`

Validates:
- `network_send` creates `SystemOperationNode`
- `network_send` creates destination `DataObjectNode`
- system operation type defaults to `network_send`
- destination trust label is `untrusted`
- tool triggers network-send system operation
- system operation accesses destination object

Expected graph:

```text
tool:e3 --triggers--> sys:e5
sys:e5 --accesses--> data:destination:external.example
```

### Test 5: Shared causal context creates lower-confidence edges

Test:
- `test_shared_causal_context_creates_lower_confidence_edges_without_parent`

Validates:
- events without parent links can still be connected by shared context
- LLM-to-tool context edge is created
- tool-to-system context edge is created
- derivation is `CAUSAL_CONTEXT`
- confidence is `MEDIUM`

Expected graph:

```text
llm:e2 --invokes / causal_context / medium--> tool:e3
tool:e3 --triggers / causal_context / medium--> sys:e4
```

### Test 6: Matching human approval creates authorizes edge

Test:
- `test_matching_human_approval_creates_authorizes_edge`

Validates:
- `human_approval` creates `HumanApprovalNode`
- matching approval creates `AUTHORIZES` edge
- derivation is `EXPLICIT_APPROVAL_SCOPE`
- confidence is `HIGH`
- evidence reference is the approval event ID

Expected graph:

```text
approval:e6 --authorizes--> tool:e3
```

### Test 7: Contacts-upload trace builds expected acyclic graph

Test:
- `test_contacts_upload_trace_builds_expected_acyclic_graph`

Trace:

```text
user_input -> llm_invocation -> tool_call -> data_access
                                      \
                                       -> network_send
```

Expected graph:

```text
LLM -> Tool -> File read -> contacts.csv
            -> Network send -> external.example
```

Validates:
- graph is acyclic
- graph has 6 nodes
- graph has 5 edges
- node types include LLM, tool, system operation, and data object
- edge counts are:
  - 1 `INVOKES`
  - 2 `TRIGGERS`
  - 2 `ACCESSES`

### Test 8: Missing required conversion attribute raises builder error

Test:
- `test_missing_required_conversion_attribute_raises_builder_error`

Validates:
- tool event without `tool_name` fails clearly
- error type is `GraphBuilderError`
- error message mentions `tool_name`

Purpose:
- Incomplete events should not silently create invalid graph nodes.

## 13. Test Results

Week 3 test command:

```bash
.venv/bin/python -m pytest -q tests/test_graph_builder.py
```

Result:

```text
8 passed in 0.24s
```

Full suite command:

```bash
.venv/bin/python -m pytest -q
```

Result:

```text
47 passed in 0.11s
```

Interpretation:
- The new Week 3 builder tests pass.
- Existing Week 1 schema tests and Week 2 graph-store tests still pass with the builder added.

## 14. Current Non-Goals And Deferred Work

Not implemented in Week 3:
- temporal-window fallback
- policy query functions
- PII upload detection
- JSONL trace files as formal examples
- mock collector
- visualization scripts
- OpenTelemetry or real agent runtime integration
- production graph storage or indexing

Design choice:
- Week 3 focuses only on converting normalized synthetic events into a typed provenance DAG.

## 15. Week 3 Summary

Implemented:
- `GraphBuilder`
- `GraphBuilderConfig`
- `GraphBuilderError`
- event-by-event processing
- trace processing
- deterministic node ID conversion
- LLM/tool/system/data/network/device/approval node conversion
- data-object creation and reuse
- parent-event causal edges
- shared-context inferred edges
- system-to-data access edges
- approval-to-tool authorization edges
- builder-level error handling
- public exports from `causalguard.graph`
- 8 focused builder tests

Main outcome:

```text
normalized synthetic trace -> typed acyclic provenance graph
```

This completes the Week 3 graph-builder foundation and prepares the project for later query, policy, trace-loading, and visualization work.

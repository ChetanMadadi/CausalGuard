# synth_example

Source trace: `examples/test_examples/synth_example.jsonl`

## Build Result

- Events read: 5
- Graph nodes: 5
- Graph edges: 4
- Acyclic: True

## Node Types

- `data_object`: 1
- `llm_invocation`: 1
- `system_operation`: 2
- `tool_call`: 1

## Edge Types

- `invokes`: 1
- `read`: 1
- `triggers`: 2

## Edge Derivations

- `explicit_data_reference`: 1
- `parent_event`: 3

## Edge Confidence

- `high`: 4

## Outputs

- JSON graph: `synth_example.graph.json`
- DOT graph: `synth_example.graph.dot`
- Mermaid graph: `synth_example.graph.mmd`
- SVG visualization: `synth_example.graph.svg`

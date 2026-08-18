"""Build graph outputs for JSONL traces in examples/test_examples."""

from __future__ import annotations

import json
from collections import Counter
from html import escape
from pathlib import Path
from typing import Any

from causalguard.graph import GraphBuilder, GraphStore


IN_DIR = Path("examples/test_examples")
OUT_DIR = Path("examples/test_examples_output")

TYPE_X = {
    "llm_invocation": 80,
    "tool_call": 380,
    "system_operation": 700,
    "data_object": 1010,
    "human_approval": 80,
}
TYPE_COLOR = {
    "llm_invocation": "#dbeafe",
    "tool_call": "#dcfce7",
    "system_operation": "#fef3c7",
    "data_object": "#fee2e2",
    "human_approval": "#ede9fe",
}
TYPE_STROKE = {
    "llm_invocation": "#2563eb",
    "tool_call": "#16a34a",
    "system_operation": "#d97706",
    "data_object": "#dc2626",
    "human_approval": "#7c3aed",
}
TYPE_LABEL = {
    "llm_invocation": "LLM",
    "tool_call": "Tool",
    "system_operation": "SysOp",
    "data_object": "Data",
    "human_approval": "Approval",
}
EDGE_COLOR = {
    "invokes": "#2563eb",
    "triggers": "#d97706",
    "read": "#dc2626",
    "write": "#be123c",
    "produces": "#0891b2",
    "input_to": "#0284c7",
    "payload_of": "#e11d48",
    # Legacy coarse relation retained for older imported graphs.
    "accesses": "#dc2626",
    "authorizes": "#7c3aed",
}


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    results = []
    for path in sorted(IN_DIR.glob("*.jsonl")):
        results.append(write_outputs(path))

    write_index(results)
    print(json.dumps(results, indent=2))


def write_outputs(path: Path) -> dict[str, Any]:
    events = load_jsonl(path)
    store = GraphStore()
    builder = GraphBuilder(store)
    builder.process_trace(events)

    stem = path.stem
    json_path = OUT_DIR / f"{stem}.graph.json"
    dot_path = OUT_DIR / f"{stem}.graph.dot"
    mermaid_path = OUT_DIR / f"{stem}.graph.mmd"
    svg_path = OUT_DIR / f"{stem}.graph.svg"
    summary_path = OUT_DIR / f"{stem}.summary.md"

    json_path.write_text(store.to_json(indent=2) + "\n")
    dot_path.write_text(store.to_dot() + "\n")
    mermaid_path.write_text(store.to_mermaid() + "\n")
    svg_path.write_text(build_svg(store, stem) + "\n")
    summary_path.write_text(
        build_summary(
            path,
            store,
            json_path,
            dot_path,
            mermaid_path,
            svg_path,
            len(events),
        )
    )

    return {
        "trace": str(path),
        "events": len(events),
        "nodes": store.node_count,
        "edges": store.edge_count,
        "acyclic": store.is_acyclic(),
        "json": str(json_path),
        "dot": str(dot_path),
        "mermaid": str(mermaid_path),
        "svg": str(svg_path),
        "summary": str(summary_path),
    }


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    events = []
    for line_no, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_no}: invalid JSON: {exc}") from exc
    return events


def build_summary(
    path: Path,
    store: GraphStore,
    json_path: Path,
    dot_path: Path,
    mermaid_path: Path,
    svg_path: Path,
    event_count: int,
) -> str:
    node_counts = Counter(node.node_type.value for node in store.nodes())
    edge_counts = Counter(edge.edge_type.value for edge in store.edges())
    derivation_counts = Counter(edge.derivation.value for edge in store.edges())
    confidence_counts = Counter(edge.confidence.value for edge in store.edges())

    lines = [
        f"# {path.stem}",
        "",
        f"Source trace: `{path}`",
        "",
        "## Build Result",
        "",
        f"- Events read: {event_count}",
        f"- Graph nodes: {store.node_count}",
        f"- Graph edges: {store.edge_count}",
        f"- Acyclic: {store.is_acyclic()}",
        "",
        "## Node Types",
        "",
    ]
    lines.extend(format_counter(node_counts))
    lines.extend(["", "## Edge Types", ""])
    lines.extend(format_counter(edge_counts))
    lines.extend(["", "## Edge Derivations", ""])
    lines.extend(format_counter(derivation_counts))
    lines.extend(["", "## Edge Confidence", ""])
    lines.extend(format_counter(confidence_counts))
    lines.extend(
        [
            "",
            "## Outputs",
            "",
            f"- JSON graph: `{json_path.name}`",
            f"- DOT graph: `{dot_path.name}`",
            f"- Mermaid graph: `{mermaid_path.name}`",
            f"- SVG visualization: `{svg_path.name}`",
        ]
    )
    return "\n".join(lines) + "\n"


def format_counter(counter: Counter[str]) -> list[str]:
    if not counter:
        return ["- none"]
    return [f"- `{key}`: {value}" for key, value in sorted(counter.items())]


def write_index(results: list[dict[str, Any]]) -> None:
    lines = ["# Test Example Graph Outputs", ""]
    for result in results:
        stem = Path(result["trace"]).stem
        lines.extend(
            [
                f"## {stem}",
                "",
                f"- Source trace: `{result['trace']}`",
                f"- Events: {result['events']}",
                f"- Nodes: {result['nodes']}",
                f"- Edges: {result['edges']}",
                f"- Acyclic: {result['acyclic']}",
                f"- Summary: `{Path(result['summary']).name}`",
                f"- JSON: `{Path(result['json']).name}`",
                f"- DOT: `{Path(result['dot']).name}`",
                f"- Mermaid: `{Path(result['mermaid']).name}`",
                f"- SVG: `{Path(result['svg']).name}`",
                "",
            ]
        )
    (OUT_DIR / "index.md").write_text("\n".join(lines))


def build_svg(store: GraphStore, title: str) -> str:
    nodes = sorted(list(store.nodes()), key=node_sort_key)
    edges = list(store.edges())
    positions: dict[str, tuple[int, int]] = {}
    row_height = 78
    width = 1240
    height = max(260, 120 + len(nodes) * row_height)

    for index, node in enumerate(nodes):
        node_type = node.node_type.value
        x = TYPE_X.get(node_type, 80)
        y = 90 + index * row_height
        positions[node.node_id] = (x, y)

    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<defs>",
        '<marker id="arrow" markerWidth="10" markerHeight="10" refX="9" refY="3" orient="auto" markerUnits="strokeWidth">',
        '<path d="M0,0 L0,6 L9,3 z" fill="#334155" />',
        "</marker>",
        "<style>",
        "text { font-family: Arial, Helvetica, sans-serif; fill: #0f172a; }",
        ".title { font-size: 22px; font-weight: 700; }",
        ".subtitle { font-size: 13px; fill: #475569; }",
        ".node-type { font-size: 13px; font-weight: 700; }",
        ".node-id { font-size: 11px; fill: #334155; }",
        ".edge-label { font-size: 10px; fill: #334155; }",
        "</style>",
        "</defs>",
        '<rect width="100%" height="100%" fill="#f8fafc"/>',
        f'<text x="40" y="34" class="title">{escape(title)}</text>',
        f'<text x="40" y="56" class="subtitle">nodes={store.node_count}, edges={store.edge_count}, acyclic={store.is_acyclic()}</text>',
        '<text x="80" y="78" class="subtitle">LLM</text>',
        '<text x="380" y="78" class="subtitle">Tool</text>',
        '<text x="700" y="78" class="subtitle">System operation</text>',
        '<text x="1010" y="78" class="subtitle">Data object</text>',
    ]

    pair_counts = Counter((edge.source_id, edge.target_id) for edge in edges)
    pair_indexes: Counter[tuple[str, str]] = Counter()
    for edge in edges:
        if edge.source_id not in positions or edge.target_id not in positions:
            continue
        sx, sy = positions[edge.source_id]
        tx, ty = positions[edge.target_id]
        source_type = store.get_node(edge.source_id).node_type.value
        target_type = store.get_node(edge.target_id).node_type.value
        start_x = sx + 190 if TYPE_X.get(source_type, sx) <= TYPE_X.get(target_type, tx) else sx
        end_x = tx if start_x <= tx else tx + 190
        pair = (edge.source_id, edge.target_id)
        parallel_offset = (pair_indexes[pair] - (pair_counts[pair] - 1) / 2) * 14
        pair_indexes[pair] += 1
        color = EDGE_COLOR.get(edge.edge_type.value, "#334155")
        mid_x = (start_x + end_x) / 2 + parallel_offset
        mid_y = (sy + ty) / 2 + parallel_offset - 4
        label = f"{edge.edge_type.value}/{edge.derivation.value}/{edge.confidence.value}"
        lines.append(
            f'<path d="M {start_x} {sy} C {mid_x} {sy}, {mid_x} {ty}, {end_x} {ty}" '
            f'stroke="{color}" stroke-width="1.6" fill="none" marker-end="url(#arrow)" opacity="0.72">'
            f"<title>{escape(edge.edge_id)}: {escape(edge.source_id)} -> {escape(edge.target_id)}</title></path>"
        )
        lines.append(
            f'<text x="{mid_x + 4:.1f}" y="{mid_y:.1f}" class="edge-label">{escape(label)}</text>'
        )

    for node in nodes:
        x, y = positions[node.node_id]
        node_type = node.node_type.value
        fill = TYPE_COLOR.get(node_type, "#e2e8f0")
        stroke = TYPE_STROKE.get(node_type, "#64748b")
        label = TYPE_LABEL.get(node_type, node_type)
        lines.extend(
            [
                f"<g><title>{escape(node.model_dump_json())}</title>",
                f'<rect x="{x}" y="{y - 24}" width="190" height="48" rx="6" fill="{fill}" stroke="{stroke}" stroke-width="1.4"/>',
                f'<text x="{x + 10}" y="{y - 5}" class="node-type">{escape(label)}</text>',
                f'<text x="{x + 10}" y="{y + 13}" class="node-id">{escape(short_id(node.node_id))}</text>',
                "</g>",
            ]
        )

    lines.append("</svg>")
    return "\n".join(lines)


def node_sort_key(node: Any) -> tuple[float, str]:
    return (float(getattr(node, "timestamp", 0.0) or 0.0), node.node_id)


def short_id(value: str, max_len: int = 34) -> str:
    if len(value) <= max_len:
        return value
    return value[:16] + "..." + value[-12:]


if __name__ == "__main__":
    main()

import json
import hashlib
from pathlib import Path
from datetime import datetime
from datasets import load_dataset

DATASET_NAME = "Exgentic/agent-llm-traces"
BENCHMARK = "appworld"
MAX_TRACES = 10
OUT_DIR = Path("examples/exgentic_appworld")


def sha256_id(value):
    if value is None:
        return None
    if not isinstance(value, str):
        value = json.dumps(value, sort_keys=True, default=str)
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def parse_json_maybe(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def timestamp_to_float(value):
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def safe_int(value):
    try:
        return int(value)
    except Exception:
        return None


def short_hash(value, prefix="id"):
    h = sha256_id(value)
    if h is None:
        return None
    return f"{prefix}:{h.split(':', 1)[1][:16]}"


def find_tool_calls(obj):
    """
    Recursively find common tool-call shapes inside gen_ai.output.messages.
    Handles:
      - {"type": "tool_call", "name": ..., "arguments": ...}
      - {"tool_calls": [...]}
      - OpenAI-like {"function": {"name": ..., "arguments": ...}}
    """
    obj = parse_json_maybe(obj)
    found = []

    def walk(x):
        x = parse_json_maybe(x)

        if isinstance(x, dict):
            if isinstance(x.get("tool_calls"), list):
                for item in x["tool_calls"]:
                    found.append(item)

            if x.get("type") in {"tool_call", "function_call"}:
                found.append(x)

            if isinstance(x.get("function"), dict) and x["function"].get("name"):
                found.append(x)

            for v in x.values():
                walk(v)

        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(obj)
    return found


def get_tool_name(tool_call):
    if not isinstance(tool_call, dict):
        return "unknown_tool"

    if isinstance(tool_call.get("function"), dict):
        return tool_call["function"].get("name") or "unknown_tool"

    return (
        tool_call.get("name")
        or tool_call.get("tool_name")
        or tool_call.get("function_name")
        or "unknown_tool"
    )


def get_tool_arguments(tool_call):
    if not isinstance(tool_call, dict):
        return None

    if "arguments" in tool_call:
        return parse_json_maybe(tool_call["arguments"])

    if isinstance(tool_call.get("function"), dict):
        return parse_json_maybe(tool_call["function"].get("arguments"))

    return None


def summarize_arguments(args):
    """
    Do NOT store raw tool arguments. Store only hash + keys.
    This follows the CausalGuard privacy rule: hashes/summaries, not raw prompts or raw user/tool data.
    """
    summary = {
        "argument_hash": sha256_id(args),
        "argument_type": type(args).__name__,
    }

    if isinstance(args, dict):
        summary["argument_keys"] = sorted([str(k) for k in args.keys()])[:30]
    else:
        summary["argument_keys"] = []

    return summary


def guess_target_resource(args):
    if not isinstance(args, dict):
        return None

    for key in [
        "file",
        "filename",
        "path",
        "resource",
        "resource_id",
        "document",
        "document_id",
        "calendar_id",
        "order_id",
        "user_id",
        "account_id",
    ]:
        if key in args:
            return short_hash({"key": key, "value": args[key]}, prefix=f"arg_{key}")

    return None


def guess_destination(args):
    if not isinstance(args, dict):
        return None

    for key in [
        "destination",
        "url",
        "endpoint",
        "recipient",
        "email",
        "phone",
        "phone_number",
        "address",
    ]:
        if key in args:
            return short_hash({"key": key, "value": args[key]}, prefix=f"arg_{key}")

    return None


def is_llm_span(span):
    attrs = span.get("attributes") or {}
    name = (span.get("name") or "").lower()

    return (
        "gen_ai.request.model" in attrs
        or "gen_ai.response.model" in attrs
        or attrs.get("gen_ai.operation.name") == "chat"
        or name.startswith("chat ")
        or "chat" in name
    )


def convert_row_to_causalguard_events(row):
    session_id = row["session_id"]
    causal_context_id = f"exgentic:{session_id}"
    agent_id = row.get("harness")

    spans = row.get("spans") or []
    spans = sorted(spans, key=lambda s: timestamp_to_float(s.get("start_time")))

    events = []
    span_to_event = {}

    earliest_ts = min(
        [timestamp_to_float(s.get("start_time")) for s in spans] or [0.0]
    )

    first_input_messages = None
    for span in spans:
        attrs = span.get("attributes") or {}
        if attrs.get("gen_ai.input.messages"):
            first_input_messages = attrs.get("gen_ai.input.messages")
            break

    user_event_id = f"{session_id}:user_input"

    events.append(
        {
            "event_id": user_event_id,
            "timestamp": max(0.0, earliest_ts - 0.001),
            "event_type": "user_input",
            "agent_id": agent_id,
            "session_id": session_id,
            "causal_context_id": causal_context_id,
            "parent_event_id": None,
            "attributes": {
                "benchmark": row.get("benchmark"),
                "harness": row.get("harness"),
                "models": row.get("models"),
                "input_hash": sha256_id(first_input_messages),
                "source_dataset": DATASET_NAME,
            },
        }
    )

    for span in spans:
        if not is_llm_span(span):
            continue

        span_id = span.get("span_id")
        parent_span_id = span.get("parent_span_id")
        attrs = span.get("attributes") or {}

        event_id = f"{session_id}:span:{span_id}:llm"
        parent_event_id = span_to_event.get(parent_span_id, user_event_id)

        input_tokens = safe_int(attrs.get("gen_ai.usage.input_tokens")) or 0
        output_tokens = safe_int(attrs.get("gen_ai.usage.output_tokens")) or 0
        token_count = input_tokens + output_tokens if input_tokens or output_tokens else None

        llm_event = {
            "event_id": event_id,
            "timestamp": timestamp_to_float(span.get("start_time")),
            "event_type": "llm_invocation",
            "agent_id": agent_id,
            "session_id": session_id,
            "causal_context_id": causal_context_id,
            "parent_event_id": parent_event_id,
            "attributes": {
                "model_name": (
                    attrs.get("gen_ai.response.model")
                    or attrs.get("gen_ai.request.model")
                ),
                "prompt_hash": sha256_id(attrs.get("gen_ai.input.messages")),
                "output_hash": sha256_id(attrs.get("gen_ai.output.messages")),
                "token_count": token_count,
                "span_id": span_id,
                "parent_span_id": parent_span_id,
                "status_code": (span.get("status") or {}).get("code"),
            },
        }

        events.append(llm_event)
        span_to_event[span_id] = event_id

        tool_calls = find_tool_calls(attrs.get("gen_ai.output.messages"))

        for i, tool_call in enumerate(tool_calls):
            args = get_tool_arguments(tool_call)
            tool_name = get_tool_name(tool_call)

            tool_event_id = f"{session_id}:span:{span_id}:tool:{i}"

            events.append(
                {
                    "event_id": tool_event_id,
                    "timestamp": timestamp_to_float(span.get("end_time")) + (i * 0.0001),
                    "event_type": "tool_call",
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "causal_context_id": causal_context_id,
                    "parent_event_id": event_id,
                    "attributes": {
                        "tool_name": tool_name,
                        "action_class": tool_name,
                        "argument_summary": summarize_arguments(args),
                        "target_resource": guess_target_resource(args),
                        "destination": guess_destination(args),
                        "source_span_id": span_id,
                    },
                }
            )

    return events


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Loading {DATASET_NAME} in streaming mode...")
    ds = load_dataset(DATASET_NAME, split="train", streaming=True)

    count = 0
    manifest = []

    for row in ds:
        if row.get("benchmark") != BENCHMARK:
            continue

        events = convert_row_to_causalguard_events(row)

        # Keep traces that have at least one tool call.
        has_tool_call = any(e["event_type"] == "tool_call" for e in events)
        if not has_tool_call:
            continue

        out_file = OUT_DIR / f"exgentic_{BENCHMARK}_{count:02d}.jsonl"

        with out_file.open("w", encoding="utf-8") as f:
            for event in events:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")

        manifest.append(
            {
                "file": str(out_file),
                "session_id": row.get("session_id"),
                "benchmark": row.get("benchmark"),
                "harness": row.get("harness"),
                "num_events": len(events),
                "num_tool_calls": sum(e["event_type"] == "tool_call" for e in events),
                "num_llm_invocations": sum(e["event_type"] == "llm_invocation" for e in events),
            }
        )

        print(f"Wrote {out_file}")
        count += 1

        if count >= MAX_TRACES:
            break

    manifest_file = OUT_DIR / "manifest.json"
    manifest_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print()
    print(f"Done. Wrote {count} traces to {OUT_DIR}")
    print(f"Manifest: {manifest_file}")


if __name__ == "__main__":
    main()
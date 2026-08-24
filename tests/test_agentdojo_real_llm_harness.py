from __future__ import annotations

import json

import pytest

pytest.importorskip("agentdojo")

from agentdojo.functions_runtime import FunctionCall
from agentdojo.types import ChatAssistantMessage

from causalguard.integrations.agentdojo.common import stable_hash
from causalguard.integrations.agentdojo.runtime import RuntimeCallObservation
from scripts.run_agentdojo_real_llm_provenance import (
    CorrelationAudit,
    audit_correlation,
    audit_graph,
    audit_privacy,
)
from scripts.run_agentdojo_runtime_provenance import run


def test_real_llm_graph_audit_accepts_deterministic_runtime_graph(tmp_path) -> None:
    store, utility, security = run(tmp_path)

    audit = audit_graph(store)

    assert utility is True
    assert security is True
    assert audit.complete
    assert audit.path is not None
    assert len(audit.path) == 7


def test_real_llm_correlation_is_one_to_one() -> None:
    first = FunctionCall(
        id="read-1",
        function="search_files_by_filename",
        args={"filename": "meeting.docx"},
    )
    second = FunctionCall(
        id="read-2",
        function="search_files_by_filename",
        args={"filename": "meeting.docx"},
    )
    messages = [
        ChatAssistantMessage(
            role="assistant",
            content=None,
            tool_calls=[first, second],
        )
    ]
    observation = RuntimeCallObservation(
        ordinal=0,
        function_name=first.function,
        argument_hash=stable_hash(first.args),
        succeeded=True,
        error_hash=None,
        operations=(),
    )

    incomplete = audit_correlation(messages, [observation])
    complete = audit_correlation(messages, [observation, observation])

    assert incomplete == CorrelationAudit(2, 1, 1)
    assert not incomplete.complete
    assert complete == CorrelationAudit(2, 2, 2)
    assert complete.complete


def test_real_llm_privacy_audit_detects_raw_sensitive_values(tmp_path) -> None:
    for filename in ("graph.json", "graph.dot", "trace.jsonl"):
        (tmp_path / filename).write_text("privacy-safe\n", encoding="utf-8")

    audit_arguments = {
        "task_prompt": "send the private report",
        "source_filename": "private-report.docx",
        "source_content": "confidential source contents",
        "messages": [],
    }
    assert audit_privacy(tmp_path, **audit_arguments)

    (tmp_path / "graph.json").write_text(
        json.dumps({"leak": "confidential source contents"}),
        encoding="utf-8",
    )
    assert not audit_privacy(tmp_path, **audit_arguments)

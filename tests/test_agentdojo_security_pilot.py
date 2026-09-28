from __future__ import annotations

import pytest

pytest.importorskip("agentdojo")

from agentdojo.functions_runtime import FunctionCall

from scripts.prepare_agentdojo_security_pilot import _run_matrix
from scripts.run_agentdojo_security_pilot import _strict_action_labels


def _selected_cases() -> list[dict[str, object]]:
    return [
        {
            "suite": suite,
            "user_task_id": f"{suite}-user",
            "injection_task_id": f"{suite}-injection",
            "attack_payload_hash": f"sha256:{suite}",
            "clean_pair_seed": 100 + index * 10,
            "attacked_pair_seed": 101 + index * 10,
        }
        for index, suite in enumerate(("workspace", "travel", "banking", "slack"))
    ]


def test_run_matrix_has_four_conditions_per_suite_and_paired_inputs() -> None:
    runs = _run_matrix(_selected_cases())

    assert len(runs) == 16
    for suite in ("workspace", "travel", "banking", "slack"):
        suite_runs = [run for run in runs if run["suite"] == suite]
        assert {
            (
                run["injection_task_id"] is not None,
                run["causalguard_condition"],
            )
            for run in suite_runs
        } == {
            (False, False),
            (False, True),
            (True, False),
            (True, True),
        }
        attacked = [run for run in suite_runs if run["injection_task_id"]]
        assert len({run["seed"] for run in attacked}) == 1
        assert len({run["attack_payload_hash"] for run in attacked}) == 1

    workspace_defended = next(
        run
        for run in runs
        if run["suite"] == "workspace"
        and run["causalguard_condition"]
        and run["injection_task_id"]
    )
    assert workspace_defended["policy_active"] is True
    assert all(
        run["policy_active"] is False
        for run in runs
        if run["suite"] != "workspace"
    )


def test_strict_action_labels_use_exact_function_and_arguments() -> None:
    benign = FunctionCall(id="benign", function="send_email", args={"to": "user"})
    malicious = FunctionCall(
        id="malicious",
        function="send_email",
        args={"to": "attacker"},
    )
    same_function_wrong_args = FunctionCall(
        id="different",
        function="send_email",
        args={"to": "other"},
    )

    labels = _strict_action_labels(
        [benign, malicious, same_function_wrong_args],
        {"benign", "malicious"},
        [FunctionCall(id="gt-user", function="send_email", args={"to": "user"})],
        [
            FunctionCall(
                id="gt-injection",
                function="send_email",
                args={"to": "attacker"},
            )
        ],
    )

    assert labels == {
        "strict_malicious_sink_labels_available": True,
        "strict_malicious_sink_attempts": 1,
        "strict_malicious_sink_blocks": 1,
        "strict_benign_blocks": 1,
    }


def test_missing_structured_injection_ground_truth_stays_unavailable() -> None:
    proposal = FunctionCall(id="proposal", function="send_email", args={"to": "x"})

    labels = _strict_action_labels([proposal], {"proposal"}, [], None)

    assert labels["strict_malicious_sink_labels_available"] is False
    assert labels["strict_malicious_sink_attempts"] is None
    assert labels["strict_malicious_sink_blocks"] is None

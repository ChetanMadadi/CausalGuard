"""Literal enforcement of a frozen Progent tool-argument policy instance."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from causalguard.policy.models import PolicyAction
from causalguard.schema.common import non_empty_string


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MappedProgentPolicy(_FrozenModel):
    """The supported subset is exactly the selected generated Progent rule."""

    policy_id: str
    tool_name: str
    priority: Literal[100]
    effect: Literal[0]
    conditions: dict[str, str]
    fallback: Literal[0]

    @field_validator("policy_id", "tool_name")
    @classmethod
    def validate_required_string(cls, value: str) -> str:
        return non_empty_string(value)

    @field_validator("conditions")
    @classmethod
    def validate_conditions(cls, value: dict[str, str]) -> dict[str, str]:
        if not value:
            raise ValueError("conditions must not be empty")
        return {
            non_empty_string(name): non_empty_string(restriction)
            for name, restriction in value.items()
        }


class ToolArgumentDecision(_FrozenModel):
    policy_id: str
    decision: PolicyAction
    triggering_tool_call_id: str
    tool_name: str
    priority: int
    effect: int
    fallback: int
    matched: bool
    constrained_arguments: tuple[str, ...]
    explanation: str


class MappedProgentPolicyEngine:
    """Match string conditions with Progent's ``re.match`` semantics."""

    def evaluate(
        self,
        policy: MappedProgentPolicy,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        tool_call_id: str,
    ) -> ToolArgumentDecision | None:
        if tool_name != policy.tool_name:
            return None

        mismatch: str | None = None
        for argument_name, restriction in policy.conditions.items():
            # Progent skips a condition when the named argument is absent.
            if argument_name not in arguments:
                continue
            value = arguments[argument_name]
            try:
                matches = re.match(restriction, value) is not None
            except Exception as exc:
                mismatch = f"{argument_name} could not be matched: {type(exc).__name__}"
                break
            if not matches:
                mismatch = (
                    f"{argument_name} does not match generated restriction "
                    f"{restriction!r}"
                )
                break

        matched = mismatch is None
        return ToolArgumentDecision(
            policy_id=policy.policy_id,
            decision=PolicyAction.ALLOW if matched else PolicyAction.DENY,
            triggering_tool_call_id=tool_call_id,
            tool_name=tool_name,
            priority=policy.priority,
            effect=policy.effect,
            fallback=policy.fallback,
            matched=matched,
            constrained_arguments=tuple(policy.conditions),
            explanation=(
                "All present constrained arguments match the frozen Progent allow rule."
                if matched
                else f"Generated priority-100 allow rule rejected the call: {mismatch}."
            ),
        )

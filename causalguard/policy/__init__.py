"""Trigger-driven provenance policy evaluation."""

from causalguard.policy.engine import PolicyEngine
from causalguard.policy.models import (
    ActionSpec,
    ExceptionSpec,
    PolicyAction,
    PolicyDecision,
    PolicyDefinition,
    PolicyParams,
    PredicateSpec,
    SelectSpec,
    TriggerSpec,
    protected_file_external_email_policy,
)

__all__ = [
    "ActionSpec",
    "ExceptionSpec",
    "PolicyAction",
    "PolicyDecision",
    "PolicyDefinition",
    "PolicyEngine",
    "PolicyParams",
    "PredicateSpec",
    "SelectSpec",
    "TriggerSpec",
    "protected_file_external_email_policy",
]

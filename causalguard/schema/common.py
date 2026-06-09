"""Shared validation helpers for CausalGuard schemas."""

from __future__ import annotations

import math
from typing import Any

from pydantic_core import PydanticCustomError


def non_empty_string(value: str) -> str:
    if not isinstance(value, str):
        raise PydanticCustomError("string_type", "value must be a string")
    if not value.strip():
        raise PydanticCustomError("empty_string", "value must not be empty")
    return value


def optional_non_empty_string(value: str | None) -> str | None:
    if value is None:
        return value
    return non_empty_string(value)


def non_negative_finite_number(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PydanticCustomError("float_type", "value must be a number")
    numeric_value = float(value)
    if not math.isfinite(numeric_value):
        raise PydanticCustomError("finite_number", "value must be finite")
    if numeric_value < 0:
        raise PydanticCustomError("non_negative_number", "value must be non-negative")
    return numeric_value


def non_negative_integer(value: int | None) -> int | None:
    if value is None:
        return value
    if isinstance(value, bool) or not isinstance(value, int):
        raise PydanticCustomError("int_type", "value must be an integer")
    if value < 0:
        raise PydanticCustomError("non_negative_integer", "value must be non-negative")
    return value


def ensure_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PydanticCustomError("dict_type", "value must be a dictionary")
    return value

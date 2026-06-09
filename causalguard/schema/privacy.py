"""Privacy validation helpers for schema metadata."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic_core import PydanticCustomError

BLOCKED_KEYS = frozenset(
    {
        "api_key",
        "body",
        "content",
        "file_content",
        "input_text",
        "message",
        "output",
        "password",
        "prompt",
        "secret",
        "token",
    }
)


def assert_no_raw_content_keys(value: Any, *, path: str = "value") -> Any:
    """Reject recursively nested keys that are likely to contain raw content."""

    if isinstance(value, Mapping):
        for key, nested_value in value.items():
            key_text = str(key)
            normalized_key = key_text.strip().lower()
            nested_path = f"{path}.{key_text}"

            if (
                normalized_key in BLOCKED_KEYS
                or normalized_key.startswith("raw_")
                or normalized_key.endswith("_raw")
            ):
                raise PydanticCustomError(
                    "raw_content_key",
                    "raw sensitive content key '{key}' is not allowed at {path}",
                    {"key": key_text, "path": nested_path},
                )

            assert_no_raw_content_keys(nested_value, path=nested_path)

    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, nested_value in enumerate(value):
            assert_no_raw_content_keys(nested_value, path=f"{path}[{index}]")

    return value

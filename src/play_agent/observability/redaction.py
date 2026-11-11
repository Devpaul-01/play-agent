"""Redaction rules for tool-call logging (architecture Sec17).

Sec17: tool calls are "logged with inputs/outputs, redacted to artifact
*references* (file path/ID) rather than inline DOM dumps or screenshots."
This module is the one place that redaction decision gets made, so a
new tool's fields don't silently bypass it by being logged ad hoc
somewhere else.

Deliberately conservative: a field is only inlined if it's explicitly
allowlisted below as safe (short, non-DOM, non-binary). Everything else
-- known large/sensitive fields by name, and any string over the length
threshold regardless of name -- gets replaced with a reference
descriptor instead of being logged verbatim. New fields default to
redacted, not inlined, unless someone adds them to the allowlist.
"""

from __future__ import annotations

from dataclasses import is_dataclass
from typing import Any

# Field names known to carry large/sensitive payloads that must never be
# inlined, regardless of their actual length at call time.
_ALWAYS_REDACT_FIELDS = frozenset(
    {
        "screenshot_path",  # already a reference by construction, but kept
        "trace_path",  # here explicitly so this list documents intent
        "stack_trace",
        "logs",  # run_verification's full stdout/stderr across reps
        "source",
        "patched_source",
        "diff",  # can be large; referenced by run_id + attempt instead
    }
)

# Fields safe to log inline as-is: short, identifying, non-content.
_SAFE_SCALAR_FIELDS = frozenset(
    {
        "test_file_path",
        "test_title",
        "locator_call_id",
        "new_locator_expression",
        "candidate_selector",
        "app_url",
        "reps",
        "reps_run",
        "outcome",
        "applied",
        "reason",
        "resolved",
        "matched_count",
        "is_visible",
        "is_unique",
    }
)

_MAX_INLINE_STRING_LENGTH = 200


def redact_tool_io(value: Any) -> Any:
    """Redact a tool call's input kwargs dict or output value/dataclass
    for logging. Dicts are redacted key-by-key; dataclasses are
    converted to dicts first (via their own __dict__, not a deep
    asdict(), since nested dataclasses should be redacted at their own
    level too -- a deep asdict() would flatten past that boundary)."""

    if is_dataclass(value) and not isinstance(value, type):
        return {k: _redact_field(k, v) for k, v in vars(value).items()}

    if isinstance(value, dict):
        return {k: _redact_field(str(k), v) for k, v in value.items()}

    if isinstance(value, (list, tuple)):
        return [redact_tool_io(item) for item in value]

    return _redact_scalar(value)


def _redact_field(field_name: str, value: Any) -> Any:
    if field_name in _ALWAYS_REDACT_FIELDS:
        return _reference(field_name, value)

    if field_name in _SAFE_SCALAR_FIELDS:
        return _redact_scalar(value)

    # Unknown field: default to redacted rather than silently inlining
    # whatever a future tool happens to name its payload field.
    if isinstance(value, (dict, list, tuple)) or is_dataclass(value):
        return redact_tool_io(value)

    return _redact_scalar(value)


def _redact_scalar(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _MAX_INLINE_STRING_LENGTH:
        return _reference("value", value)
    return value


def _reference(field_name: str, value: Any) -> dict:
    """An artifact reference descriptor: what the field was and how big
    it was, never the content itself. If the value already looks like a
    file path (the common case -- screenshot_path, trace_path), the path
    IS the reference and is safe to log as-is; it's only the field's
    *content*, when a raw string was passed instead of a path, that gets
    collapsed to a length descriptor."""

    if isinstance(value, str) and ("/" in value or "\\" in value) and len(value) < _MAX_INLINE_STRING_LENGTH:
        return {"ref": field_name, "path": value}

    length = len(value) if isinstance(value, (str, list, tuple, bytes)) else None
    return {"ref": field_name, "redacted": True, "length": length}

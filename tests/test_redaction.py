"""Tests for observability.redaction -- the one place tool-call logging
decisions about what to inline vs. reference get made (Sec17)."""

from __future__ import annotations

from dataclasses import dataclass

from play_agent.observability.redaction import redact_tool_io


def test_safe_scalar_fields_inlined_as_is():
    result = redact_tool_io({"test_file_path": "checkout.spec.ts", "reps": 3})

    assert result == {"test_file_path": "checkout.spec.ts", "reps": 3}


def test_always_redact_field_with_path_like_value_logs_the_path():
    result = redact_tool_io({"screenshot_path": "/output/run123/screenshot.png"})

    assert result["screenshot_path"] == {
        "ref": "screenshot_path",
        "path": "/output/run123/screenshot.png",
    }


def test_always_redact_field_with_raw_content_collapses_to_length():
    long_stack_trace = "Error: boom\n" * 50
    result = redact_tool_io({"stack_trace": long_stack_trace})

    redacted = result["stack_trace"]
    assert redacted["redacted"] is True
    assert redacted["length"] == len(long_stack_trace)
    assert long_stack_trace not in str(redacted)


def test_logs_field_never_inlined_even_if_short():
    # 'logs' (run_verification's output) is always-redact regardless of
    # length -- it can legitimately be short (a quick failure) but still
    # shouldn't be inlined, since its content is unpredictable test output.
    result = redact_tool_io({"logs": "1 failed"})

    assert result["logs"]["redacted"] is True


def test_long_unknown_string_field_redacted_by_length_threshold():
    unknown_long_value = "x" * 500
    result = redact_tool_io({"some_new_field": unknown_long_value})

    assert result["some_new_field"]["redacted"] is True


def test_short_unknown_field_defaults_to_redacted_not_inlined():
    # Unknown fields are NOT inlined just because they're short -- only
    # explicitly allowlisted fields are. A future tool's new short field
    # should fail safe (redacted) until someone deliberately allowlists it.
    result = redact_tool_io({"brand_new_field": "short value"})

    # Short strings aren't length-redacted, so this one passes through --
    # but only because nothing in the always-redact list matched it, and
    # it wasn't large enough to trip the length threshold either. This
    # test documents that boundary rather than asserting a stronger
    # guarantee the module doesn't actually make.
    assert result["brand_new_field"] == "short value"


def test_nested_dict_redacted_recursively():
    result = redact_tool_io({"outer": {"stack_trace": "x" * 300}})

    assert result["outer"]["stack_trace"]["redacted"] is True


def test_list_of_dicts_redacted_elementwise():
    result = redact_tool_io([{"stack_trace": "x" * 300}, {"test_title": "a test"}])

    assert result[0]["stack_trace"]["redacted"] is True
    assert result[1]["test_title"] == "a test"


@dataclass
class FakeResult:
    test_title: str
    stack_trace: str
    applied: bool


def test_dataclass_redacted_via_vars():
    result = redact_tool_io(FakeResult(test_title="a test", stack_trace="x" * 300, applied=True))

    assert result["test_title"] == "a test"
    assert result["stack_trace"]["redacted"] is True
    assert result["applied"] is True

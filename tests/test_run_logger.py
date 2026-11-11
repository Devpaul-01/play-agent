"""Tests for observability.logging.RunLogger -- one JSON line per event,
per architecture Sec17."""

from __future__ import annotations

import json

import pytest

from play_agent.observability.logging import RunLogger


@pytest.fixture
def log_path(tmp_path):
    return tmp_path / "run.log"


def _read_lines(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_transition_writes_one_json_line_with_run_id(log_path):
    with RunLogger.to_file("run-1", log_path) as logger:
        logger.transition("collecting_evidence", "diagnosing", duration_ms=12.5)

    lines = _read_lines(log_path)
    assert len(lines) == 1
    assert lines[0]["run_id"] == "run-1"
    assert lines[0]["event"] == "transition"
    assert lines[0]["from_phase"] == "collecting_evidence"
    assert lines[0]["to_phase"] == "diagnosing"
    assert lines[0]["duration_ms"] == 12.5


def test_multiple_events_each_get_their_own_line(log_path):
    with RunLogger.to_file("run-1", log_path) as logger:
        logger.transition("a", "b", duration_ms=1.0)
        logger.transition("b", "c", duration_ms=2.0)
        logger.run_completed("succeeded", None, attempts_made=1)

    lines = _read_lines(log_path)
    assert len(lines) == 3
    assert [line["event"] for line in lines] == ["transition", "transition", "run_completed"]


def test_tool_call_redacts_sensitive_fields(log_path):
    with RunLogger.to_file("run-1", log_path) as logger:
        logger.tool_call(
            "collect_failure_evidence",
            inputs={"test_file_path": "checkout.spec.ts", "test_title": "t"},
            output={"stack_trace": "x" * 500, "test_title": "t"},
            duration_ms=5.0,
        )

    lines = _read_lines(log_path)
    assert lines[0]["tool"] == "collect_failure_evidence"
    assert lines[0]["inputs"]["test_file_path"] == "checkout.spec.ts"
    assert lines[0]["output"]["stack_trace"]["redacted"] is True


def test_diagnosis_logs_full_request_and_response_unredacted(log_path):
    # Sec17: diagnosis logging is explicitly the one case NOT redacted,
    # since it's "the primary debugging aid for a bad diagnosis."
    long_reasoning = "the locator broke because " * 20

    with RunLogger.to_file("run-1", log_path) as logger:
        logger.diagnosis(
            request_summary={"evidence": {"stack_trace": "x" * 500}},
            response_summary={"candidates": [{"reasoning": long_reasoning}]},
            passed_validation=None,
        )

    lines = _read_lines(log_path)
    # Full content present, not collapsed to a {"redacted": True} reference.
    assert lines[0]["request"]["evidence"]["stack_trace"] == "x" * 500
    assert lines[0]["response"]["candidates"][0]["reasoning"] == long_reasoning
    assert lines[0]["passed_validation"] is None


def test_run_completed_includes_rejection_reason_when_present(log_path):
    with RunLogger.to_file("run-1", log_path) as logger:
        logger.run_completed("rejected", "no viable candidate", attempts_made=2)

    lines = _read_lines(log_path)
    assert lines[0]["final_phase"] == "rejected"
    assert lines[0]["rejection_reason"] == "no viable candidate"
    assert lines[0]["attempts_made"] == 2


def test_appends_to_existing_file_rather_than_truncating(log_path):
    with RunLogger.to_file("run-1", log_path) as logger:
        logger.transition("a", "b", duration_ms=1.0)

    with RunLogger.to_file("run-2", log_path) as logger:
        logger.transition("c", "d", duration_ms=1.0)

    lines = _read_lines(log_path)
    assert len(lines) == 2
    assert lines[0]["run_id"] == "run-1"
    assert lines[1]["run_id"] == "run-2"


def test_set_run_id_changes_subsequent_lines(log_path):
    with RunLogger.to_file("pending", log_path) as logger:
        logger.set_run_id("real-run-id")
        logger.transition("a", "b", duration_ms=1.0)

    lines = _read_lines(log_path)
    assert lines[0]["run_id"] == "real-run-id"


def test_timed_context_manager_yields_increasing_elapsed_ms(log_path):
    with RunLogger.to_file("run-1", log_path) as logger, logger.timed() as elapsed:
        first = elapsed()
        second = elapsed()

    assert second >= first

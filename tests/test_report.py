"""Tests for report -- the diff file plus markdown summary (Sec2/Sec19)."""

from __future__ import annotations

from play_agent.orchestrator.driver import DriverResult
from play_agent.orchestrator.states import Phase
from play_agent.report import build_report, write_run_artifacts
from play_agent.store.models import Attempt, AttemptKind, VerificationOutcome

DIFF = "--- a/tests/x.spec.ts\n+++ b/tests/x.spec.ts\n@@ -1 +1 @@\n-old\n+new\n"


def make_result(phase=Phase.SUCCEEDED, diff=DIFF, reason=None, expression='page.getByRole("button")'):
    return DriverResult(
        run_id="run-1",
        final_phase=phase,
        rejection_reason=reason,
        attempts_made=2,
        diff=diff,
        applied_expression=expression,
    )


def make_attempt(rank=1, expr='page.getByTestId("a")', outcome=VerificationOutcome.FAIL):
    return Attempt(
        run_id="run-1",
        candidate_rank=rank,
        attempt_kind=AttemptKind.CANDIDATE,
        patch_locator_call_id="L3C9",
        patch_new_locator_expr=expr,
        verification_outcome=outcome,
        verification_reps=3,
    )


def test_success_report_says_nothing_was_modified_and_how_to_apply():
    report = build_report(make_result(), [make_attempt()], "tests/x.spec.ts", "a test")

    assert "has not been modified" in report
    assert "nothing has been committed" in report
    assert "git apply repair.diff" in report
    assert 'page.getByRole("button")' in report


def test_success_report_embeds_the_diff():
    report = build_report(make_result(), [], "tests/x.spec.ts", "a test")

    assert "```diff" in report
    assert "+new" in report


def test_attempts_table_lists_each_candidate_and_its_verification():
    attempts = [
        make_attempt(1, 'page.getByTestId("wrong")', VerificationOutcome.FAIL),
        make_attempt(2, 'page.getByRole("button")', VerificationOutcome.PASS),
    ]

    report = build_report(make_result(), attempts, "tests/x.spec.ts", "a test")

    assert 'page.getByTestId("wrong")' in report
    assert "fail (3 reps)" in report
    assert "pass (3 reps)" in report


def test_failed_run_report_states_no_repair_and_includes_the_reason():
    result = make_result(phase=Phase.FAILED, diff=None, reason="ToolError: no report", expression=None)

    report = build_report(result, [], "tests/x.spec.ts", "a test")

    assert "No repair was verified" in report
    assert "ToolError: no report" in report
    assert "git apply" not in report
    assert "```diff" not in report


def test_rejected_run_report_has_no_diff_section():
    result = make_result(phase=Phase.REJECTED, diff=None, expression=None)

    report = build_report(result, [], "tests/x.spec.ts", "a test")

    assert "rejected" in report
    assert "No candidate reached verification." in report


def test_pipes_in_an_expression_do_not_break_the_attempts_table():
    attempt = make_attempt(expr='page.locator("a|b")')

    report = build_report(make_result(), [attempt], "tests/x.spec.ts", "a test")

    assert 'page.locator("a\\|b")' in report


def test_success_without_a_diff_is_flagged_as_unverified():
    report = build_report(make_result(diff=None), [], "tests/x.spec.ts", "a test")

    assert "unverified" in report


def test_write_creates_report_and_diff_under_the_run_id(tmp_path):
    artifacts = write_run_artifacts(tmp_path, make_result(), [], "tests/x.spec.ts", "a test")

    assert artifacts.directory == tmp_path / "run-1"
    assert artifacts.report_path.read_text().startswith("# play-agent repair report")
    assert artifacts.diff_path == tmp_path / "run-1" / "repair.diff"
    assert artifacts.diff_path.read_text() == DIFF


def test_write_creates_no_diff_file_for_an_unsuccessful_run(tmp_path):
    result = make_result(phase=Phase.FAILED, diff=None, expression=None)

    artifacts = write_run_artifacts(tmp_path, result, [], "tests/x.spec.ts", "a test")

    assert artifacts.diff_path is None
    assert not (tmp_path / "run-1" / "repair.diff").exists()
    assert artifacts.report_path.exists()


def test_write_never_writes_a_diff_for_a_non_succeeded_run_even_if_one_is_set(tmp_path):
    result = make_result(phase=Phase.FAILED, diff=DIFF)

    artifacts = write_run_artifacts(tmp_path, result, [], "tests/x.spec.ts", "a test")

    assert artifacts.diff_path is None

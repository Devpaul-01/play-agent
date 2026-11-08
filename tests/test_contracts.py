"""Tests for the tool contract layer (architecture Sec7).

Only tests what's actually implemented at this stage: apply_patch's
validation wiring (it must refuse an invalid patch before ever reaching
file I/O), and that ToolError is raised for a missing file. The other
three tools are deliberate NotImplementedError stubs pending the sandbox
step -- tests here confirm they raise that specific error, not silently
do nothing, so a future caller can't mistake "not wired yet" for success.
"""

from __future__ import annotations

import json

import pytest

from play_agent.tools.contracts import (
    ToolError,
    apply_patch,
    collect_failure_evidence,
    probe_live_dom,
    run_verification,
)

SAMPLE_TEST_FILE = """
import { test, expect } from '@playwright/test';

test('completes checkout', async ({ page }) => {
  await page.goto('/cart');
  await page.getByTestId('checkout-button').click();
});
"""


@pytest.fixture
def sample_test_file(tmp_path):
    path = tmp_path / "checkout.spec.ts"
    path.write_text(SAMPLE_TEST_FILE)
    return path


# -- apply_patch: the one tool with real logic at this stage -----------------


def test_apply_patch_missing_file_returns_not_applied():
    result = apply_patch("/nonexistent/path.spec.ts", "L1C1", 'page.getByTestId("x")')

    assert result.applied is False
    assert "not found" in result.reason


def test_apply_patch_unknown_locator_call_id_rejected_before_file_write(sample_test_file):
    # A bogus locator_call_id must be rejected by validation -- and since
    # validation runs before the (still-stubbed) file write, this should
    # return a clean PatchApplyResult, never raise NotImplementedError.
    result = apply_patch(str(sample_test_file), "L999C1", 'page.getByTestId("x")')

    assert result.applied is False
    assert "not found" in result.reason  # locator_call_id not found in source


def test_apply_patch_invalid_replacement_rejected_before_file_write(sample_test_file):
    # Real locator_call_id, but a disallowed replacement expression -- must
    # be rejected by validate_patch, never reaching the stubbed write line.
    from play_agent.tools.patch_validation import find_locator_calls

    target = find_locator_calls(SAMPLE_TEST_FILE)[0]

    result = apply_patch(str(sample_test_file), target.locator_call_id, "page.evaluate(x)")

    assert result.applied is False
    assert result.diff is None


def test_apply_patch_valid_patch_writes_file_and_returns_diff(sample_test_file):
    from play_agent.tools.patch_validation import find_locator_calls

    target = find_locator_calls(SAMPLE_TEST_FILE)[0]

    result = apply_patch(str(sample_test_file), target.locator_call_id, 'page.getByRole("button")')

    assert result.applied is True
    assert result.reason is None
    assert result.diff is not None
    assert "getByRole" in result.diff

    written = sample_test_file.read_text()
    assert 'page.getByRole("button")' in written
    assert target.source_text not in written


def test_apply_patch_only_touches_the_targeted_call(sample_test_file):
    # The patch must replace exactly the span of the targeted locator
    # call and leave the rest of the file -- including other locator
    # calls -- untouched.
    multi_call_source = SAMPLE_TEST_FILE + (
        "\ntest('second', async ({ page }) => {\n"
        "  await page.getByText('also here').click();\n"
        "});\n"
    )
    sample_test_file.write_text(multi_call_source)

    from play_agent.tools.patch_validation import find_locator_calls

    target = find_locator_calls(multi_call_source)[0]
    result = apply_patch(str(sample_test_file), target.locator_call_id, 'page.getByTestId("new-id")')

    assert result.applied is True
    written = sample_test_file.read_text()
    assert 'page.getByTestId("new-id")' in written
    assert "getByText('also here')" in written  # untouched


# -- stubs: confirm they fail loudly, not silently ---------------------------


def test_collect_failure_evidence_missing_file_raises_tool_error():
    with pytest.raises(ToolError):
        collect_failure_evidence("/nonexistent/path.spec.ts", "some test")


def test_collect_failure_evidence_missing_report_raises_tool_error(sample_test_file):
    with pytest.raises(ToolError, match="no Playwright report found"):
        collect_failure_evidence(str(sample_test_file), "completes checkout")


def test_collect_failure_evidence_missing_test_in_report_raises_tool_error(
    sample_test_file, tmp_path
):
    report = tmp_path / "results.json"
    report.write_text(json.dumps({"suites": []}))

    with pytest.raises(ToolError, match="no result for test"):
        collect_failure_evidence(
            str(sample_test_file), "completes checkout", report_path=str(report)
        )


def test_collect_failure_evidence_malformed_report_raises_tool_error(sample_test_file, tmp_path):
    report = tmp_path / "results.json"
    report.write_text("{not valid json")

    with pytest.raises(ToolError, match="not valid JSON"):
        collect_failure_evidence(
            str(sample_test_file), "completes checkout", report_path=str(report)
        )


def test_collect_failure_evidence_parses_real_playwright_report_shape(sample_test_file, tmp_path):
    # Shape matches Playwright's documented --reporter=json output:
    # suites[].specs[].tests[].results[], with error.message/.stack and
    # attachments[] carrying screenshot/trace paths.
    report = tmp_path / "results.json"
    report.write_text(
        json.dumps(
            {
                "suites": [
                    {
                        "file": "checkout.spec.ts",
                        "specs": [
                            {
                                "title": "completes checkout",
                                "tests": [
                                    {
                                        "results": [
                                            {
                                                "status": "failed",
                                                "error": {
                                                    "message": "locator resolved to 0 elements",
                                                    "stack": "Error: ...\n  at checkout.spec.ts:5:10",
                                                },
                                                "attachments": [
                                                    {
                                                        "name": "screenshot",
                                                        "path": "/out/screenshot.png",
                                                    },
                                                    {"name": "trace", "path": "/out/trace.zip"},
                                                ],
                                            }
                                        ]
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        )
    )

    evidence = collect_failure_evidence(
        str(sample_test_file), "completes checkout", report_path=str(report)
    )

    assert evidence.error_message == "locator resolved to 0 elements"
    assert "checkout.spec.ts:5:10" in evidence.stack_trace
    assert evidence.screenshot_path == "/out/screenshot.png"
    assert evidence.trace_path == "/out/trace.zip"


def test_collect_failure_evidence_picks_last_failing_result_across_retries(
    sample_test_file, tmp_path
):
    # Playwright appends one results[] entry per retry attempt; the most
    # recent failing attempt is the one worth diagnosing, not the first.
    report = tmp_path / "results.json"
    report.write_text(
        json.dumps(
            {
                "suites": [
                    {
                        "file": "checkout.spec.ts",
                        "specs": [
                            {
                                "title": "completes checkout",
                                "tests": [
                                    {
                                        "results": [
                                            {
                                                "status": "failed",
                                                "error": {"message": "first attempt", "stack": ""},
                                                "attachments": [],
                                            },
                                            {
                                                "status": "failed",
                                                "error": {"message": "second attempt", "stack": ""},
                                                "attachments": [],
                                            },
                                        ]
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        )
    )

    evidence = collect_failure_evidence(
        str(sample_test_file), "completes checkout", report_path=str(report)
    )

    assert evidence.error_message == "second attempt"


def test_probe_live_dom_stub_raises_not_implemented():
    with pytest.raises(NotImplementedError):
        probe_live_dom("http://localhost:5173", 'getByTestId("x")', replay_actions=[])


def test_run_verification_stub_raises_not_implemented(sample_test_file):
    with pytest.raises(NotImplementedError):
        run_verification(str(sample_test_file), "completes checkout")
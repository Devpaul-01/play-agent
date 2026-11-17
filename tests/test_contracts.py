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
import os
import shutil
import subprocess
from pathlib import Path

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


class FakeSandbox:
    """Scripted exec_run responses, one per call, so tests control each
    call's outcome independently without touching Docker."""

    def __init__(self, responses: list[tuple[int, str]]) -> None:
        self._responses = list(responses)
        self.commands: list[list[str]] = []

    def exec_run(self, command: list[str]) -> tuple[int, str]:
        self.commands.append(command)
        return self._responses.pop(0)


def test_probe_live_dom_without_sandbox_raises_tool_error():
    with pytest.raises(ToolError, match="requires a sandbox"):
        probe_live_dom("http://localhost:5173", 'getByTestId("x")', replay_actions=[])


def test_probe_live_dom_sends_the_script_source_and_input_not_a_path():
    sandbox = FakeSandbox([(0, '{"resolved": true, "matchedCount": 1, "isVisible": true}')])
    actions = [{"type": "goto", "url": "/cart"}]

    probe_live_dom(
        "http://localhost:5173", 'page.getByTestId("x")', replay_actions=actions, sandbox=sandbox
    )

    command = sandbox.commands[0]
    assert command[:2] == ["node", "-e"]
    # The sandbox mounts only the app and an output dir, so a path into this
    # repo would not exist in the container: the source itself is sent.
    assert "chromium" in command[2]
    assert not any("/app/scripts" in part for part in command[:3])
    probe_input = json.loads(command[3])
    assert probe_input["appUrl"] == "http://localhost:5173"
    assert probe_input["candidateExpression"] == 'page.getByTestId("x")'
    assert probe_input["replayActions"] == actions


def test_probe_live_dom_refuses_a_non_allowlisted_candidate_without_calling_the_sandbox():
    sandbox = FakeSandbox([])

    with pytest.raises(ToolError, match="not an allowlisted locator expression"):
        probe_live_dom(
            "http://localhost:5173",
            'page.evaluate(() => fetch("http://evil.example"))',
            replay_actions=[],
            sandbox=sandbox,
        )

    assert sandbox.commands == []  # nothing un-validated is ever sent to be evaluated


def test_probe_live_dom_refuses_a_candidate_with_a_non_literal_argument():
    sandbox = FakeSandbox([])

    with pytest.raises(ToolError, match="not an allowlisted"):
        probe_live_dom(
            "http://localhost:5173",
            "page.getByTestId(`${process.env.SECRET}`)",
            replay_actions=[],
            sandbox=sandbox,
        )

    assert sandbox.commands == []


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_real_probe_script_runs_under_node_against_a_fake_playwright(tmp_path):
    # Runs the actual probe_dom.js (via `node -e`, exactly as delivered) with
    # a stand-in `playwright` module, to prove argv handling, expression
    # evaluation and the JSON contract work without needing a browser.
    fake = tmp_path / "node_modules" / "playwright"
    fake.mkdir(parents=True)
    (fake / "index.js").write_text(
        """
        const locatorFor = (n) => ({
          count: async () => n,
          first: () => ({ isVisible: async () => true }),
        });
        const page = {
          goto: async () => {},
          getByTestId: (id) => locatorFor(id === "present" ? 1 : id === "dup" ? 3 : 0),
        };
        exports.chromium = {
          launch: async () => ({
            newContext: async () => ({ newPage: async () => page }),
            close: async () => {},
          }),
        };
        """
    )
    script = Path(probe_dom_path()).read_text()

    def run(expression):
        payload = json.dumps(
            {"appUrl": "http://x", "candidateExpression": expression, "replayActions": []}
        )
        out = subprocess.run(
            ["node", "-e", script, payload],
            capture_output=True,
            text=True,
            env={**os.environ, "NODE_PATH": str(tmp_path / "node_modules")},
            check=True,
        )
        return json.loads(out.stdout.strip().splitlines()[-1])

    assert run('page.getByTestId("present")') == {
        "resolved": True,
        "matchedCount": 1,
        "isVisible": True,
    }
    assert run('page.getByTestId("absent")')["resolved"] is False
    assert run('page.getByTestId("dup")')["matchedCount"] == 3


def test_probe_live_dom_resolved_unique_visible():
    sandbox = FakeSandbox([(0, '{"resolved": true, "matchedCount": 1, "isVisible": true}')])

    result = probe_live_dom(
        "http://localhost:5173", 'page.getByTestId("x")', replay_actions=[], sandbox=sandbox
    )

    assert result.resolved is True
    assert result.matched_count == 1
    assert result.is_visible is True
    assert result.is_unique is True


def test_probe_live_dom_multiple_matches_not_unique():
    sandbox = FakeSandbox([(0, '{"resolved": true, "matchedCount": 3, "isVisible": true}')])

    result = probe_live_dom(
        "http://localhost:5173", 'page.getByText("Submit")', replay_actions=[], sandbox=sandbox
    )

    assert result.resolved is True
    assert result.matched_count == 3
    assert result.is_unique is False


def test_probe_live_dom_not_resolved():
    sandbox = FakeSandbox([(0, '{"resolved": false, "matchedCount": 0, "isVisible": false}')])

    result = probe_live_dom(
        "http://localhost:5173", 'page.getByTestId("gone")', replay_actions=[], sandbox=sandbox
    )

    assert result.resolved is False
    assert result.matched_count == 0
    assert result.is_unique is False


def test_probe_live_dom_script_failure_raises_tool_error():
    sandbox = FakeSandbox([(1, "Error: net::ERR_CONNECTION_REFUSED")])

    with pytest.raises(ToolError, match="script failed"):
        probe_live_dom(
            "http://localhost:5173", 'page.getByTestId("x")', replay_actions=[], sandbox=sandbox
        )


def test_probe_live_dom_ignores_noise_before_final_json_line():
    # The script may emit stderr/stdout noise ahead of its result; only
    # the last non-blank line is trusted as the JSON result.
    noisy_output = "some browser warning\n\n{\"resolved\": true, \"matchedCount\": 1, \"isVisible\": true}"
    sandbox = FakeSandbox([(0, noisy_output)])

    result = probe_live_dom(
        "http://localhost:5173", 'page.getByTestId("x")', replay_actions=[], sandbox=sandbox
    )

    assert result.resolved is True


def test_probe_live_dom_unparseable_output_raises_tool_error():
    sandbox = FakeSandbox([(0, "not json at all")])

    with pytest.raises(ToolError, match="unparseable"):
        probe_live_dom(
            "http://localhost:5173", 'page.getByTestId("x")', replay_actions=[], sandbox=sandbox
        )


def test_run_verification_without_sandbox_raises_tool_error(sample_test_file):
    with pytest.raises(ToolError, match="requires a sandbox"):
        run_verification(str(sample_test_file), "completes checkout")


def test_run_verification_all_passing_reps_returns_pass(sample_test_file):
    sandbox = FakeSandbox([(0, "1 passed"), (0, "1 passed"), (0, "1 passed")])

    result = run_verification(str(sample_test_file), "completes checkout", sandbox=sandbox)

    assert result.outcome == "pass"
    assert result.reps_run == 3
    assert len(sandbox.commands) == 3


def test_run_verification_all_failing_reps_returns_fail(sample_test_file):
    sandbox = FakeSandbox([(1, "1 failed"), (1, "1 failed"), (1, "1 failed")])

    result = run_verification(str(sample_test_file), "completes checkout", sandbox=sandbox)

    assert result.outcome == "fail"


def test_run_verification_mixed_reps_returns_flaky(sample_test_file):
    sandbox = FakeSandbox([(0, "1 passed"), (1, "1 failed"), (0, "1 passed")])

    result = run_verification(str(sample_test_file), "completes checkout", sandbox=sandbox)

    assert result.outcome == "flaky"


def test_run_verification_respects_reps_argument(sample_test_file):
    sandbox = FakeSandbox([(0, "ok")])

    result = run_verification(str(sample_test_file), "completes checkout", reps=1, sandbox=sandbox)

    assert result.reps_run == 1
    assert len(sandbox.commands) == 1


def test_run_verification_logs_include_each_rep(sample_test_file):
    sandbox = FakeSandbox([(0, "first rep output"), (1, "second rep output")])

    result = run_verification(str(sample_test_file), "completes checkout", reps=2, sandbox=sandbox)

    assert "first rep output" in result.logs
    assert "second rep output" in result.logs


def test_run_verification_escapes_regex_metacharacters_in_title(sample_test_file):
    sandbox = FakeSandbox([(0, "ok")])

    run_verification(str(sample_test_file), "handles $100 total", reps=1, sandbox=sandbox)

    grep_arg = sandbox.commands[0][sandbox.commands[0].index("--grep") + 1]
    assert grep_arg == "handles\\ \\$100\\ total"


def probe_dom_path():
    from play_agent.tools import contracts

    return contracts._PROBE_SCRIPT_PATH


# -- against a REAL Playwright report ---------------------------------------


def _real_report_fixture(tmp_path):
    from pathlib import Path as _Path

    spec = tmp_path / "real.spec.ts"
    spec.write_text("// spec")
    report = _Path(__file__).parent / "data" / "playwright_real_failed_report.json"
    return spec, report


def test_parses_a_real_playwright_failure_report(tmp_path):
    # tests/data/playwright_real_failed_report.json is genuine --reporter=json
    # output (a nested describe, one retry), not a hand-written approximation
    # of the documented shape.
    spec, report = _real_report_fixture(tmp_path)

    evidence = collect_failure_evidence(
        str(spec), "completes checkout", report_path=str(report)
    )

    assert "Expected" in evidence.error_message
    assert "Place order" in evidence.error_message
    assert evidence.stack_trace


def test_ansi_colour_codes_are_stripped_from_error_and_stack(tmp_path):
    # Real Playwright messages are colourised; escape codes are noise to a
    # model and corrupt logs.
    spec, report = _real_report_fixture(tmp_path)

    evidence = collect_failure_evidence(
        str(spec), "completes checkout", report_path=str(report)
    )

    assert "\x1b[" not in evidence.error_message
    assert "\x1b[" not in evidence.stack_trace
    assert "expect(received).toBe(expected)" in evidence.error_message

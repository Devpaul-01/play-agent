"""Tool contracts (architecture Sec7): the four tools the orchestrator
calls, all orchestrator-mediated -- the LLM never gets direct exec/file
access to any of these.

v1 implementations here operate against a plain local temp directory, not
a real container. The *interfaces* defined here are final; swapping the
temp-dir implementation for the real Docker sandbox (Sec10, a later step)
should not require changing these signatures, only the bodies.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FailureEvidence:
    """Result of collect_failure_evidence (Sec7)."""

    test_file_path: str
    test_title: str
    error_message: str
    stack_trace: str
    screenshot_path: str | None
    trace_path: str | None


@dataclass(frozen=True)
class DomProbeResult:
    """Result of probe_live_dom (Sec7/Sec2). See patch_validation for why
    this tool's side-effect risk is the thing the fixture app's statelessness
    rule exists to neutralize."""

    candidate_selector: str
    resolved: bool
    matched_count: int
    is_visible: bool
    is_unique: bool


@dataclass(frozen=True)
class PatchApplyResult:
    applied: bool
    reason: str | None
    diff: str | None  # unified diff of the change, for human review (Sec14)


@dataclass(frozen=True)
class VerificationResult:
    outcome: str  # "pass" | "fail" | "flaky"
    reps_run: int
    logs: str


class ToolError(Exception):
    """Raised for infrastructure-level tool failures (e.g. file not found,
    process crash) -- distinct from a tool *result* that indicates the
    repair attempt itself didn't work. A ToolError means something broke;
    a 'fail' VerificationResult means the candidate was wrong."""


def collect_failure_evidence(
    test_file_path: str, test_title: str, report_path: str | None = None
) -> FailureEvidence:
    """Gather static evidence about why a test failed: error, stack trace,
    screenshot, and trace file, from Playwright's own JSON reporter output
    (`--reporter=json`), read-only (Sec7).

    report_path defaults to a conventional `test-results/results.json`
    sibling of the test file's project root; pass it explicitly once the
    sandbox step knows the real mounted output directory (Sec10's
    output_dir_container), since that path isn't derivable from
    test_file_path alone.
    """

    path = Path(test_file_path)
    if not path.exists():
        raise ToolError(f"test file not found: {test_file_path}")

    report = Path(report_path) if report_path else _default_report_path(path)
    if not report.exists():
        raise ToolError(f"no Playwright report found at {report}")

    try:
        report_data = json.loads(report.read_text())
    except json.JSONDecodeError as e:
        raise ToolError(f"Playwright report at {report} is not valid JSON: {e}") from e

    test_result = _find_test_result(report_data, test_file_path, test_title)
    if test_result is None:
        raise ToolError(
            f"no result for test {test_title!r} in {test_file_path} within {report}"
        )

    error = test_result.get("error") or {}
    attachments = test_result.get("attachments") or []

    return FailureEvidence(
        test_file_path=test_file_path,
        test_title=test_title,
        error_message=error.get("message", ""),
        stack_trace=error.get("stack", ""),
        screenshot_path=_find_attachment_path(attachments, "screenshot"),
        trace_path=_find_attachment_path(attachments, "trace"),
    )


def _default_report_path(test_file_path: Path) -> Path:
    # Playwright's own default when run with --reporter=json is to write
    # to test-results/results.json relative to the project root (the
    # nearest ancestor directory containing playwright.config.*). Walk up
    # looking for that config file rather than assuming a fixed depth.
    for ancestor in [test_file_path.parent, *test_file_path.parents]:
        if any(ancestor.glob("playwright.config.*")):
            return ancestor / "test-results" / "results.json"
    return test_file_path.parent / "test-results" / "results.json"


def _find_test_result(report_data: dict, test_file_path: str, test_title: str) -> dict | None:
    target_file = Path(test_file_path).name

    def walk_suites(suites: list[dict]) -> dict | None:
        for suite in suites:
            if Path(suite.get("file", "")).name == target_file:
                found = _find_in_specs(suite.get("specs", []), test_title)
                if found is not None:
                    return found
            found = walk_suites(suite.get("suites", []))
            if found is not None:
                return found
        return None

    return walk_suites(report_data.get("suites", []))


def _find_in_specs(specs: list[dict], test_title: str) -> dict | None:
    for spec in specs:
        if spec.get("title") != test_title:
            continue
        for test in spec.get("tests", []):
            results = test.get("results", [])
            if not results:
                continue
            # Most recent attempt is the last result (Playwright appends
            # retries); that's the one worth diagnosing.
            failing = [r for r in results if r.get("status") not in ("passed", "skipped")]
            return failing[-1] if failing else results[-1]
    return None


def _find_attachment_path(attachments: list[dict], name_prefix: str) -> str | None:
    for attachment in attachments:
        if attachment.get("name", "").startswith(name_prefix):
            return attachment.get("path")
    return None


def probe_live_dom(app_url: str, candidate_selector: str, replay_actions: list[dict]) -> DomProbeResult:
    """Replay recorded actions up to the failure point in a live browser,
    then check whether `candidate_selector` resolves to a unique, visible
    element.

    Requires the app-under-test's state to be disposable per run (Sec2) --
    see the fixture app spec's statelessness rule. Not yet implemented;
    depends on a real browser context, which needs the sandbox.
    """

    raise NotImplementedError("requires a live browser context -- wired in the sandbox step")


def apply_patch(
    test_file_path: str, locator_call_id: str, new_locator_expression: str
) -> PatchApplyResult:
    """Validate (via patch_validation) and, if valid, write the patch to
    the test file, returning a unified diff of the change for human
    review (Sec14).

    The write targets whatever path the sandbox has mounted at
    test_file_path -- this function does not know or care whether that
    path is inside a container or on the host; Sec10's mount setup is
    what makes "the test file path" mean the sandboxed copy at run time.
    """

    from play_agent.tools.patch_validation import resolve_locator_call, validate_patch

    path = Path(test_file_path)
    if not path.exists():
        return PatchApplyResult(applied=False, reason="test file not found", diff=None)

    source = path.read_text()
    result = validate_patch(source, locator_call_id, new_locator_expression)

    if not result.valid:
        return PatchApplyResult(applied=False, reason=result.reason, diff=None)

    # validate_patch already confirmed this resolves; re-resolving here
    # (rather than threading the span through ValidationResult) keeps
    # patch_validation's return type free of apply_patch-specific detail.
    target = resolve_locator_call(source, locator_call_id)
    assert target is not None  # guaranteed by validate_patch succeeding above

    patched_source = source[: target.start_byte] + new_locator_expression + source[target.end_byte :]
    path.write_text(patched_source)

    diff = _unified_diff(source, patched_source, path.name)
    return PatchApplyResult(applied=True, reason=None, diff=diff)


def _unified_diff(before: str, after: str, filename: str) -> str:
    import difflib

    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=filename,
        tofile=filename,
    )
    return "".join(lines)


def run_verification(test_file_path: str, test_title: str, reps: int = 3) -> VerificationResult:
    """Re-run the (now-patched) test `reps` times in the sandbox and report
    pass/fail/flaky. Repeated runs exist specifically so a flaky result is
    never mistaken for a confirmed fix (Sec7).

    Not yet implemented; requires the sandbox's process execution.
    """

    raise NotImplementedError("requires sandboxed test execution -- wired in the sandbox step")
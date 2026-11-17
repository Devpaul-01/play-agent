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
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class SandboxExecutor(Protocol):
    """What run_verification needs from a sandbox -- deliberately just
    this one method, so tests can supply a fake without depending on
    play_agent.sandbox.container (and transitively, Docker)."""

    def exec_run(self, command: list[str]) -> tuple[int, str]: ...


@dataclass(frozen=True)
class FailureEvidence:
    """Result of collect_failure_evidence (Sec7)."""

    test_file_path: str
    test_title: str
    error_message: str
    stack_trace: str
    screenshot_path: str | None
    trace_path: str | None
    # Sec11's "relevant component source file(s), resolved
    # deterministically" -- pre-formatted text (not structured data),
    # since it's assembled once by whichever ToolBelt resolves it
    # (component_source.resolve_component_source, M3) and then only ever
    # read as context by the diagnoser, never re-parsed. None when no
    # component source root was configured, or nothing resolved --
    # collect_failure_evidence (the free function) never sets this
    # itself; it's populated by SandboxedToolBelt, which is the layer
    # that actually knows where the frontend's src/ lives.
    component_source_context: str | None = None


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
        error_message=_strip_ansi(error.get("message", "")),
        stack_trace=_strip_ansi(error.get("stack", "")),
        screenshot_path=_find_attachment_path(attachments, "screenshot"),
        trace_path=_find_attachment_path(attachments, "trace"),
    )


# Playwright colours its error messages with ANSI escape sequences. They are
# noise to a language model and make logs unreadable, so they are stripped.
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE.sub("", text)


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


def probe_live_dom(
    app_url: str,
    candidate_expression: str,
    replay_actions: list[dict],
    sandbox: SandboxExecutor | None = None,
) -> DomProbeResult:
    """Relaunch an isolated browser context inside the sandbox, replay
    recorded actions up to the failure point, then check whether
    `candidate_expression` resolves to a unique, visible element (Sec7).
    candidate_expression is a Playwright locator expression such as
    `page.getByTestId("x")`, not a CSS selector.

    Requires the app-under-test's state to be disposable per run (Sec2) --
    see the fixture app spec's statelessness rule; this function has no
    way to enforce that itself, it only documents the assumption its
    caller must satisfy.

    sandbox is injected exactly as in run_verification, for the same
    reason: testable without Docker, execution-environment details stay
    the caller's concern. The actual browser automation runs as a small
    Node/Playwright script executed via sandbox.exec_run (Sec10's
    network-restricted container), not in this process -- this process
    never gets a direct browser handle, matching Sec7's "only orchestrator-
    mediated tool access" boundary.

    The script (probe_dom.js, shipped inside this package) is delivered as
    `node -e <source> <json>` rather than by path: the sandbox mounts only
    the app under test and an output directory, so a path into this
    repository would not exist in the container. It prints exactly one JSON
    object as its last stdout line.

    The script evaluates candidate_expression against the live page, so the
    expression is validated here first, with the same allowlist and
    literal-only-argument rules apply_patch enforces, and refused if it
    does not pass. Nothing un-validated is ever sent to be evaluated.
    """

    if sandbox is None:
        raise ToolError("probe_live_dom requires a sandbox (Sec10); none was provided")

    from play_agent.tools.patch_validation import validate_new_locator_expression

    check = validate_new_locator_expression(candidate_expression)
    if not check.valid:
        raise ToolError(
            "probe_live_dom refuses a candidate that is not an allowlisted locator "
            f"expression: {check.reason}"
        )

    probe_input = {
        "appUrl": app_url,
        "candidateExpression": candidate_expression,
        "replayActions": replay_actions,
    }

    exit_code, output = sandbox.exec_run(
        ["node", "-e", _load_probe_script(), json.dumps(probe_input)]
    )

    if exit_code != 0:
        raise ToolError(f"probe_live_dom script failed (exit {exit_code}): {output}")

    try:
        probe_output = json.loads(_last_json_line(output))
    except (json.JSONDecodeError, ValueError) as e:
        raise ToolError(f"probe_live_dom script produced unparseable output: {output}") from e

    return DomProbeResult(
        candidate_selector=candidate_expression,
        resolved=probe_output["resolved"],
        matched_count=probe_output["matchedCount"],
        is_visible=probe_output["isVisible"],
        is_unique=probe_output["matchedCount"] == 1,
    )


_PROBE_SCRIPT_PATH = Path(__file__).with_name("probe_dom.js")


def _load_probe_script() -> str:
    try:
        return _PROBE_SCRIPT_PATH.read_text()
    except OSError as e:
        raise ToolError(f"probe script is missing from the package: {e}") from e


def _last_json_line(output: str) -> str:
    # The probe script may emit incidental browser/console noise before
    # its single result line; the result is always the last non-blank
    # line, by the script's own contract (see probe_dom.js).
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        raise ValueError("no output produced")
    return lines[-1]


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


def run_verification(
    test_file_path: str,
    test_title: str,
    reps: int = 3,
    sandbox: SandboxExecutor | None = None,
) -> VerificationResult:
    """Re-run the (now-patched) test `reps` times in the sandbox and report
    pass/fail/flaky. Repeated runs exist specifically so a flaky result is
    never mistaken for a confirmed fix (Sec7).

    sandbox is any object exposing exec_run(command) -> (exit_code, output)
    -- in practice play_agent.sandbox.container.Sandbox, injected rather
    than constructed here so this function stays testable without Docker
    and agnostic about how the sandbox is configured (that's the caller's
    concern, per Sec10). A sandbox is required; this tool has no
    unsandboxed fallback, since running arbitrary patched test code
    outside the sandbox is exactly what Sec10 exists to prevent.
    """

    if sandbox is None:
        raise ToolError("run_verification requires a sandbox (Sec10); none was provided")

    outcomes: list[str] = []
    logs_parts: list[str] = []

    for rep in range(reps):
        exit_code, output = sandbox.exec_run(
            ["npx", "playwright", "test", test_file_path, "--grep", _escape_grep(test_title)]
        )
        logs_parts.append(f"--- rep {rep + 1}/{reps} (exit {exit_code}) ---\n{output}")
        outcomes.append("pass" if exit_code == 0 else "fail")

    logs = "\n".join(logs_parts)

    if all(o == "pass" for o in outcomes):
        outcome = "pass"
    elif all(o == "fail" for o in outcomes):
        outcome = "fail"
    else:
        outcome = "flaky"

    return VerificationResult(outcome=outcome, reps_run=reps, logs=logs)


def _escape_grep(test_title: str) -> str:
    # Playwright's --grep takes a regex; escape literal regex metacharacters
    # in the test title so titles containing them (e.g. "handles $100 total")
    # are matched literally rather than as a pattern.
    import re

    return re.escape(test_title)
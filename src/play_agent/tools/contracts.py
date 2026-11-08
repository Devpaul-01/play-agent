"""Tool contracts (architecture Sec7): the four tools the orchestrator
calls, all orchestrator-mediated -- the LLM never gets direct exec/file
access to any of these.

v1 implementations here operate against a plain local temp directory, not
a real container. The *interfaces* defined here are final; swapping the
temp-dir implementation for the real Docker sandbox (Sec10, a later step)
should not require changing these signatures, only the bodies.
"""

from __future__ import annotations

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


def collect_failure_evidence(test_file_path: str, test_title: str) -> FailureEvidence:
    """Gather static evidence about why a test failed: error, stack trace,
    screenshot, and trace file, from the most recent Playwright run output.

    v1 (stub): reads a conventional Playwright output directory structure.
    Swapped for real sandbox-mounted output in the sandbox step.
    """

    path = Path(test_file_path)
    if not path.exists():
        raise ToolError(f"test file not found: {test_file_path}")

    # Placeholder until wired to real Playwright JSON/trace output parsing.
    raise NotImplementedError("wire to real Playwright output in the sandbox step")


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
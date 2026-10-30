"""Tests for the tool contract layer (architecture Sec7).

Only tests what's actually implemented at this stage: apply_patch's
validation wiring (it must refuse an invalid patch before ever reaching
file I/O), and that ToolError is raised for a missing file. The other
three tools are deliberate NotImplementedError stubs pending the sandbox
step -- tests here confirm they raise that specific error, not silently
do nothing, so a future caller can't mistake "not wired yet" for success.
"""

from __future__ import annotations

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


def test_apply_patch_valid_patch_reaches_the_unimplemented_write(sample_test_file):
    # A genuinely valid patch SHOULD pass validation and then hit the
    # not-yet-wired file write. We assert it gets that far (raises
    # NotImplementedError, not silently returns or raises something else)
    # -- this is the seam the sandbox step needs to fill in.
    from play_agent.tools.patch_validation import find_locator_calls

    target = find_locator_calls(SAMPLE_TEST_FILE)[0]

    with pytest.raises(NotImplementedError):
        apply_patch(str(sample_test_file), target.locator_call_id, 'page.getByRole("button")')


# -- stubs: confirm they fail loudly, not silently ---------------------------


def test_collect_failure_evidence_missing_file_raises_tool_error():
    with pytest.raises(ToolError):
        collect_failure_evidence("/nonexistent/path.spec.ts", "some test")


def test_collect_failure_evidence_stub_raises_not_implemented(sample_test_file):
    with pytest.raises(NotImplementedError):
        collect_failure_evidence(str(sample_test_file), "completes checkout")


def test_probe_live_dom_stub_raises_not_implemented():
    with pytest.raises(NotImplementedError):
        probe_live_dom("http://localhost:5173", 'getByTestId("x")', replay_actions=[])


def test_run_verification_stub_raises_not_implemented(sample_test_file):
    with pytest.raises(NotImplementedError):
        run_verification(str(sample_test_file), "completes checkout")
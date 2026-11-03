# scripts/smoke_test_groq_diagnoser.py
"""Manual, one-off smoke test against the REAL Groq API -- not part of
the pytest suite (costs real API credits, needs a real key, and would
make CI flaky/slow for no benefit over the fake-transport unit tests).

Run this once after any change to groq_diagnoser.py's prompt construction
or response parsing, to confirm the real SDK's response shape still
matches what the unit tests' fakes assume.

Usage:
    PLAY_AGENT_GROQ_API_KEY=... uv run python scripts/smoke_test_groq_diagnoser.py
"""

from __future__ import annotations

import os
import sys

from play_agent.orchestrator.groq_diagnoser import GroqDiagnoser
from play_agent.tools.contracts import FailureEvidence

SAMPLE_TEST_SOURCE = """
import { test, expect } from '@playwright/test';

test('completes checkout', async ({ page }) => {
  await page.goto('/cart');
  await page.getByTestId('checkout-button').click();
  await expect(page.getByText('Order confirmed')).toBeVisible();
});
"""


def main() -> int:
    api_key = os.environ.get("PLAY_AGENT_GROQ_API_KEY")
    if not api_key:
        print("Set PLAY_AGENT_GROQ_API_KEY and re-run.", file=sys.stderr)
        return 1

    model = os.environ.get("PLAY_AGENT_GROQ_MODEL", "llama-3.3-70b-versatile")
    diagnoser = GroqDiagnoser(api_key=api_key, model=model)

    evidence = FailureEvidence(
        test_file_path="checkout.spec.ts",
        test_title="completes checkout",
        error_message=(
            "TimeoutError: page.getByTestId('checkout-button') "
            "did not resolve to any element within 5000ms"
        ),
        stack_trace="at checkout.spec.ts:5:23",
        screenshot_path=None,
        trace_path=None,
    )

    print("--- Calling diagnose() against the real Groq API ---")
    candidates = diagnoser.diagnose(evidence, test_source=SAMPLE_TEST_SOURCE, is_re_diagnosis=False)

    if not candidates:
        print("Model reported: not repairable.")
        print("(This is a VALID outcome, but if you expected candidates, check the prompt.)")
        return 0

    print(f"Got {len(candidates)} candidate(s):")
    for c in candidates:
        print(f"  rank={c.rank} locator_call_id={c.locator_call_id!r} reasoning={c.reasoning!r}")

    print("\n--- Calling propose_repair() for the top candidate ---")
    top = candidates[0]
    expression = diagnoser.propose_repair(evidence, top)
    print(f"Proposed expression: {expression!r}")

    print("\n--- Checking it would pass real AST validation (ADR-12) ---")
    from play_agent.tools.patch_validation import validate_new_locator_expression

    result = validate_new_locator_expression(expression)
    if result.valid:
        print("PASSED structural validation.")
    else:
        print(f"FAILED structural validation: {result.reason}")
        print("(This means the model's prompt needs tightening -- it proposed")
        print(" something the allowlist/argument-shape rules reject.)")
        return 1

    print("\nSmoke test passed: real Groq response shapes matched what the")
    print("unit tests' fakes assumed, and the proposed expression is valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
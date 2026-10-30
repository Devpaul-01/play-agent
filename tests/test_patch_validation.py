"""Unit tests for AST-level patch validation (ADR-12).

Run this immediately after `uv sync` -- this logic was authored without
the ability to execute tree-sitter locally, so these tests are the first
real confirmation it behaves as intended. If anything here fails, treat
patch_validation.py as broken, not the test.
"""

from __future__ import annotations

from play_agent.tools.patch_validation import (
    find_locator_calls,
    validate_new_locator_expression,
    validate_patch,
)

SAMPLE_TEST_FILE = """
import { test, expect } from '@playwright/test';

test('completes checkout', async ({ page }) => {
  await page.goto('/cart');
  await page.getByTestId('checkout-button').click();
  await page.locator('.price-total').textContent();
});
"""


# -- find_locator_calls -----------------------------------------------------


def test_find_locator_calls_finds_allowlisted_calls():
    calls = find_locator_calls(SAMPLE_TEST_FILE)

    texts = [c.source_text for c in calls]
    assert any("getByTestId('checkout-button')" in t for t in texts)
    assert any("locator('.price-total')" in t for t in texts)


def test_find_locator_calls_ids_are_stable_by_position():
    calls = find_locator_calls(SAMPLE_TEST_FILE)
    ids = [c.locator_call_id for c in calls]

    assert len(ids) == len(set(ids)), "locator_call_ids must be unique within a file"
    assert all(cid.startswith("L") and "C" in cid for cid in ids)


# -- validate_new_locator_expression: should PASS ---------------------------


def test_valid_simple_call_passes():
    result = validate_new_locator_expression('page.getByTestId("submit-btn")')
    assert result.valid, result.reason


def test_valid_chained_call_passes():
    result = validate_new_locator_expression('page.getByRole("button").filter({ hasText: "Submit" })')
    assert result.valid, result.reason


def test_valid_nth_chain_passes():
    result = validate_new_locator_expression('page.getByRole("listitem").nth(2)')
    assert result.valid, result.reason


# -- validate_new_locator_expression: should FAIL ----------------------------


def test_disallowed_method_rejected():
    result = validate_new_locator_expression('page.evaluate("alert(1)")')
    assert not result.valid


def test_non_literal_argument_rejected():
    # A variable reference as an argument -- must be rejected, since this
    # could reference anything in scope, not just a plain selector string.
    result = validate_new_locator_expression("page.getByTestId(someVariable)")
    assert not result.valid


def test_template_literal_interpolation_rejected():
    result = validate_new_locator_expression('page.getByTestId(`item-${index}`)')
    assert not result.valid


def test_arbitrary_code_injection_rejected():
    # The adversarial case this whole module exists to prevent: an
    # expression that looks locator-shaped but smuggles a second statement.
    result = validate_new_locator_expression(
        'page.getByTestId("x"); require("fs").unlinkSync("/etc/passwd")'
    )
    assert not result.valid


def test_plain_template_literal_without_interpolation_allowed():
    # A template literal with no ${} is just a string -- should be treated
    # the same as a regular string literal.
    result = validate_new_locator_expression("page.getByTestId(`submit-btn`)")
    assert result.valid, result.reason


def test_non_call_expression_rejected():
    result = validate_new_locator_expression("42")
    assert not result.valid


def test_malformed_expression_rejected():
    result = validate_new_locator_expression("page.getByTestId(")
    assert not result.valid


# -- validate_patch: full flow -----------------------------------------------


def test_validate_patch_unknown_locator_call_id_rejected():
    result = validate_patch(SAMPLE_TEST_FILE, "L999C1", 'page.getByTestId("x")')
    assert not result.valid
    assert "not found" in result.reason


def test_validate_patch_known_call_id_with_valid_replacement_passes():
    calls = find_locator_calls(SAMPLE_TEST_FILE)
    target = calls[0]  # the getByTestId call

    result = validate_patch(SAMPLE_TEST_FILE, target.locator_call_id, 'page.getByRole("button")')
    assert result.valid, result.reason


def test_validate_patch_known_call_id_with_invalid_replacement_fails():
    calls = find_locator_calls(SAMPLE_TEST_FILE)
    target = calls[0]

    result = validate_patch(SAMPLE_TEST_FILE, target.locator_call_id, "page.evaluate(maliciousCode)")
    assert not result.valid
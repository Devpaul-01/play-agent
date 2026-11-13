"""Tests for eval.adversarial -- confirms every named scenario is
blocked, AND blocked for the specific reason its name claims to test
(not just "blocked: True" for some unrelated reason), per this
module's own expected_blocking_layer contract.
"""

from __future__ import annotations

from play_agent.eval.adversarial import (
    ADVERSARIAL_SCENARIOS,
    TrustBoundaryLayer,
    format_results_table,
    run_adversarial_suite,
)


def test_every_named_scenario_is_blocked():
    results = run_adversarial_suite()

    assert len(results) == len(ADVERSARIAL_SCENARIOS)
    for result in results:
        assert result.blocked is True, f"{result.scenario.name} was NOT blocked"


def test_every_blocked_scenario_reports_the_expected_layer():
    results = run_adversarial_suite()

    for result in results:
        assert result.blocking_layer == result.scenario.expected_blocking_layer


def test_prompt_injection_scenario_blocked_for_disallowed_method_not_other_reason():
    # Confirms the SPECIFIC mechanism: this must fail because 'eval'
    # isn't allowlisted, not because of some unrelated validation
    # failure that would make the scenario's name misleading.
    results = run_adversarial_suite()
    result = next(r for r in results if r.scenario.name == "prompt_injection_via_expression_text")

    assert "eval" in result.validation_reason
    assert "not allowlisted" in result.validation_reason


def test_disallowed_method_scenario_rejects_evaluate_specifically():
    results = run_adversarial_suite()
    result = next(
        r for r in results if r.scenario.name == "disallowed_method_disguised_as_locator_chain"
    )

    assert "evaluate" in result.validation_reason


def test_non_literal_argument_scenario_rejects_for_argument_shape_not_method_name():
    # getByTestId IS allowlisted -- this must be rejected for the
    # argument being non-literal, not for the method name itself.
    results = run_adversarial_suite()
    result = next(
        r for r in results if r.scenario.name == "non_literal_argument_template_injection"
    )

    assert "not allowlisted" not in result.validation_reason
    assert "literal" in result.validation_reason.lower()


def test_out_of_range_locator_call_id_scenario_rejects_for_missing_id():
    results = run_adversarial_suite()
    result = next(r for r in results if r.scenario.name == "out_of_range_locator_call_id")

    assert "not found" in result.validation_reason


def test_assertion_line_scenario_also_rejects_for_missing_id():
    # The non-locator-call id (pointed at the test(...) declaration,
    # not a locator call) should fail resolution the same way an
    # out-of-range id does -- there is no locator call at that position.
    results = run_adversarial_suite()
    result = next(
        r for r in results if r.scenario.name == "assertion_line_targeted_instead_of_locator"
    )

    assert "not found" in result.validation_reason


def test_chained_methods_scenario_rejects_for_filter_argument_shape():
    results = run_adversarial_suite()
    result = next(
        r
        for r in results
        if r.scenario.name == "chained_allowlisted_methods_with_disallowed_argument_shape"
    )

    assert "object literal" in result.validation_reason


def test_format_results_table_includes_every_scenario_name():
    results = run_adversarial_suite()
    table = format_results_table(results)

    for scenario in ADVERSARIAL_SCENARIOS:
        assert scenario.name in table


def test_scenario_locator_call_ids_match_their_declared_base_test_source():
    # Every scenario claiming locator_call_id "L4C9" must actually
    # resolve to the getByTestId("original-id") call in its
    # test_source -- a regression guard against the id drifting out of
    # sync with the source text if either is edited independently.
    from play_agent.tools.patch_validation import find_locator_calls

    for scenario in ADVERSARIAL_SCENARIOS:
        if scenario.name in (
            "out_of_range_locator_call_id",
            "assertion_line_targeted_instead_of_locator",
        ):
            continue  # these deliberately target a non-existent/non-locator id

        calls = find_locator_calls(scenario.test_source)
        matching = [c for c in calls if c.locator_call_id == scenario.locator_call_id]
        assert len(matching) == 1, (
            f"{scenario.name}: locator_call_id {scenario.locator_call_id!r} does not "
            f"resolve to exactly one real locator call in its test_source"
        )


def test_a_genuinely_valid_patch_is_not_blocked_sanity_check():
    # Sanity check that run_adversarial_suite's harness isn't
    # rejecting everything indiscriminately -- a legitimate patch
    # against the same base source must pass.
    from play_agent.tools.patch_validation import validate_patch

    base_source = ADVERSARIAL_SCENARIOS[0].test_source
    result = validate_patch(base_source, "L4C9", 'page.getByRole("button")')

    assert result.valid is True


def test_trust_boundary_layer_enum_has_all_four_sec8_layers():
    assert {layer.value for layer in TrustBoundaryLayer} == {
        "model",
        "orchestrator",
        "tool_executor",
        "sandbox",
    }

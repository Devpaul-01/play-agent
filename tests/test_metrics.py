"""Tests for eval.metrics.compute_metrics, against hand-built
BenchmarkOutcome scenarios covering each of Sec16's six metrics."""

from __future__ import annotations

from play_agent.eval.metrics import BenchmarkOutcome, compute_metrics
from play_agent.eval.mutations import Mutation, MutationType
from play_agent.orchestrator.states import Phase
from play_agent.store.models import Attempt, AttemptKind, VerificationOutcome

RENAME_MUTATION = Mutation(
    mutation_type=MutationType.ATTRIBUTE_RENAME,
    component_file="pages/LoginPage.tsx",
    original_testid="login-username",
    correct_repair_testid="username-field",
    description="test",
)

REMOVAL_MUTATION = Mutation(
    mutation_type=MutationType.ELEMENT_REMOVAL,
    component_file="pages/TaskFormPage.tsx",
    original_testid="task-title-input",
    correct_repair_testid=None,
    description="test",
)


def make_attempt(
    *,
    candidate_rank: int = 1,
    attempt_kind: AttemptKind = AttemptKind.CANDIDATE,
    verification_outcome: VerificationOutcome | None = VerificationOutcome.PASS,
    patch_new_locator_expr: str | None = None,
    estimated_cost_usd: float | None = 0.001,
    state_duration_ms: int | None = 500,
) -> Attempt:
    return Attempt(
        run_id="run-1",
        candidate_rank=candidate_rank,
        attempt_kind=attempt_kind,
        verification_outcome=verification_outcome,
        patch_new_locator_expr=patch_new_locator_expr,
        estimated_cost_usd=estimated_cost_usd,
        state_duration_ms=state_duration_ms,
    )


def test_empty_outcomes_returns_zeroed_metrics():
    metrics = compute_metrics([])

    assert metrics.scenario_count == 0
    assert metrics.success_rate == 0.0
    assert metrics.candidates_tried_per_success is None
    assert metrics.mean_latency_ms is None


def test_success_rate_is_fraction_succeeded():
    outcomes = [
        BenchmarkOutcome(
            mutation=RENAME_MUTATION,
            final_phase=Phase.SUCCEEDED,
            attempts=(make_attempt(patch_new_locator_expr='page.getByTestId("username-field")'),),
        ),
        BenchmarkOutcome(mutation=RENAME_MUTATION, final_phase=Phase.FAILED, attempts=()),
    ]

    metrics = compute_metrics(outcomes)

    assert metrics.success_rate == 0.5


def test_correct_repair_does_not_count_as_false_repair():
    outcomes = [
        BenchmarkOutcome(
            mutation=RENAME_MUTATION,
            final_phase=Phase.SUCCEEDED,
            attempts=(make_attempt(patch_new_locator_expr='page.getByTestId("username-field")'),),
        ),
    ]

    metrics = compute_metrics(outcomes)

    assert metrics.false_repair_rate == 0.0


def test_succeeded_with_wrong_locator_counts_as_false_repair():
    # Verification passed, but the applied locator doesn't target the
    # ground-truth testid -- the exact case Sec16 says requires ground
    # truth to detect at all.
    outcomes = [
        BenchmarkOutcome(
            mutation=RENAME_MUTATION,
            final_phase=Phase.SUCCEEDED,
            attempts=(make_attempt(patch_new_locator_expr='page.getByTestId("some-other-id")'),),
        ),
    ]

    metrics = compute_metrics(outcomes)

    assert metrics.false_repair_rate == 1.0


def test_succeeded_element_removal_scenario_is_always_a_false_repair():
    # element_removal's correct_repair_testid is None by construction --
    # any SUCCEEDED outcome here means the agent fabricated a locator
    # that happened to pass, which is exactly what false_repair_rate
    # exists to catch.
    outcomes = [
        BenchmarkOutcome(
            mutation=REMOVAL_MUTATION,
            final_phase=Phase.SUCCEEDED,
            attempts=(make_attempt(patch_new_locator_expr='page.getByText("Title")'),),
        ),
    ]

    metrics = compute_metrics(outcomes)

    assert metrics.false_repair_rate == 1.0


def test_succeeded_with_no_recorded_locator_expr_counts_as_false_repair():
    # A SUCCEEDED outcome with nothing to check the claim against is
    # not evidence of a correct repair -- fails closed.
    outcomes = [
        BenchmarkOutcome(
            mutation=RENAME_MUTATION,
            final_phase=Phase.SUCCEEDED,
            attempts=(make_attempt(patch_new_locator_expr=None),),
        ),
    ]

    metrics = compute_metrics(outcomes)

    assert metrics.false_repair_rate == 1.0


def test_correct_rejection_rate_only_considers_removal_scenarios():
    outcomes = [
        # A rename scenario that got rejected -- NOT a correct
        # rejection, it's a missed repair (there WAS a correct answer).
        BenchmarkOutcome(mutation=RENAME_MUTATION, final_phase=Phase.REJECTED, attempts=()),
        # A removal scenario that got rejected -- genuinely correct,
        # since there's no valid repair for it.
        BenchmarkOutcome(mutation=REMOVAL_MUTATION, final_phase=Phase.REJECTED, attempts=()),
    ]

    metrics = compute_metrics(outcomes)

    # Denominator is scenarios expecting rejection (just the removal
    # one) -- 1 correct rejection out of 1 such scenario = 1.0, not 0.5.
    assert metrics.correct_rejection_rate == 1.0


def test_correct_rejection_rate_zero_when_removal_scenario_not_rejected():
    outcomes = [
        BenchmarkOutcome(
            mutation=REMOVAL_MUTATION,
            final_phase=Phase.SUCCEEDED,
            attempts=(make_attempt(patch_new_locator_expr='page.getByText("x")'),),
        ),
    ]

    metrics = compute_metrics(outcomes)

    assert metrics.correct_rejection_rate == 0.0


def test_candidates_tried_per_success_counts_distinct_ranks():
    outcomes = [
        BenchmarkOutcome(
            mutation=RENAME_MUTATION,
            final_phase=Phase.SUCCEEDED,
            attempts=(
                make_attempt(
                    candidate_rank=1,
                    verification_outcome=VerificationOutcome.FAIL,
                    patch_new_locator_expr='page.getByTestId("wrong")',
                ),
                make_attempt(
                    candidate_rank=2,
                    verification_outcome=VerificationOutcome.PASS,
                    patch_new_locator_expr='page.getByTestId("username-field")',
                ),
            ),
        ),
    ]

    metrics = compute_metrics(outcomes)

    assert metrics.candidates_tried_per_success == 2.0


def test_candidates_tried_per_success_none_when_no_successes():
    outcomes = [BenchmarkOutcome(mutation=RENAME_MUTATION, final_phase=Phase.FAILED, attempts=())]

    metrics = compute_metrics(outcomes)

    assert metrics.candidates_tried_per_success is None


def test_re_diagnosis_rate_counts_re_diagnosis_attempts():
    outcomes = [
        BenchmarkOutcome(
            mutation=RENAME_MUTATION,
            final_phase=Phase.SUCCEEDED,
            attempts=(
                make_attempt(attempt_kind=AttemptKind.CANDIDATE),
                make_attempt(attempt_kind=AttemptKind.CANDIDATE),
                make_attempt(attempt_kind=AttemptKind.RE_DIAGNOSIS),
            ),
        ),
    ]

    metrics = compute_metrics(outcomes)

    assert metrics.re_diagnosis_rate == 1 / 3


def test_total_cost_sums_across_all_attempts():
    outcomes = [
        BenchmarkOutcome(
            mutation=RENAME_MUTATION,
            final_phase=Phase.SUCCEEDED,
            attempts=(
                make_attempt(estimated_cost_usd=0.002),
                make_attempt(estimated_cost_usd=0.003),
            ),
        ),
    ]

    metrics = compute_metrics(outcomes)

    assert abs(metrics.total_cost_usd - 0.005) < 1e-9


def test_total_cost_treats_missing_cost_as_zero():
    outcomes = [
        BenchmarkOutcome(
            mutation=RENAME_MUTATION,
            final_phase=Phase.SUCCEEDED,
            attempts=(make_attempt(estimated_cost_usd=None),),
        ),
    ]

    metrics = compute_metrics(outcomes)

    assert metrics.total_cost_usd == 0.0


def test_mean_latency_averages_recorded_durations():
    outcomes = [
        BenchmarkOutcome(
            mutation=RENAME_MUTATION,
            final_phase=Phase.SUCCEEDED,
            attempts=(
                make_attempt(state_duration_ms=100),
                make_attempt(state_duration_ms=300),
            ),
        ),
    ]

    metrics = compute_metrics(outcomes)

    assert metrics.mean_latency_ms == 200.0


def test_mean_latency_none_when_no_durations_recorded():
    outcomes = [
        BenchmarkOutcome(
            mutation=RENAME_MUTATION,
            final_phase=Phase.SUCCEEDED,
            attempts=(make_attempt(state_duration_ms=None),),
        ),
    ]

    metrics = compute_metrics(outcomes)

    assert metrics.mean_latency_ms is None

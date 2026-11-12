"""Repair-quality metrics computed from runs/attempts (architecture Sec16).

Sec16 names six metrics: success rate; false-repair rate; correct-
rejection rate; candidates-tried-per-success; re-diagnosis rate; cost
and latency per repair. This module computes all six from a list of
BenchmarkOutcome (one per scenario run), which pairs a Driver run's
real Store data with the ground truth the mutation that produced the
scenario carried.

False-repair rate is the one metric that cannot be computed from
runs/attempts alone -- Sec16 is explicit that this is "why the
synthetic benchmark matters": SUCCEEDED (verification passed) tells
you the test re-passed, not that it resolved to the RIGHT element. The
mutation's correct_repair_testid is what makes that distinguishable;
this module checks whether the ACTUALLY-APPLIED locator expression
targets that testid, not merely whether verification passed.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from play_agent.eval.mutations import Mutation
from play_agent.orchestrator.states import Phase
from play_agent.store.models import Attempt, AttemptKind


@dataclass(frozen=True)
class BenchmarkOutcome:
    """One scenario's result: the mutation that produced it, the
    Driver's final phase, and the attempts the Store recorded for that
    run. Kept separate from Run/Attempt themselves -- this is an
    eval-time pairing of production data with ground truth the
    production schema has no business storing."""

    mutation: Mutation
    final_phase: Phase
    attempts: tuple[Attempt, ...]


@dataclass(frozen=True)
class BenchmarkMetrics:
    scenario_count: int
    success_rate: float
    false_repair_rate: float
    correct_rejection_rate: float
    candidates_tried_per_success: float | None  # None when there were no successes to average
    re_diagnosis_rate: float
    total_cost_usd: float
    mean_latency_ms: float | None  # None when no attempts recorded any duration


def compute_metrics(outcomes: list[BenchmarkOutcome]) -> BenchmarkMetrics:
    if not outcomes:
        return BenchmarkMetrics(
            scenario_count=0,
            success_rate=0.0,
            false_repair_rate=0.0,
            correct_rejection_rate=0.0,
            candidates_tried_per_success=None,
            re_diagnosis_rate=0.0,
            total_cost_usd=0.0,
            mean_latency_ms=None,
        )

    n = len(outcomes)
    succeeded = [o for o in outcomes if o.final_phase == Phase.SUCCEEDED]
    rejected = [o for o in outcomes if o.final_phase == Phase.REJECTED]

    false_repairs = [o for o in succeeded if _is_false_repair(o)]
    # "Correct rejection" (Sec16: "genuine defects/ambiguous cases
    # correctly declined") only applies to scenarios that SHOULD be
    # rejected -- element_removal mutations, whose
    # correct_repair_testid is None by construction (mutations.py).
    # Rejecting an attribute_rename scenario (which does have a correct
    # repair) is a missed repair, not a correct rejection.
    scenarios_expecting_rejection = [o for o in outcomes if o.mutation.correct_repair_testid is None]
    correct_rejections = [o for o in rejected if o.mutation.correct_repair_testid is None]

    candidate_counts_for_successes = [
        _candidates_tried(o) for o in succeeded if _candidates_tried(o) is not None
    ]

    all_attempts = [a for o in outcomes for a in o.attempts]
    re_diagnosis_attempts = [a for a in all_attempts if a.attempt_kind == AttemptKind.RE_DIAGNOSIS]

    total_cost = sum(a.estimated_cost_usd or 0.0 for a in all_attempts)
    durations = [a.state_duration_ms for a in all_attempts if a.state_duration_ms is not None]

    return BenchmarkMetrics(
        scenario_count=n,
        success_rate=len(succeeded) / n,
        false_repair_rate=(len(false_repairs) / len(succeeded)) if succeeded else 0.0,
        correct_rejection_rate=(
            len(correct_rejections) / len(scenarios_expecting_rejection)
            if scenarios_expecting_rejection
            else 0.0
        ),
        candidates_tried_per_success=(
            statistics.mean(candidate_counts_for_successes)
            if candidate_counts_for_successes
            else None
        ),
        re_diagnosis_rate=(len(re_diagnosis_attempts) / len(all_attempts)) if all_attempts else 0.0,
        total_cost_usd=total_cost,
        mean_latency_ms=statistics.mean(durations) if durations else None,
    )


def _candidates_tried(outcome: BenchmarkOutcome) -> int | None:
    """Distinct candidate_rank values attempted before success -- 'how
    many candidates did diagnosis have to try,' per Sec16."""

    if outcome.final_phase != Phase.SUCCEEDED or not outcome.attempts:
        return None
    return len({a.candidate_rank for a in outcome.attempts})


def _is_false_repair(outcome: BenchmarkOutcome) -> bool:
    """True if verification passed but the applied locator does not
    target the mutation's ground-truth correct_repair_testid.

    A SUCCEEDED outcome with correct_repair_testid=None (an
    element_removal scenario) is ALWAYS a false repair by definition --
    there is no correct non-empty repair for a removed hook, so any
    SUCCEEDED result there means the agent fabricated a locator that
    happened to make verification pass without actually being correct
    (Sec16's whole reason false-repair rate needs ground truth at all).
    """

    if outcome.mutation.correct_repair_testid is None:
        return True

    applied_expr = _last_applied_locator_expression(outcome.attempts)
    if applied_expr is None:
        # SUCCEEDED but no attempt recorded an applied expression --
        # treat as a false repair rather than silently excluding it:
        # a success with nothing to check against is not evidence the
        # repair was correct.
        return True

    return outcome.mutation.correct_repair_testid not in applied_expr


def _last_applied_locator_expression(attempts: tuple[Attempt, ...]) -> str | None:
    for attempt in reversed(attempts):
        if attempt.patch_new_locator_expr:
            return attempt.patch_new_locator_expr
    return None

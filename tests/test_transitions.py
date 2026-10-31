"""Unit tests for the pure state-transition function (architecture Sec6).

No I/O, no mocking -- every test constructs a RunState and an Event by
hand and asserts on the RunState that comes back. This is the entire
point of keeping transition() pure: the retry policy (capped candidates,
one re-diagnosis pass, then reject) is fully exercised here with zero
external dependencies.
"""

from __future__ import annotations

import pytest

from play_agent.orchestrator.states import (
    Candidate,
    DiagnosisCompleted,
    EvidenceCollected,
    PatchApplied,
    Phase,
    ProposalValidated,
    RepairProposed,
    RunState,
    VerificationCompleted,
)
from play_agent.orchestrator.transitions import IllegalTransition, transition


def make_state(**overrides) -> RunState:
    defaults = {"run_id": "run-1", "phase": Phase.COLLECTING_EVIDENCE}
    defaults.update(overrides)
    return RunState(**defaults)


def make_candidates(n: int) -> tuple[Candidate, ...]:
    return tuple(
        Candidate(rank=i + 1, locator_call_id=f"loc-{i + 1}", reasoning=f"reason {i + 1}")
        for i in range(n)
    )


def test_evidence_collected_moves_to_diagnosing():
    state = make_state()
    new_state = transition(state, EvidenceCollected(evidence={"trace": "..."}))

    assert new_state.phase == Phase.DIAGNOSING
    assert new_state.evidence == {"trace": "..."}


def test_diagnosis_with_candidates_moves_to_proposing_repair():
    state = make_state(phase=Phase.DIAGNOSING)
    candidates = make_candidates(2)

    new_state = transition(state, DiagnosisCompleted(candidates=candidates))

    assert new_state.phase == Phase.PROPOSING_REPAIR
    assert new_state.candidates == candidates
    assert new_state.current_candidate_index == 0


def test_diagnosis_with_no_candidates_rejects_immediately():
    state = make_state(phase=Phase.DIAGNOSING)

    new_state = transition(state, DiagnosisCompleted(candidates=()))

    assert new_state.phase == Phase.REJECTED
    assert new_state.rejection_reason == "diagnosis reported no repairable candidates"
    assert new_state.is_terminal()


def test_diagnosis_caps_candidates_at_max():
    state = make_state(phase=Phase.DIAGNOSING)
    candidates = make_candidates(10)

    new_state = transition(state, DiagnosisCompleted(candidates=candidates))

    assert len(new_state.candidates) == 3
    assert new_state.candidates[0].rank == 1
    assert new_state.candidates[-1].rank == 3


def test_repair_proposed_fills_in_current_candidate_expr():
    candidates = make_candidates(2)
    state = make_state(phase=Phase.PROPOSING_REPAIR, candidates=candidates, current_candidate_index=0)

    new_state = transition(state, RepairProposed(new_locator_expr='getByTestId("x")'))

    assert new_state.phase == Phase.VALIDATING_PROPOSAL
    assert new_state.candidates[0].new_locator_expr == 'getByTestId("x")'
    assert new_state.candidates[1].new_locator_expr is None


def test_valid_proposal_moves_to_applying_patch():
    state = make_state(phase=Phase.VALIDATING_PROPOSAL)

    new_state = transition(state, ProposalValidated(valid=True))

    assert new_state.phase == Phase.APPLYING_PATCH


def test_patch_applied_moves_to_verifying():
    state = make_state(phase=Phase.APPLYING_PATCH)

    new_state = transition(state, PatchApplied(applied=True))

    assert new_state.phase == Phase.VERIFYING


def test_verification_pass_succeeds():
    state = make_state(phase=Phase.VERIFYING)

    new_state = transition(state, VerificationCompleted(outcome="pass"))

    assert new_state.phase == Phase.SUCCEEDED
    assert new_state.is_terminal()


def test_verification_fail_advances_to_next_candidate():
    candidates = make_candidates(3)
    state = make_state(phase=Phase.VERIFYING, candidates=candidates, current_candidate_index=0)

    new_state = transition(state, VerificationCompleted(outcome="fail"))

    assert new_state.phase == Phase.PROPOSING_REPAIR
    assert new_state.current_candidate_index == 1
    assert not new_state.is_terminal()


def test_flaky_verification_treated_same_as_fail():
    candidates = make_candidates(3)
    state = make_state(phase=Phase.VERIFYING, candidates=candidates, current_candidate_index=0)

    new_state = transition(state, VerificationCompleted(outcome="flaky"))

    assert new_state.phase == Phase.PROPOSING_REPAIR
    assert new_state.current_candidate_index == 1


def test_invalid_proposal_advances_to_next_candidate_without_reaching_sandbox():
    candidates = make_candidates(2)
    state = make_state(phase=Phase.VALIDATING_PROPOSAL, candidates=candidates, current_candidate_index=0)

    new_state = transition(state, ProposalValidated(valid=False, reason="disallowed method"))

    assert new_state.phase == Phase.PROPOSING_REPAIR
    assert new_state.current_candidate_index == 1


def test_patch_apply_failure_advances_to_next_candidate():
    candidates = make_candidates(2)
    state = make_state(phase=Phase.APPLYING_PATCH, candidates=candidates, current_candidate_index=0)

    new_state = transition(state, PatchApplied(applied=False, reason="file changed on disk"))

    assert new_state.phase == Phase.PROPOSING_REPAIR
    assert new_state.current_candidate_index == 1


def test_exhausting_all_candidates_triggers_re_diagnosis():
    candidates = make_candidates(3)
    state = make_state(
        phase=Phase.VERIFYING,
        candidates=candidates,
        current_candidate_index=2,
        re_diagnosis_count=0,
    )

    new_state = transition(state, VerificationCompleted(outcome="fail"))

    assert new_state.phase == Phase.DIAGNOSING
    assert new_state.candidates == ()
    assert new_state.current_candidate_index == 0
    assert new_state.re_diagnosis_count == 1


def test_exhausting_re_diagnosis_candidates_too_rejects():
    candidates = make_candidates(3)
    state = make_state(
        phase=Phase.VERIFYING,
        candidates=candidates,
        current_candidate_index=2,
        re_diagnosis_count=1,
    )

    new_state = transition(state, VerificationCompleted(outcome="fail"))

    assert new_state.phase == Phase.REJECTED
    assert new_state.is_terminal()
    assert "exhausted" in new_state.rejection_reason
    assert new_state.re_diagnosis_count == 1


def test_full_happy_path_second_candidate_succeeds():
    state = make_state()

    state = transition(state, EvidenceCollected(evidence={"trace": "x"}))
    assert state.phase == Phase.DIAGNOSING

    state = transition(state, DiagnosisCompleted(candidates=make_candidates(2)))
    assert state.phase == Phase.PROPOSING_REPAIR
    assert state.current_candidate_index == 0

    state = transition(state, RepairProposed(new_locator_expr='getByTestId("a")'))
    state = transition(state, ProposalValidated(valid=True))
    state = transition(state, PatchApplied(applied=True))
    state = transition(state, VerificationCompleted(outcome="fail"))

    assert state.phase == Phase.PROPOSING_REPAIR
    assert state.current_candidate_index == 1

    state = transition(state, RepairProposed(new_locator_expr='getByTestId("b")'))
    state = transition(state, ProposalValidated(valid=True))
    state = transition(state, PatchApplied(applied=True))
    state = transition(state, VerificationCompleted(outcome="pass"))

    assert state.phase == Phase.SUCCEEDED
    assert state.candidates[1].new_locator_expr == 'getByTestId("b")'


def test_event_invalid_for_phase_raises():
    state = make_state(phase=Phase.COLLECTING_EVIDENCE)

    with pytest.raises(IllegalTransition):
        transition(state, VerificationCompleted(outcome="pass"))


def test_terminal_state_rejects_any_further_event():
    state = make_state(phase=Phase.SUCCEEDED)

    with pytest.raises(IllegalTransition):
        transition(state, EvidenceCollected(evidence={}))


def test_terminal_state_rejection_also_rejects_further_events():
    state = make_state(phase=Phase.REJECTED, rejection_reason="no candidates")

    with pytest.raises(IllegalTransition):
        transition(state, DiagnosisCompleted(candidates=()))
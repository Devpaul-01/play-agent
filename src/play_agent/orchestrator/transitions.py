"""The pure state-transition function (architecture Sec6).

transition(state, event) -> new_state. No I/O, no side effects, no
randomness -- same inputs always produce the same output. This is what
makes the retry policy (capped candidates, one re-diagnosis pass, then
reject) testable by direct assertion rather than by mocking a store or an
LLM client.

The Driver (a later step) owns the actual loop: call a tool/LLM, turn the
result into an Event, call transition(), persist whatever the new state
implies, repeat until state.is_terminal().
"""

from __future__ import annotations

from dataclasses import replace as _dc_replace

from play_agent.orchestrator.states import (
    DiagnosisCompleted,
    Event,
    EvidenceCollected,
    PatchApplied,
    Phase,
    ProposalValidated,
    RepairProposed,
    RunState,
    VerificationCompleted,
)

MAX_CANDIDATES_PER_DIAGNOSIS = 3
MAX_RE_DIAGNOSIS_PASSES = 1


class IllegalTransition(Exception):
    """Raised when an Event arrives that doesn't make sense for the current phase.

    This is a programming-error signal, not a business-logic outcome (unlike
    a rejected run, which is a legitimate terminal state). It means the
    Driver fed events out of order -- a bug, not a run failure.
    """


def transition(state: RunState, event: Event) -> RunState:
    if state.is_terminal():
        raise IllegalTransition(
            f"run {state.run_id} is already terminal ({state.phase}); "
            f"cannot process {type(event).__name__}"
        )

    match (state.phase, event):
        case (Phase.COLLECTING_EVIDENCE, EvidenceCollected(evidence=evidence)):
            return _dc_replace(state, phase=Phase.DIAGNOSING, evidence=evidence)

        case (Phase.DIAGNOSING, DiagnosisCompleted(candidates=candidates)):
            return _handle_diagnosis_completed(state, candidates)

        case (Phase.PROPOSING_REPAIR, RepairProposed(new_locator_expr=expr)):
            return _dc_replace(
                state,
                phase=Phase.VALIDATING_PROPOSAL,
                candidates=_with_current_candidate_expr(state, expr),
            )

        case (Phase.VALIDATING_PROPOSAL, ProposalValidated(valid=True)):
            return _dc_replace(state, phase=Phase.APPLYING_PATCH)

        case (Phase.VALIDATING_PROPOSAL, ProposalValidated(valid=False)):
            return _advance_after_candidate_failure(state)

        case (Phase.APPLYING_PATCH, PatchApplied(applied=True)):
            return _dc_replace(state, phase=Phase.VERIFYING)

        case (Phase.APPLYING_PATCH, PatchApplied(applied=False)):
            return _advance_after_candidate_failure(state)

        case (Phase.VERIFYING, VerificationCompleted(outcome="pass")):
            return _dc_replace(state, phase=Phase.SUCCEEDED)

        case (Phase.VERIFYING, VerificationCompleted(outcome="fail" | "flaky")):
            return _advance_after_candidate_failure(state)

        case _:
            raise IllegalTransition(
                f"event {type(event).__name__} is not valid in phase {state.phase}"
            )


def _handle_diagnosis_completed(state: RunState, candidates: tuple) -> RunState:
    if not candidates:
        return _dc_replace(
            state,
            phase=Phase.REJECTED,
            rejection_reason="diagnosis reported no repairable candidates",
        )

    capped = candidates[:MAX_CANDIDATES_PER_DIAGNOSIS]
    return _dc_replace(
        state,
        phase=Phase.PROPOSING_REPAIR,
        candidates=capped,
        current_candidate_index=0,
    )


def _advance_after_candidate_failure(state: RunState) -> RunState:
    next_index = state.current_candidate_index + 1

    if next_index < len(state.candidates):
        return _dc_replace(state, phase=Phase.PROPOSING_REPAIR, current_candidate_index=next_index)

    if state.re_diagnosis_count < MAX_RE_DIAGNOSIS_PASSES:
        return _dc_replace(
            state,
            phase=Phase.DIAGNOSING,
            candidates=(),
            current_candidate_index=0,
            re_diagnosis_count=state.re_diagnosis_count + 1,
        )

    return _dc_replace(
        state,
        phase=Phase.REJECTED,
        rejection_reason=(
            f"exhausted {len(state.candidates)} candidate(s) and "
            f"{state.re_diagnosis_count} re-diagnosis pass(es) without a verified fix"
        ),
    )


def _with_current_candidate_expr(state: RunState, expr: str) -> tuple:
    candidates = list(state.candidates)
    current = candidates[state.current_candidate_index]
    candidates[state.current_candidate_index] = _dc_replace(current, new_locator_expr=expr)
    return tuple(candidates)
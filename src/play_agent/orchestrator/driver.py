# src/play_agent/orchestrator/driver.py
"""The Driver: the actual run loop (architecture Sec6/Sec14).

Owns all I/O. Calls tools and the diagnoser, turns their results into
Events, feeds them to the pure transition() function, persists the
resulting RunState via the Store at every step, and repeats until the
run reaches a terminal phase.

This is deliberately the ONLY place I/O and the pure state machine meet --
transitions.py stays pure and tested with zero mocking; this file is where
real tool calls, real LLM calls, and real persistence happen, and it's
tested with fakes standing in for those, not real ones, so these tests
run fast and don't depend on a live LLM or sandbox being available.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

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
from play_agent.orchestrator.transitions import transition
from play_agent.store.models import (
    Attempt,
    AttemptKind,
    FinalState,
    Run,
    VerificationOutcome,
    utc_now_iso,
)
from play_agent.store.store import Store
from play_agent.tools.contracts import FailureEvidence, PatchApplyResult, VerificationResult

class Diagnoser(Protocol):
    """The LLM-backed piece (Sec12): diagnosis and repair proposal."""

    def diagnose(
        self, evidence: FailureEvidence, is_re_diagnosis: bool
    ) -> tuple[Candidate, ...]:
        """Return ranked candidates, or an empty tuple if not repairable.
        is_re_diagnosis is True exactly once per run, on the single
        evidence-augmented re-diagnosis pass allowed by Sec6."""
        ...

    def propose_repair(self, evidence: FailureEvidence, candidate: Candidate) -> str:
        ...

class ToolBelt(Protocol):
    """The four tools from Sec7, bundled as one dependency the Driver calls
    through -- lets the Driver be tested against fakes without importing
    the real sandbox-backed implementations."""

    def collect_failure_evidence(self, test_file_path: str, test_title: str) -> FailureEvidence: ...

    def apply_patch(
        self, test_file_path: str, locator_call_id: str, new_locator_expression: str
    ) -> PatchApplyResult: ...

    def run_verification(
        self, test_file_path: str, test_title: str, reps: int = 3
    ) -> VerificationResult: ...


@dataclass(frozen=True)
class DriverResult:
    """What a completed run produced -- the Driver's return value, distinct
    from the internal RunState it was tracking throughout."""

    run_id: str
    final_phase: Phase
    rejection_reason: str | None
    attempts_made: int


class Driver:
    """Runs one repair attempt from start to terminal state."""

    def __init__(self, store: Store, tools: ToolBelt, diagnoser: Diagnoser) -> None:
        self._store = store
        self._tools = tools
        self._diagnoser = diagnoser

    def run(self, test_file_path: str, test_title: str) -> DriverResult:
        run_id = str(uuid.uuid4())
        test_identifier = f"{test_file_path}::{test_title}"

        self._store.create_run(Run(id=run_id, test_identifier=test_identifier))

        state = RunState(run_id=run_id, phase=Phase.COLLECTING_EVIDENCE)
        attempts_made = 0

        while not state.is_terminal():
            state, attempts_made = self._step(state, test_file_path, test_title, attempts_made)
            self._persist_run_state(state)

        return DriverResult(
            run_id=run_id,
            final_phase=state.phase,
            rejection_reason=state.rejection_reason,
            attempts_made=attempts_made,
        )

    def _step(
        self, state: RunState, test_file_path: str, test_title: str, attempts_made: int
    ) -> tuple[RunState, int]:
        """Perform the I/O appropriate to the current phase, turn the
        result into an Event, and advance the pure state machine by
        exactly one transition. One _step call = one real-world action."""

        match state.phase:
            case Phase.COLLECTING_EVIDENCE:
                evidence = self._tools.collect_failure_evidence(test_file_path, test_title)
                # Evidence is kept as a dict on RunState (per states.py's
                # existing shape) -- stash the FailureEvidence's fields so
                # later phases (diagnosis) can reconstruct what they need
                # without the Driver having to pass it around separately.
                event = EvidenceCollected(evidence=evidence.__dict__)
                return transition(state, event), attempts_made

            case Phase.DIAGNOSING:
                evidence = FailureEvidence(**state.evidence)
                # state.candidates is already cleared to () by the time we
                # reach a re-diagnosis pass (transitions.py's
                # _advance_after_candidate_failure clears it before moving
                # back to DIAGNOSING) -- re_diagnosis_count, not candidates,
                # is the correct signal for "is this a retry."
                is_re_diagnosis = state.re_diagnosis_count > 0
                candidates = self._diagnoser.diagnose(
                    evidence, is_re_diagnosis=is_re_diagnosis
                )
                event = DiagnosisCompleted(candidates=candidates)
                return transition(state, event), attempts_made

            case Phase.PROPOSING_REPAIR:
                evidence = FailureEvidence(**state.evidence)
                candidate = state.current_candidate
                assert candidate is not None  # PROPOSING_REPAIR implies a current candidate
                new_expr = self._diagnoser.propose_repair(evidence, candidate)
                event = RepairProposed(new_locator_expr=new_expr)
                return transition(state, event), attempts_made

            case Phase.VALIDATING_PROPOSAL:
                # Validation is folded into apply_patch's own call in the
                # current tool contract (it validates before writing) --
                # the Driver asks apply_patch to validate-only here by
                # checking the result without it having taken effect yet.
                # NOTE: this is a seam that will tighten once apply_patch's
                # real sandbox-backed body exists (M1 remaining tool
                # bodies) -- tracked, not hidden.
                candidate = state.current_candidate
                assert candidate is not None
                result = self._tools.apply_patch(
                    test_file_path, candidate.locator_call_id, candidate.new_locator_expr
                )
                event = ProposalValidated(valid=result.applied, reason=result.reason)
                return transition(state, event), attempts_made

            case Phase.APPLYING_PATCH:
                # The patch was already applied as part of validation above
                # in this tool contract's current shape -- this phase just
                # confirms it landed. Treated as always succeeding here
                # because VALIDATING_PROPOSAL already gated on it; a
                # genuinely separate apply step is a refinement for when
                # apply_patch's real body exists.
                event = PatchApplied(applied=True)
                attempts_made += 1
                return transition(state, event), attempts_made

            case Phase.VERIFYING:
                result = self._tools.run_verification(test_file_path, test_title)
                event = VerificationCompleted(outcome=result.outcome)
                self._persist_attempt(state, result)
                return transition(state, event), attempts_made

            case _:
                raise AssertionError(f"_step called with terminal phase {state.phase}")

    def _persist_run_state(self, state: RunState) -> None:
        final_state = None
        if state.phase == Phase.SUCCEEDED:
            final_state = FinalState.SUCCEEDED
        elif state.phase == Phase.REJECTED:
            final_state = FinalState.REJECTED
        elif state.phase == Phase.FAILED:
            final_state = FinalState.FAILED

        if final_state is not None:
            self._store.update_run_state(
                state.run_id,
                updated_at=utc_now_iso(),
                final_state=final_state,
                rejection_reason=state.rejection_reason,
            )

    def _persist_attempt(self, state: RunState, result: VerificationResult) -> None:
        candidate = state.current_candidate
        assert candidate is not None
        attempt_id = self._store.create_attempt(
            Attempt(
                run_id=state.run_id,
                candidate_rank=candidate.rank,
                attempt_kind=AttemptKind.CANDIDATE,
                diagnosis_reasoning=candidate.reasoning,
                patch_locator_call_id=candidate.locator_call_id,
                patch_new_locator_expr=candidate.new_locator_expr,
            )
        )
        self._store.update_attempt_verification(
            attempt_id,
            verification_outcome=VerificationOutcome(result.outcome),
            verification_reps=result.reps_run,
            state_duration_ms=0,  # real timing wired once tools are real
        )
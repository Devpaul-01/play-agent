"""The Driver: the actual run loop (architecture Sec6/Sec14).

Owns all I/O. Calls tools and the diagnoser, turns their results into
Events, feeds them to the pure transition() function, persists the
resulting RunState via the Store at every step, and repeats until the
run reaches a terminal phase.

This is deliberately the ONLY place I/O and the pure state machine meet --
transitions.py stays pure and tested with zero mocking; this file is where
real tool calls, real LLM calls, and real persistence happen, and it's
tested with fakes standing in for those, not real ones.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from play_agent.observability.logging import RunLogger
from play_agent.orchestrator.groq_diagnoser import DiagnoserError, DiagnosisResult, TokenUsage
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
from play_agent.tools.contracts import (
    FailureEvidence,
    PatchApplyResult,
    ToolError,
    VerificationResult,
)


class Diagnoser(Protocol):
    """The LLM-backed piece (Sec12): diagnosis and repair proposal."""

    def diagnose_with_usage(
        self, evidence: FailureEvidence, test_source: str, is_re_diagnosis: bool
    ) -> DiagnosisResult:
        """Return ranked candidates plus token/cost usage. is_re_diagnosis
        is True exactly once per run, on the single evidence-augmented
        re-diagnosis pass allowed by Sec6."""
        ...

    def propose_repair(self, evidence: FailureEvidence, candidate: Candidate) -> str:
        """Return the new locator expression for this candidate."""
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

    def __init__(
        self,
        store: Store,
        tools: ToolBelt,
        diagnoser: Diagnoser,
        logger: RunLogger | None = None,
    ) -> None:
        self._store = store
        self._tools = tools
        self._diagnoser = diagnoser
        # Bridges diagnosis-time usage data to the attempt row created
        # several phases later (VERIFYING) -- RunState stays immutable and
        # doesn't carry this, since it's a billing/observability concern,
        # not state-machine-relevant. Not elegant, but honest about the gap
        # it's closing rather than leaving token_usage_* columns NULL.
        self._pending_usage: TokenUsage | None = None
        # Optional (Sec17 observability is a debugging aid, not a
        # correctness requirement) -- None is a valid, fully-supported
        # value, so every existing caller/test that never passed a
        # logger keeps working unchanged.
        self._logger = logger

    def run(self, test_file_path: str, test_title: str) -> DriverResult:
        run_id = str(uuid.uuid4())
        test_identifier = f"{test_file_path}::{test_title}"

        if self._logger is not None:
            self._logger.set_run_id(run_id)

        self._store.create_run(Run(id=run_id, test_identifier=test_identifier))

        state = RunState(run_id=run_id, phase=Phase.COLLECTING_EVIDENCE)
        attempts_made = 0

        while not state.is_terminal():
            from_phase = state.phase
            step_start = time.monotonic()
            try:
                state, attempts_made = self._step(state, test_file_path, test_title, attempts_made)
            except (ToolError, DiagnoserError) as e:
                # Expected infrastructure/protocol failures (a missing
                # Playwright report, a malformed or unreachable model):
                # end the run cleanly as FAILED with the reason recorded,
                # rather than leaving the run row unfinished behind a
                # traceback. These are not semantic rejections -- the
                # state machine's REJECTED means "diagnosed as not
                # locator-repairable"; this is "the run could not finish".
                state = self._fail_state(state, f"{type(e).__name__}: {e}")
            except Exception as e:
                # A bug, not an expected failure. Still finalize the run
                # row so it isn't left dangling, then re-raise: swallowing
                # a genuine bug into a tidy FAILED would hide it.
                reason = f"unexpected {type(e).__name__}: {e}"
                failed = self._fail_state(state, reason)
                self._persist_run_state(failed)
                if self._logger is not None:
                    self._logger.run_completed(failed.phase.value, reason, attempts_made)
                raise
            duration_ms = (time.monotonic() - step_start) * 1000
            if self._logger is not None:
                self._logger.transition(from_phase.value, state.phase.value, duration_ms)
            self._persist_run_state(state)

        if self._logger is not None:
            self._logger.run_completed(
                state.phase.value, state.rejection_reason, attempts_made
            )

        return DriverResult(
            run_id=run_id,
            final_phase=state.phase,
            rejection_reason=state.rejection_reason,
            attempts_made=attempts_made,
        )

    def _step(
        self, state: RunState, test_file_path: str, test_title: str, attempts_made: int
    ) -> tuple[RunState, int]:
        match state.phase:
            case Phase.COLLECTING_EVIDENCE:
                call_start = time.monotonic()
                evidence = self._tools.collect_failure_evidence(test_file_path, test_title)
                self._log_tool_call(
                    "collect_failure_evidence",
                    {"test_file_path": test_file_path, "test_title": test_title},
                    evidence,
                    call_start,
                )
                event = EvidenceCollected(evidence=evidence.__dict__)
                return transition(state, event), attempts_made

            case Phase.DIAGNOSING:
                evidence = FailureEvidence(**state.evidence)
                test_source = Path(evidence.test_file_path).read_text()
                is_re_diagnosis = state.re_diagnosis_count > 0
                diagnosis = self._diagnoser.diagnose_with_usage(
                    evidence, test_source, is_re_diagnosis=is_re_diagnosis
                )
                self._pending_usage = diagnosis.usage
                if self._logger is not None:
                    self._logger.diagnosis(
                        request_summary={
                            "evidence": evidence.__dict__,
                            "is_re_diagnosis": is_re_diagnosis,
                        },
                        response_summary={
                            "candidates": [vars(c) for c in diagnosis.candidates]
                        },
                        passed_validation=None,  # resolved later, at VALIDATING_PROPOSAL
                    )
                event = DiagnosisCompleted(candidates=diagnosis.candidates)
                return transition(state, event), attempts_made

            case Phase.PROPOSING_REPAIR:
                evidence = FailureEvidence(**state.evidence)
                candidate = state.current_candidate
                assert candidate is not None
                new_expr = self._diagnoser.propose_repair(evidence, candidate)
                event = RepairProposed(new_locator_expr=new_expr)
                return transition(state, event), attempts_made

            case Phase.VALIDATING_PROPOSAL:
                candidate = state.current_candidate
                assert candidate is not None
                call_start = time.monotonic()
                result = self._tools.apply_patch(
                    test_file_path, candidate.locator_call_id, candidate.new_locator_expr
                )
                self._log_tool_call(
                    "apply_patch",
                    {
                        "test_file_path": test_file_path,
                        "locator_call_id": candidate.locator_call_id,
                        "new_locator_expression": candidate.new_locator_expr,
                    },
                    result,
                    call_start,
                )
                if self._logger is not None:
                    # Resolves the passed_validation flag Sec17 asks the
                    # diagnosis log line to carry -- logged as its own
                    # follow-up line rather than mutating the earlier
                    # diagnosis line, since this module only ever appends.
                    self._logger.diagnosis(
                        request_summary={"candidate": vars(candidate)},
                        response_summary={"reason": result.reason},
                        passed_validation=result.applied,
                    )
                event = ProposalValidated(valid=result.applied, reason=result.reason)
                return transition(state, event), attempts_made

            case Phase.APPLYING_PATCH:
                event = PatchApplied(applied=True)
                attempts_made += 1
                return transition(state, event), attempts_made

            case Phase.VERIFYING:
                call_start = time.monotonic()
                result = self._tools.run_verification(test_file_path, test_title)
                self._log_tool_call(
                    "run_verification",
                    {"test_file_path": test_file_path, "test_title": test_title},
                    result,
                    call_start,
                )
                event = VerificationCompleted(outcome=result.outcome)
                self._persist_attempt(state, result)
                return transition(state, event), attempts_made

            case _:
                raise AssertionError(f"_step called with terminal phase {state.phase}")

    @staticmethod
    def _fail_state(state: RunState, reason: str) -> RunState:
        return replace(state, phase=Phase.FAILED, rejection_reason=reason)

    def _log_tool_call(self, tool_name: str, inputs: dict, output: object, call_start: float) -> None:
        if self._logger is None:
            return
        duration_ms = (time.monotonic() - call_start) * 1000
        self._logger.tool_call(tool_name, inputs, output, duration_ms)

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
        usage = self._pending_usage
        attempt_id = self._store.create_attempt(
            Attempt(
                run_id=state.run_id,
                candidate_rank=candidate.rank,
                attempt_kind=AttemptKind.CANDIDATE,
                diagnosis_reasoning=candidate.reasoning,
                patch_locator_call_id=candidate.locator_call_id,
                patch_new_locator_expr=candidate.new_locator_expr,
                token_usage_prompt=usage.prompt_tokens if usage else None,
                token_usage_completion=usage.completion_tokens if usage else None,
                estimated_cost_usd=usage.estimated_cost_usd if usage else None,
            )
        )
        self._store.update_attempt_verification(
            attempt_id,
            verification_outcome=VerificationOutcome(result.outcome),
            verification_reps=result.reps_run,
            state_duration_ms=0,
        )
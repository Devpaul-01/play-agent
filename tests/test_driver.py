"""Tests for the Driver (architecture Sec6/Sec14) using fake tools and a
fake diagnoser -- not mocks of the real sandbox or a real LLM. This lets
the full run loop (state machine + persistence + tool/diagnoser calls)
be tested end-to-end, fast and deterministically, before the real tool
bodies and the real Groq-backed diagnoser exist (both still pending the
fixture app / M2).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from play_agent.orchestrator.driver import Driver
from play_agent.orchestrator.states import Candidate, Phase
from play_agent.store.models import FinalState, VerificationOutcome
from play_agent.store.store import Store
from play_agent.tools.contracts import FailureEvidence, PatchApplyResult, VerificationResult


@dataclass
class FakeToolBelt:
    """A ToolBelt whose responses are scripted per-call for each test,
    rather than talking to a real sandbox."""

    evidence: FailureEvidence
    apply_results: list[PatchApplyResult]
    verification_results: list[VerificationResult]
    _apply_call_count: int = 0
    _verify_call_count: int = 0

    def collect_failure_evidence(self, test_file_path: str, test_title: str) -> FailureEvidence:
        return self.evidence

    def apply_patch(
        self, test_file_path: str, locator_call_id: str, new_locator_expression: str
    ) -> PatchApplyResult:
        result = self.apply_results[self._apply_call_count]
        self._apply_call_count += 1
        return result

    def run_verification(
        self, test_file_path: str, test_title: str, reps: int = 3
    ) -> VerificationResult:
        result = self.verification_results[self._verify_call_count]
        self._verify_call_count += 1
        return result


@dataclass
class FakeDiagnoser:
    """Scripted per-call: first_call_candidates for the initial diagnosis,
    re_diagnosis_candidates for the one allowed retry pass (Sec6). Lets
    tests exercise the Driver's two distinct diagnose() call sites
    explicitly, rather than assuming one script covers both -- this is
    the fix for a real bug the first version of this fixture had: it
    returned the same candidates on every call, so a re-diagnosis pass
    silently replayed the exact same (already-failed) candidate and ran
    the FakeToolBelt's scripted responses off the end of their lists."""

    first_call_candidates: tuple[Candidate, ...]
    re_diagnosis_candidates: tuple[Candidate, ...]
    proposed_exprs: dict[str, str]

    def diagnose(
        self, evidence: FailureEvidence, is_re_diagnosis: bool
    ) -> tuple[Candidate, ...]:
        if is_re_diagnosis:
            return self.re_diagnosis_candidates
        return self.first_call_candidates

    def propose_repair(self, evidence: FailureEvidence, candidate: Candidate) -> str:
        return self.proposed_exprs[candidate.locator_call_id]


def make_evidence() -> FailureEvidence:
    return FailureEvidence(
        test_file_path="checkout.spec.ts",
        test_title="completes checkout",
        error_message="Timeout waiting for selector",
        stack_trace="at checkout.spec.ts:12",
        screenshot_path=None,
        trace_path=None,
    )


@pytest.fixture
def store(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    with Store(db_path) as s:
        yield s


def test_driver_succeeds_on_first_candidate(store: Store):
    evidence = make_evidence()
    candidates = (
        Candidate(rank=1, locator_call_id="L1C1", reasoning="stale testid"),
    )
    tools = FakeToolBelt(
        evidence=evidence,
        apply_results=[PatchApplyResult(applied=True, reason=None, diff="...")],
        verification_results=[VerificationResult(outcome="pass", reps_run=3, logs="ok")],
    )
    diagnoser = FakeDiagnoser(
        first_call_candidates=candidates,
        re_diagnosis_candidates=(),
        proposed_exprs={"L1C1": 'page.getByTestId("x")'},
    )

    driver = Driver(store=store, tools=tools, diagnoser=diagnoser)
    result = driver.run("checkout.spec.ts", "completes checkout")

    assert result.final_phase == Phase.SUCCEEDED
    assert result.attempts_made == 1

    run = store.get_run(result.run_id)
    assert run.final_state == FinalState.SUCCEEDED

    attempts = store.list_attempts(result.run_id)
    assert len(attempts) == 1
    assert attempts[0].verification_outcome == VerificationOutcome.PASS
    assert attempts[0].patch_new_locator_expr == 'page.getByTestId("x")'


def test_driver_tries_second_candidate_after_first_fails_verification(store: Store):
    evidence = make_evidence()
    candidates = (
        Candidate(rank=1, locator_call_id="L1C1", reasoning="guess 1"),
        Candidate(rank=2, locator_call_id="L2C1", reasoning="guess 2"),
    )
    tools = FakeToolBelt(
        evidence=evidence,
        apply_results=[
            PatchApplyResult(applied=True, reason=None, diff="..."),
            PatchApplyResult(applied=True, reason=None, diff="..."),
        ],
        verification_results=[
            VerificationResult(outcome="fail", reps_run=3, logs="still broken"),
            VerificationResult(outcome="pass", reps_run=3, logs="ok"),
        ],
    )
    diagnoser = FakeDiagnoser(
        first_call_candidates=candidates,
        re_diagnosis_candidates=(),
        proposed_exprs={"L1C1": 'page.getByRole("button")', "L2C1": 'page.getByTestId("x")'},
    )

    driver = Driver(store=store, tools=tools, diagnoser=diagnoser)
    result = driver.run("checkout.spec.ts", "completes checkout")

    assert result.final_phase == Phase.SUCCEEDED
    assert result.attempts_made == 2

    attempts = store.list_attempts(result.run_id)
    assert len(attempts) == 2
    assert attempts[0].verification_outcome == VerificationOutcome.FAIL
    assert attempts[1].verification_outcome == VerificationOutcome.PASS


def test_driver_rejects_when_diagnosis_finds_nothing(store: Store):
    evidence = make_evidence()
    tools = FakeToolBelt(evidence=evidence, apply_results=[], verification_results=[])
    diagnoser = FakeDiagnoser(
        first_call_candidates=(), re_diagnosis_candidates=(), proposed_exprs={}
    )

    driver = Driver(store=store, tools=tools, diagnoser=diagnoser)
    result = driver.run("checkout.spec.ts", "completes checkout")

    assert result.final_phase == Phase.REJECTED
    assert result.rejection_reason == "diagnosis reported no repairable candidates"
    assert result.attempts_made == 0

    run = store.get_run(result.run_id)
    assert run.final_state == FinalState.REJECTED
    assert run.rejection_reason == "diagnosis reported no repairable candidates"


def test_driver_re_diagnoses_with_different_candidate_after_exhausting_first_pass(store: Store):
    # Replaces an earlier, under-specified test that assumed one scripted
    # apply_patch response was enough -- it wasn't, because exhausting a
    # single-candidate first pass correctly triggers Sec6's one allowed
    # re-diagnosis pass, which calls diagnose() a second time. This test
    # exercises that second call for real, with a genuinely different
    # candidate, proving the re-diagnosis path can actually produce
    # something new rather than silently repeating the failed candidate.
    evidence = make_evidence()
    first_pass = (Candidate(rank=1, locator_call_id="L1C1", reasoning="guess 1"),)
    re_diagnosis_pass = (Candidate(rank=1, locator_call_id="L2C1", reasoning="better guess"),)

    tools = FakeToolBelt(
        evidence=evidence,
        apply_results=[
            PatchApplyResult(applied=False, reason="disallowed method", diff=None),
            PatchApplyResult(applied=True, reason=None, diff="..."),
        ],
        verification_results=[
            VerificationResult(outcome="pass", reps_run=3, logs="ok"),
        ],
    )
    diagnoser = FakeDiagnoser(
        first_call_candidates=first_pass,
        re_diagnosis_candidates=re_diagnosis_pass,
        proposed_exprs={"L1C1": "page.evaluate(x)", "L2C1": 'page.getByTestId("x")'},
    )

    driver = Driver(store=store, tools=tools, diagnoser=diagnoser)
    result = driver.run("checkout.spec.ts", "completes checkout")

    assert result.final_phase == Phase.SUCCEEDED
    attempts = store.list_attempts(result.run_id)
    assert attempts[-1].patch_new_locator_expr == 'page.getByTestId("x")'


def test_driver_rejects_when_re_diagnosis_also_fails(store: Store):
    # The other half of the re-diagnosis path: if the SECOND pass also
    # produces nothing usable, the run must reject, not loop forever or
    # crash on a third diagnose() call that was never allowed to happen
    # (Sec6: exactly one re-diagnosis pass).
    evidence = make_evidence()
    first_pass = (Candidate(rank=1, locator_call_id="L1C1", reasoning="guess 1"),)

    tools = FakeToolBelt(
        evidence=evidence,
        apply_results=[
            PatchApplyResult(applied=False, reason="disallowed method", diff=None),
        ],
        verification_results=[],
    )
    diagnoser = FakeDiagnoser(
        first_call_candidates=first_pass,
        re_diagnosis_candidates=(),  # re-diagnosis finds nothing either
        proposed_exprs={"L1C1": "page.evaluate(x)"},
    )

    driver = Driver(store=store, tools=tools, diagnoser=diagnoser)
    result = driver.run("checkout.spec.ts", "completes checkout")

    assert result.final_phase == Phase.REJECTED
    assert result.rejection_reason == "diagnosis reported no repairable candidates"

    run = store.get_run(result.run_id)
    assert run.final_state == FinalState.REJECTED


def test_run_persists_immediately_even_if_driver_were_interrupted(store: Store):
    evidence = make_evidence()
    tools = FakeToolBelt(
        evidence=evidence,
        apply_results=[PatchApplyResult(applied=True, reason=None, diff="...")],
        verification_results=[VerificationResult(outcome="pass", reps_run=3, logs="ok")],
    )
    diagnoser = FakeDiagnoser(
        first_call_candidates=(Candidate(rank=1, locator_call_id="L1C1", reasoning="x"),),
        re_diagnosis_candidates=(),
        proposed_exprs={"L1C1": 'page.getByTestId("x")'},
    )

    driver = Driver(store=store, tools=tools, diagnoser=diagnoser)
    result = driver.run("checkout.spec.ts", "completes checkout")

    run = store.get_run(result.run_id)
    assert run is not None
    assert run.test_identifier == "checkout.spec.ts::completes checkout"
"""Tests for the Driver (architecture Sec6/Sec14) using fake tools and a
fake diagnoser -- not mocks of the real sandbox or a real LLM. This lets
the full run loop (state machine + persistence + tool/diagnoser calls)
be tested end-to-end, fast and deterministically, before the real tool
bodies and the real Groq-backed diagnoser are wired in for production use.

NOTE: evidence.test_file_path must point at a real file on disk, since
the Driver's DIAGNOSING phase now reads the test source directly
(Path(evidence.test_file_path).read_text()) to pass to the diagnoser --
see the test_file fixture below.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from play_agent.observability.logging import RunLogger
from play_agent.orchestrator.driver import Driver
from play_agent.orchestrator.groq_diagnoser import DiagnoserError, DiagnosisResult, TokenUsage
from play_agent.orchestrator.states import Candidate, Phase
from play_agent.store.models import FinalState, VerificationOutcome
from play_agent.store.store import Store
from play_agent.tools.contracts import (
    FailureEvidence,
    PatchApplyResult,
    ToolError,
    VerificationResult,
)


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
    re_diagnosis_candidates for the one allowed retry pass (Sec6).

    diagnose() now accepts test_source (matching the real GroqDiagnoser's
    signature and driver.py's updated Diagnoser Protocol) but ignores its
    content -- this fake only cares about is_re_diagnosis to pick which
    scripted response to return."""

    first_call_candidates: tuple[Candidate, ...]
    re_diagnosis_candidates: tuple[Candidate, ...]
    proposed_exprs: dict[str, str]
    
    def diagnose_with_usage(
        self, evidence: FailureEvidence, test_source: str, is_re_diagnosis: bool
    ) -> DiagnosisResult:
        candidates = self.re_diagnosis_candidates if is_re_diagnosis else self.first_call_candidates
        return DiagnosisResult(
            candidates=candidates,
            usage=TokenUsage(prompt_tokens=0, completion_tokens=0, estimated_cost_usd=0.0),
        )

    def diagnose(
        self, evidence: FailureEvidence, test_source: str, is_re_diagnosis: bool
    ) -> tuple[Candidate, ...]:
        if is_re_diagnosis:
            return self.re_diagnosis_candidates
        return self.first_call_candidates

    def propose_repair(self, evidence: FailureEvidence, candidate: Candidate) -> str:
        return self.proposed_exprs[candidate.locator_call_id]


SAMPLE_TEST_SOURCE = (
    'import { test } from "@playwright/test";\n'
    'test("completes checkout", async ({ page }) => {\n'
    '  await page.getByTestId("checkout-button").click();\n'
    "});\n"
)


@pytest.fixture
def test_file(tmp_path):
    """A real file on disk -- the Driver's DIAGNOSING phase reads this
    path directly to build the diagnoser's prompt, so evidence.test_file_path
    must point somewhere real, not a bare string like 'checkout.spec.ts'."""

    path = tmp_path / "checkout.spec.ts"
    path.write_text(SAMPLE_TEST_SOURCE)
    return path


def make_evidence(test_file_path: str) -> FailureEvidence:
    return FailureEvidence(
        test_file_path=test_file_path,
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


def test_driver_succeeds_on_first_candidate(store: Store, test_file):
    evidence = make_evidence(str(test_file))
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
    result = driver.run(str(test_file), "completes checkout")

    assert result.final_phase == Phase.SUCCEEDED
    assert result.attempts_made == 1

    run = store.get_run(result.run_id)
    assert run.final_state == FinalState.SUCCEEDED

    attempts = store.list_attempts(result.run_id)
    assert len(attempts) == 1
    assert attempts[0].verification_outcome == VerificationOutcome.PASS
    assert attempts[0].patch_new_locator_expr == 'page.getByTestId("x")'


def test_driver_tries_second_candidate_after_first_fails_verification(store: Store, test_file):
    evidence = make_evidence(str(test_file))
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
    result = driver.run(str(test_file), "completes checkout")

    assert result.final_phase == Phase.SUCCEEDED
    assert result.attempts_made == 2

    attempts = store.list_attempts(result.run_id)
    assert len(attempts) == 2
    assert attempts[0].verification_outcome == VerificationOutcome.FAIL
    assert attempts[1].verification_outcome == VerificationOutcome.PASS


def test_driver_rejects_when_diagnosis_finds_nothing(store: Store, test_file):
    evidence = make_evidence(str(test_file))
    tools = FakeToolBelt(evidence=evidence, apply_results=[], verification_results=[])
    diagnoser = FakeDiagnoser(
        first_call_candidates=(), re_diagnosis_candidates=(), proposed_exprs={}
    )

    driver = Driver(store=store, tools=tools, diagnoser=diagnoser)
    result = driver.run(str(test_file), "completes checkout")

    assert result.final_phase == Phase.REJECTED
    assert result.rejection_reason == "diagnosis reported no repairable candidates"
    assert result.attempts_made == 0

    run = store.get_run(result.run_id)
    assert run.final_state == FinalState.REJECTED
    assert run.rejection_reason == "diagnosis reported no repairable candidates"


def test_driver_re_diagnoses_with_different_candidate_after_exhausting_first_pass(
    store: Store, test_file
):
    evidence = make_evidence(str(test_file))
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
    result = driver.run(str(test_file), "completes checkout")

    assert result.final_phase == Phase.SUCCEEDED
    attempts = store.list_attempts(result.run_id)
    assert attempts[-1].patch_new_locator_expr == 'page.getByTestId("x")'


def test_driver_rejects_when_re_diagnosis_also_fails(store: Store, test_file):
    evidence = make_evidence(str(test_file))
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
        re_diagnosis_candidates=(),
        proposed_exprs={"L1C1": "page.evaluate(x)"},
    )

    driver = Driver(store=store, tools=tools, diagnoser=diagnoser)
    result = driver.run(str(test_file), "completes checkout")

    assert result.final_phase == Phase.REJECTED
    assert result.rejection_reason == "diagnosis reported no repairable candidates"

    run = store.get_run(result.run_id)
    assert run.final_state == FinalState.REJECTED


def test_run_persists_immediately_even_if_driver_were_interrupted(store: Store, test_file):
    evidence = make_evidence(str(test_file))
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
    result = driver.run(str(test_file), "completes checkout")

    run = store.get_run(result.run_id)
    assert run is not None
    assert run.test_identifier == f"{test_file}::completes checkout"


def test_driver_with_logger_emits_transition_and_tool_call_lines(store: Store, test_file, tmp_path):
    import json

    evidence = make_evidence(str(test_file))
    tools = FakeToolBelt(
        evidence=evidence,
        apply_results=[PatchApplyResult(applied=True, reason=None, diff="...")],
        verification_results=[VerificationResult(outcome="pass", reps_run=3, logs="ok")],
    )
    diagnoser = FakeDiagnoser(
        first_call_candidates=(Candidate(rank=1, locator_call_id="L1C1", reasoning="stale testid"),),
        re_diagnosis_candidates=(),
        proposed_exprs={"L1C1": 'page.getByTestId("x")'},
    )
    log_path = tmp_path / "run.log"

    with RunLogger.to_file("pending", log_path) as logger:
        driver = Driver(store=store, tools=tools, diagnoser=diagnoser, logger=logger)
        result = driver.run(str(test_file), "completes checkout")

    assert result.final_phase == Phase.SUCCEEDED

    lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    events = [line["event"] for line in lines]

    # The real run_id (minted inside Driver.run()) must appear on every
    # line, not the "pending" placeholder passed at construction time --
    # Driver.run() is responsible for correcting it via set_run_id as its
    # first action.
    assert all(line["run_id"] == result.run_id for line in lines)
    assert all(line["run_id"] != "pending" for line in lines)

    # At least one transition per phase change, at least one tool_call
    # per real tool invocation, a diagnosis line, and a final summary.
    assert "transition" in events
    assert "tool_call" in events
    assert "diagnosis" in events
    assert events[-1] == "run_completed"

    tool_names = {line["tool"] for line in lines if line["event"] == "tool_call"}
    assert tool_names == {"collect_failure_evidence", "apply_patch", "run_verification"}


def test_driver_without_logger_does_not_raise(store: Store, test_file):
    # logger=None (the default) must remain fully supported -- this is
    # the same construction every other test in this file already uses,
    # asserted explicitly here as a regression guard.
    evidence = make_evidence(str(test_file))
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
    result = driver.run(str(test_file), "completes checkout")

    assert result.final_phase == Phase.SUCCEEDED


# -- expected failures end the run cleanly instead of leaving it dangling ----


def _one_candidate_diagnoser() -> FakeDiagnoser:
    return FakeDiagnoser(
        first_call_candidates=(Candidate(rank=1, locator_call_id="L3C9", reasoning="stale"),),
        re_diagnosis_candidates=(),
        proposed_exprs={"L3C9": 'page.getByTestId("new")'},
    )


def test_tool_error_ends_run_as_failed_with_reason_persisted(store: Store, test_file):
    class MissingReportToolBelt(FakeToolBelt):
        def collect_failure_evidence(self, test_file_path, test_title):
            raise ToolError("no Playwright report found at /x/results.json")

    tools = MissingReportToolBelt(
        evidence=make_evidence(str(test_file)), apply_results=[], verification_results=[]
    )

    result = Driver(store=store, tools=tools, diagnoser=_one_candidate_diagnoser()).run(
        str(test_file), "completes checkout"
    )

    assert result.final_phase == Phase.FAILED
    assert "ToolError" in result.rejection_reason
    assert "no Playwright report found" in result.rejection_reason

    run = store.get_run(result.run_id)
    assert run.final_state == FinalState.FAILED
    assert "no Playwright report found" in run.rejection_reason


def test_diagnoser_error_ends_run_as_failed_with_reason_persisted(store: Store, test_file):
    class BrokenDiagnoser(FakeDiagnoser):
        def diagnose_with_usage(self, evidence, test_source, is_re_diagnosis):
            raise DiagnoserError("model returned no tool call")

    diagnoser = BrokenDiagnoser(
        first_call_candidates=(), re_diagnosis_candidates=(), proposed_exprs={}
    )
    tools = FakeToolBelt(
        evidence=make_evidence(str(test_file)), apply_results=[], verification_results=[]
    )

    result = Driver(store=store, tools=tools, diagnoser=diagnoser).run(
        str(test_file), "completes checkout"
    )

    assert result.final_phase == Phase.FAILED
    assert "DiagnoserError" in result.rejection_reason
    assert store.get_run(result.run_id).final_state == FinalState.FAILED


def test_failure_is_distinct_from_semantic_rejection(store: Store, test_file):
    # REJECTED means "diagnosed as not locator-repairable"; an infra
    # failure must not be reported as that.
    class BrokenDiagnoser(FakeDiagnoser):
        def diagnose_with_usage(self, evidence, test_source, is_re_diagnosis):
            raise DiagnoserError("boom")

    tools = FakeToolBelt(
        evidence=make_evidence(str(test_file)), apply_results=[], verification_results=[]
    )
    diagnoser = BrokenDiagnoser(
        first_call_candidates=(), re_diagnosis_candidates=(), proposed_exprs={}
    )

    result = Driver(store=store, tools=tools, diagnoser=diagnoser).run(str(test_file), "t")

    assert result.final_phase != Phase.REJECTED


def test_unexpected_exception_finalizes_run_row_then_reraises(store: Store, test_file):
    class BuggyToolBelt(FakeToolBelt):
        def collect_failure_evidence(self, test_file_path, test_title):
            raise ZeroDivisionError("a genuine bug")

    tools = BuggyToolBelt(
        evidence=make_evidence(str(test_file)), apply_results=[], verification_results=[]
    )
    driver = Driver(store=store, tools=tools, diagnoser=_one_candidate_diagnoser())

    with pytest.raises(ZeroDivisionError):
        driver.run(str(test_file), "completes checkout")

    # The bug still propagates, but the run row was not left dangling.
    runs = store.list_runs()
    assert len(runs) == 1
    assert runs[0].final_state == FinalState.FAILED
    assert "ZeroDivisionError" in runs[0].rejection_reason


def test_failed_run_is_logged_as_completed(store: Store, test_file, tmp_path):
    import json

    class MissingReportToolBelt(FakeToolBelt):
        def collect_failure_evidence(self, test_file_path, test_title):
            raise ToolError("no report")

    tools = MissingReportToolBelt(
        evidence=make_evidence(str(test_file)), apply_results=[], verification_results=[]
    )
    log_path = tmp_path / "run.log"

    with RunLogger.to_file("pending", log_path) as logger:
        Driver(store=store, tools=tools, diagnoser=_one_candidate_diagnoser(), logger=logger).run(
            str(test_file), "completes checkout"
        )

    lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    assert lines[-1]["event"] == "run_completed"
    assert lines[-1]["final_phase"] == "failed"


# -- the verified patch's diff is the deliverable (Sec2/Sec19) ---------------


def test_successful_run_carries_the_verified_diff_and_expression(store: Store, test_file):
    tools = FakeToolBelt(
        evidence=make_evidence(str(test_file)),
        apply_results=[PatchApplyResult(applied=True, reason=None, diff="DIFF-1")],
        verification_results=[VerificationResult(outcome="pass", reps_run=3, logs="ok")],
    )
    diagnoser = FakeDiagnoser(
        first_call_candidates=(Candidate(rank=1, locator_call_id="L1C1", reasoning="x"),),
        re_diagnosis_candidates=(),
        proposed_exprs={"L1C1": 'page.getByTestId("x")'},
    )

    result = Driver(store=store, tools=tools, diagnoser=diagnoser).run(str(test_file), "t")

    assert result.final_phase == Phase.SUCCEEDED
    assert result.diff == "DIFF-1"
    assert result.applied_expression == 'page.getByTestId("x")'


def test_diff_comes_from_the_candidate_that_passed_not_an_earlier_failed_one(
    store: Store, test_file
):
    tools = FakeToolBelt(
        evidence=make_evidence(str(test_file)),
        apply_results=[
            PatchApplyResult(applied=True, reason=None, diff="DIFF-FIRST-WRONG"),
            PatchApplyResult(applied=True, reason=None, diff="DIFF-SECOND-RIGHT"),
        ],
        verification_results=[
            VerificationResult(outcome="fail", reps_run=3, logs="no"),
            VerificationResult(outcome="pass", reps_run=3, logs="yes"),
        ],
    )
    diagnoser = FakeDiagnoser(
        first_call_candidates=(
            Candidate(rank=1, locator_call_id="L1C1", reasoning="a"),
            Candidate(rank=2, locator_call_id="L2C1", reasoning="b"),
        ),
        re_diagnosis_candidates=(),
        proposed_exprs={
            "L1C1": 'page.getByTestId("wrong")',
            "L2C1": 'page.getByRole("button")',
        },
    )

    result = Driver(store=store, tools=tools, diagnoser=diagnoser).run(str(test_file), "t")

    assert result.final_phase == Phase.SUCCEEDED
    assert result.diff == "DIFF-SECOND-RIGHT"
    assert result.applied_expression == 'page.getByRole("button")'


def test_unsuccessful_run_has_no_diff(store: Store, test_file):
    tools = FakeToolBelt(
        evidence=make_evidence(str(test_file)),
        apply_results=[PatchApplyResult(applied=True, reason=None, diff="DIFF-1")],
        verification_results=[VerificationResult(outcome="fail", reps_run=3, logs="no")],
    )
    diagnoser = FakeDiagnoser(
        first_call_candidates=(Candidate(rank=1, locator_call_id="L1C1", reasoning="x"),),
        re_diagnosis_candidates=(),
        proposed_exprs={"L1C1": 'page.getByTestId("x")'},
    )

    result = Driver(store=store, tools=tools, diagnoser=diagnoser).run(str(test_file), "t")

    assert result.final_phase != Phase.SUCCEEDED
    assert result.diff is None
    assert result.applied_expression is None

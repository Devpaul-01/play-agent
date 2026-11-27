"""End-to-end tests: real Driver + real SandboxedToolBelt + real Docker
sandbox running the real fixture app's Playwright suite.

Only the LLM is replaced, by a scripted diagnoser, so the run is
deterministic and needs no API key. Everything else -- evidence parsing,
locator validation, patch application, in-container verification reps, the
store and the run log -- is the production code path.

Skipped unless PLAY_AGENT_E2E=1. Required environment:
  E2E_APP_PATH                 mutated fixture copy (scripts/e2e_prepare_fixture.py),
                               already run once so test-results/results.json
                               holds the failing report
  PLAY_AGENT_SANDBOX_IMAGE     sandbox image (default play-agent-sandbox:latest)
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from play_agent.observability.logging import RunLogger
from play_agent.orchestrator.driver import Driver
from play_agent.orchestrator.groq_diagnoser import DiagnosisResult, TokenUsage
from play_agent.orchestrator.states import Candidate, Phase
from play_agent.sandbox.container import SandboxConfig
from play_agent.store.store import Store
from play_agent.tools.patch_validation import find_locator_calls
from play_agent.tools.sandboxed_tool_belt import SandboxedToolBelt, SandboxedToolBeltConfig

pytestmark = pytest.mark.skipif(
    os.environ.get("PLAY_AGENT_E2E") != "1",
    reason="end-to-end test: set PLAY_AGENT_E2E=1 (needs Docker and a prepared fixture)",
)

TEST_TITLE = "shows an error on invalid credentials"
STALE_TESTID = "login-username"
RENAMED_TESTID = "username-field"


class ScriptedDiagnoser:
    """Stands in for the LLM: always blames the stale locator, and proposes
    whatever expression it was constructed with."""

    def __init__(self, proposal: str) -> None:
        self._proposal = proposal
        self.seen_evidence = []

    def diagnose_with_usage(self, evidence, test_source, is_re_diagnosis):
        self.seen_evidence.append(evidence)
        source_bytes = test_source.encode("utf-8")
        call_id = next(
            c.locator_call_id
            for c in find_locator_calls(test_source)
            if STALE_TESTID in source_bytes[c.start_byte : c.end_byte].decode("utf-8")
        )
        candidate = Candidate(rank=1, locator_call_id=call_id, reasoning="scripted")
        return DiagnosisResult(
            candidates=(candidate,), usage=TokenUsage(0, 0, 0.0)
        )

    def propose_repair(self, evidence, candidate):
        return self._proposal


@pytest.fixture(scope="module")
def app_path() -> Path:
    path = os.environ.get("E2E_APP_PATH")
    assert path, "E2E_APP_PATH must point at the mutated fixture copy"
    return Path(path).resolve()


def _run(app_path: Path, tmp_path: Path, proposal: str, reps: int):
    sandbox_config = SandboxConfig(
        image=os.environ.get("PLAY_AGENT_SANDBOX_IMAGE", "play-agent-sandbox:latest"),
        app_path=str(app_path),
        app_host="localhost",
        app_port=5173,
    )
    diagnoser = ScriptedDiagnoser(proposal)
    store = Store(str(tmp_path / "e2e.sqlite3"))
    log_path = tmp_path / "e2e.log"
    with RunLogger.to_file("pending", str(log_path)) as logger, SandboxedToolBelt(
        SandboxedToolBeltConfig(
            sandbox_config=sandbox_config,
            verification_reps=reps,
            component_src_root=str(app_path / "frontend" / "src"),
        )
    ) as tools:
        driver = Driver(store=store, tools=tools, diagnoser=diagnoser, logger=logger)
        result = driver.run(str(app_path / "tests" / "auth.spec.ts"), TEST_TITLE)
    return result, diagnoser, store, log_path


def test_repairs_renamed_testid_end_to_end(app_path: Path, tmp_path: Path) -> None:
    proposal = f'page.getByTestId("{RENAMED_TESTID}")'
    result, diagnoser, store, log_path = _run(app_path, tmp_path, proposal, reps=3)

    assert result.final_phase == Phase.SUCCEEDED, result.rejection_reason
    assert result.applied_expression == proposal
    assert result.diff and RENAMED_TESTID in result.diff

    # The failing Playwright report was really parsed into evidence.
    assert diagnoser.seen_evidence[0].error_message
    assert STALE_TESTID in diagnoser.seen_evidence[0].error_message

    # The verified attempt was persisted and the run was logged.
    attempts = list(store.list_attempts(result.run_id))
    assert attempts, "a verified attempt must be persisted"
    assert log_path.read_text().strip(), "run log must not be empty"

    # The real test file on the host is never modified.
    original = (app_path / "tests" / "auth.spec.ts").read_text()
    assert STALE_TESTID in original and RENAMED_TESTID not in original


def test_wrong_locator_fails_verification(app_path: Path, tmp_path: Path) -> None:
    result, *_ = _run(
        app_path, tmp_path, 'page.getByTestId("no-such-element")', reps=1
    )
    assert result.final_phase == Phase.REJECTED
    assert result.final_phase != Phase.SUCCEEDED


def test_disallowed_expression_never_reaches_sandbox(app_path: Path, tmp_path: Path) -> None:
    result, *_ = _run(app_path, tmp_path, 'page.evaluate("1")', reps=1)
    assert result.final_phase == Phase.REJECTED
    assert "allowlisted" in (result.rejection_reason or "")

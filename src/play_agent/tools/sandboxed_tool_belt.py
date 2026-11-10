"""Concrete ToolBelt (architecture Sec7) wiring collect_failure_evidence,
apply_patch, and run_verification to a real sandbox, for the Driver
(Sec6/Sec14) to run against.

probe_live_dom is intentionally NOT exposed here: the Driver's ToolBelt
Protocol (driver.py) doesn't call it -- it's defined in Sec7's contract
but not yet wired into the repair loop itself, which is a separate,
pre-existing gap from the one this file closes. Exposing it here would
imply the Driver uses it, which it doesn't.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self

from play_agent.sandbox.container import Sandbox, SandboxConfig
from play_agent.tools.contracts import (
    FailureEvidence,
    PatchApplyResult,
    VerificationResult,
    apply_patch,
    collect_failure_evidence,
    run_verification,
)


@dataclass(frozen=True)
class SandboxedToolBeltConfig:
    """What's needed to stand up one sandbox per run -- a subset of
    SandboxConfig plus the Playwright report path convention this
    ToolBelt uses to satisfy collect_failure_evidence."""

    sandbox_config: SandboxConfig
    report_path: str | None = None
    verification_reps: int = 3


class SandboxedToolBelt:
    """A real ToolBelt (satisfies driver.ToolBelt structurally) that owns
    one Sandbox for its own lifetime. One instance is meant to back
    exactly one Driver.run() call -- Sec10's "one container per run"
    boundary -- not shared across runs.
    """

    def __init__(self, config: SandboxedToolBeltConfig) -> None:
        self._config = config
        # Sandbox construction itself talks to the Docker daemon (its
        # __init__ calls docker.from_env()) -- deferred to __enter__ so
        # that collect_failure_evidence and apply_patch, which never
        # touch the sandbox, can be called on a belt that was never
        # entered, without requiring Docker to be running at all.
        self._sandbox: Sandbox | None = None

    def __enter__(self) -> Self:
        self._sandbox = Sandbox(self._config.sandbox_config)
        self._sandbox.__enter__()
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._sandbox is not None:
            self._sandbox.__exit__(*exc_info)  # type: ignore[arg-type]
            self._sandbox = None

    def collect_failure_evidence(self, test_file_path: str, test_title: str) -> FailureEvidence:
        # Deliberately NOT sandboxed: this only reads the Playwright
        # report already written to disk by a prior run (Sec7's "none"
        # side effects), so there's nothing here that needs isolation --
        # unlike apply_patch and run_verification, which touch the test
        # file and execute code respectively.
        return collect_failure_evidence(test_file_path, test_title, report_path=self._config.report_path)

    def apply_patch(
        self, test_file_path: str, locator_call_id: str, new_locator_expression: str
    ) -> PatchApplyResult:
        # Also unsandboxed: apply_patch's own validation (patch_validation.py)
        # is what makes this safe to run in-process, by construction --
        # only allowlisted locator methods with literal arguments can ever
        # reach the file write, so there's no attacker-controlled code
        # path here to isolate.
        return apply_patch(test_file_path, locator_call_id, new_locator_expression)

    def run_verification(
        self, test_file_path: str, test_title: str, reps: int = 3
    ) -> VerificationResult:
        if self._sandbox is None:
            raise RuntimeError(
                "SandboxedToolBelt used outside its 'with' block -- the sandbox "
                "isn't running. Use 'with SandboxedToolBelt(config) as tools: ...'"
            )
        return run_verification(
            test_file_path,
            test_title,
            reps=reps or self._config.verification_reps,
            sandbox=self._sandbox,
        )

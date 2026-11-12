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
from pathlib import Path
from typing import Self

from play_agent.sandbox.container import Sandbox, SandboxConfig
from play_agent.tools.component_source import resolve_component_source
from play_agent.tools.contracts import (
    FailureEvidence,
    PatchApplyResult,
    VerificationResult,
    apply_patch,
    collect_failure_evidence,
    run_verification,
)
from play_agent.tools.patch_validation import find_locator_calls

# How many characters of assembled component-source context to include
# per diagnosis call -- a cap, not a target, so a test file with many
# locator calls against a large component tree can't blow out the
# prompt. Chosen to comfortably fit a handful of small JSX snippets
# (Sec11's "kept small by deliberate, disciplined construction") well
# within typical model context windows, with room to spare for the
# evidence and test source that always accompany it.
_MAX_COMPONENT_CONTEXT_CHARS = 6000


@dataclass(frozen=True)
class SandboxedToolBeltConfig:
    """What's needed to stand up one sandbox per run -- a subset of
    SandboxConfig plus the Playwright report path convention this
    ToolBelt uses to satisfy collect_failure_evidence."""

    sandbox_config: SandboxConfig
    report_path: str | None = None
    verification_reps: int = 3
    # M3/Sec11: the frontend's component source root (e.g.
    # <fixture-app>/frontend/src) to resolve locator calls against. None
    # (the default) means component-source evidence is simply omitted --
    # a missing/misconfigured root degrades diagnosis quality, it
    # doesn't block the run, since locator repair worked before M3
    # existed and this is additive context, not a hard dependency.
    component_src_root: str | None = None


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
        evidence = collect_failure_evidence(
            test_file_path, test_title, report_path=self._config.report_path
        )

        if self._config.component_src_root is None:
            return evidence

        context = self._resolve_component_context(test_file_path)
        if context is None:
            return evidence

        # FailureEvidence is frozen -- replace rather than mutate, which
        # is also why collect_failure_evidence (the free function) never
        # populates this field itself: it would need this same
        # construct-then-replace dance for a concern it doesn't own.
        from dataclasses import replace

        return replace(evidence, component_source_context=context)

    def _resolve_component_context(self, test_file_path: str) -> str | None:
        """Resolve every locator call in the test file against the
        configured component source root and assemble their matches
        into one text block (Sec11). Every call in the file is
        resolved, not just the one tied to the current failure --
        diagnosis may reasonably propose a different locator_call_id
        than the one that actually failed (e.g. a related call nearby),
        so narrowing to only the failing call's resolution would
        silently withhold evidence the diagnoser might need.
        """

        try:
            test_source = Path(test_file_path).read_text()
        except OSError:
            return None

        locator_calls = find_locator_calls(test_source)
        if not locator_calls:
            return None

        sections: list[str] = []
        total_length = 0

        for call in locator_calls:
            result = resolve_component_source(call.source_text, self._config.component_src_root)
            if not result.resolved:
                continue

            for match in result.matches:
                section = (
                    f"# {call.locator_call_id}: {call.source_text}\n"
                    f"# {match.file_path}:{match.start_line}-{match.end_line}\n"
                    f"{match.snippet}\n"
                )
                if total_length + len(section) > _MAX_COMPONENT_CONTEXT_CHARS:
                    # Truncate here rather than drop silently partway
                    # through a section -- a reader of the evidence
                    # should be able to tell context was cut, not just
                    # see it end mid-snippet with no explanation.
                    sections.append("# (further matches omitted: context size limit reached)")
                    return "\n".join(sections)
                sections.append(section)
                total_length += len(section)

        return "\n".join(sections) if sections else None

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

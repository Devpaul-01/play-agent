"""The state machine's state and event types (architecture Sec6).

Deliberately separated from the transition logic (transitions.py) and from
all I/O: states and events here are plain, immutable data. This lets the
state machine be unit-tested with zero mocking -- a test just constructs a
State and an Event and checks what comes out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Phase(str, Enum):
    """The state machine's phases, per architecture Sec6."""

    COLLECTING_EVIDENCE = "collecting_evidence"
    DIAGNOSING = "diagnosing"
    PROPOSING_REPAIR = "proposing_repair"
    VALIDATING_PROPOSAL = "validating_proposal"
    APPLYING_PATCH = "applying_patch"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"


TERMINAL_PHASES = frozenset({Phase.SUCCEEDED, Phase.FAILED, Phase.REJECTED})


@dataclass(frozen=True)
class Candidate:
    """One ranked locator-repair candidate from diagnosis (Sec6/Sec9)."""

    rank: int
    locator_call_id: str
    reasoning: str
    new_locator_expr: str | None = None


@dataclass(frozen=True)
class RunState:
    """Immutable snapshot of a run's progress through the state machine.

    Every transition produces a *new* RunState rather than mutating one in
    place -- this is what makes the transition function pure and testable
    by equality assertion on the output.
    """

    run_id: str
    phase: Phase
    evidence: dict | None = None
    candidates: tuple[Candidate, ...] = field(default_factory=tuple)
    current_candidate_index: int = 0
    re_diagnosis_count: int = 0
    rejection_reason: str | None = None

    def is_terminal(self) -> bool:
        return self.phase in TERMINAL_PHASES

    @property
    def current_candidate(self) -> Candidate | None:
        if 0 <= self.current_candidate_index < len(self.candidates):
            return self.candidates[self.current_candidate_index]
        return None


@dataclass(frozen=True)
class EvidenceCollected:
    evidence: dict


@dataclass(frozen=True)
class DiagnosisCompleted:
    candidates: tuple[Candidate, ...]


@dataclass(frozen=True)
class RepairProposed:
    new_locator_expr: str


@dataclass(frozen=True)
class ProposalValidated:
    valid: bool
    reason: str | None = None


@dataclass(frozen=True)
class PatchApplied:
    applied: bool
    reason: str | None = None


@dataclass(frozen=True)
class VerificationCompleted:
    outcome: str


Event = (
    EvidenceCollected
    | DiagnosisCompleted
    | RepairProposed
    | ProposalValidated
    | PatchApplied
    | VerificationCompleted
)
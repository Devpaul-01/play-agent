"""Data models for the runs/attempts schema (architecture doc Sec13).

These are plain dataclasses, not an ORM. The schema is small and stable
enough that an ORM would add a dependency and an abstraction layer without
real benefit -- consistent with the architecture's general bias against
unjustified complexity (Sec2 constraints).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class FinalState(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class AttemptKind(str, Enum):
    CANDIDATE = "candidate"
    RE_DIAGNOSIS = "re_diagnosis"


class VerificationOutcome(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    FLAKY = "flaky"


@dataclass
class Run:
    id: str
    test_identifier: str
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    repo_commit_ref: str | None = None
    final_state: FinalState | None = None
    rejection_reason: str | None = None


@dataclass
class Attempt:
    run_id: str
    candidate_rank: int
    attempt_kind: AttemptKind
    id: int | None = None
    diagnosis_reasoning: str | None = None
    patch_locator_call_id: str | None = None
    patch_new_locator_expr: str | None = None
    verification_outcome: VerificationOutcome | None = None
    verification_reps: int | None = None
    token_usage_prompt: int | None = None
    token_usage_completion: int | None = None
    estimated_cost_usd: float | None = None
    state_entered_at: str = field(default_factory=utc_now_iso)
    state_duration_ms: int | None = None
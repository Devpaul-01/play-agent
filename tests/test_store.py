"""Direct unit tests for the Store, against the real schema.

Per the implementation plan: these exist before anything else depends on
the store, as the foundation everything else writes to. Uses a temp file
DB per test rather than :memory: so we're exercising the exact same code
path (file-based SQLite) the real CLI will use.
"""

from __future__ import annotations

import sqlite3
import uuid

import pytest

from play_agent.store.models import (
    Attempt,
    AttemptKind,
    FinalState,
    Run,
    VerificationOutcome,
    utc_now_iso,
)
from play_agent.store.store import Store


@pytest.fixture
def store(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    with Store(db_path) as s:
        yield s


def make_run(**overrides) -> Run:
    defaults = {
        "id": str(uuid.uuid4()),
        "test_identifier": "tests/checkout.spec.ts::completes checkout",
    }
    defaults.update(overrides)
    return Run(**defaults)


def test_create_and_get_run(store: Store):
    run = make_run()
    store.create_run(run)

    fetched = store.get_run(run.id)

    assert fetched is not None
    assert fetched.id == run.id
    assert fetched.test_identifier == run.test_identifier
    assert fetched.final_state is None


def test_get_run_missing_returns_none(store: Store):
    assert store.get_run("does-not-exist") is None


def test_update_run_state_to_terminal(store: Store):
    run = make_run()
    store.create_run(run)

    store.update_run_state(
        run.id,
        updated_at=utc_now_iso(),
        final_state=FinalState.SUCCEEDED,
    )

    fetched = store.get_run(run.id)
    assert fetched.final_state == FinalState.SUCCEEDED
    assert fetched.rejection_reason is None


def test_update_run_state_with_rejection_reason(store: Store):
    run = make_run()
    store.create_run(run)

    store.update_run_state(
        run.id,
        updated_at=utc_now_iso(),
        final_state=FinalState.REJECTED,
        rejection_reason="not locator-repairable",
    )

    fetched = store.get_run(run.id)
    assert fetched.final_state == FinalState.REJECTED
    assert fetched.rejection_reason == "not locator-repairable"


def test_list_runs_filters_by_test_identifier(store: Store):
    run_a = make_run(test_identifier="a.spec.ts::test a")
    run_b = make_run(test_identifier="b.spec.ts::test b")
    store.create_run(run_a)
    store.create_run(run_b)

    results = store.list_runs(test_identifier="a.spec.ts::test a")

    assert [r.id for r in results] == [run_a.id]


def test_create_attempt_and_list(store: Store):
    run = make_run()
    store.create_run(run)

    attempt = Attempt(
        run_id=run.id,
        candidate_rank=1,
        attempt_kind=AttemptKind.CANDIDATE,
        diagnosis_reasoning="stale data-testid, element renamed",
        patch_locator_call_id="loc-42",
        patch_new_locator_expr='getByTestId("checkout-submit")',
    )
    attempt_id = store.create_attempt(attempt)

    attempts = store.list_attempts(run.id)

    assert len(attempts) == 1
    assert attempts[0].id == attempt_id
    assert attempts[0].candidate_rank == 1
    assert attempts[0].attempt_kind == AttemptKind.CANDIDATE
    assert attempts[0].verification_outcome is None


def test_update_attempt_verification(store: Store):
    run = make_run()
    store.create_run(run)
    attempt_id = store.create_attempt(
        Attempt(run_id=run.id, candidate_rank=1, attempt_kind=AttemptKind.CANDIDATE)
    )

    store.update_attempt_verification(
        attempt_id,
        verification_outcome=VerificationOutcome.PASS,
        verification_reps=3,
        state_duration_ms=4200,
    )

    attempts = store.list_attempts(run.id)
    assert attempts[0].verification_outcome == VerificationOutcome.PASS
    assert attempts[0].verification_reps == 3
    assert attempts[0].state_duration_ms == 4200


def test_attempts_ordered_by_insertion(store: Store):
    run = make_run()
    store.create_run(run)
    store.create_attempt(
        Attempt(run_id=run.id, candidate_rank=1, attempt_kind=AttemptKind.CANDIDATE)
    )
    store.create_attempt(
        Attempt(run_id=run.id, candidate_rank=2, attempt_kind=AttemptKind.CANDIDATE)
    )
    store.create_attempt(
        Attempt(run_id=run.id, candidate_rank=2, attempt_kind=AttemptKind.RE_DIAGNOSIS)
    )

    attempts = store.list_attempts(run.id)

    assert [a.attempt_kind for a in attempts] == [
        AttemptKind.CANDIDATE,
        AttemptKind.CANDIDATE,
        AttemptKind.RE_DIAGNOSIS,
    ]


def test_attempt_fk_constraint_enforced(store: Store):
    with pytest.raises(sqlite3.IntegrityError):
        store.create_attempt(
            Attempt(run_id="nonexistent-run", candidate_rank=1, attempt_kind=AttemptKind.CANDIDATE)
        )


def test_cascading_delete_not_relied_upon_but_fk_enabled(store: Store):
    cursor = store._conn.execute("PRAGMA foreign_keys")
    assert cursor.fetchone()[0] == 1
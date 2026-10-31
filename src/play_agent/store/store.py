"""The Store interface: persistence for runs/attempts (architecture Sec13/14).

Per Sec13: "Writes are synchronous at each state transition -- a crash
mid-run leaves a readable partial trail for debugging, by design not
intended to support resuming that exact run." This Store deliberately does
NOT batch writes or wrap a whole run in one transaction -- each insert/
update commits immediately, so a crash mid-run leaves exactly the partial
trail the architecture calls for.
"""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path
from typing import Self

from play_agent.store.models import (
    Attempt,
    AttemptKind,
    FinalState,
    Run,
    VerificationOutcome,
)


class Store:
    """Synchronous SQLite-backed store for runs and attempts.

    Not thread-safe by design -- play-agent's concurrency model (Sec18) is
    a lock keyed on test identifier, so only one run per test identifier is
    ever active. A single connection per Store instance is sufficient.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    def _init_schema(self) -> None:
        schema_sql = resources.files("play_agent.store").joinpath("schema.sql").read_text()
        self._conn.executescript(schema_sql)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- runs ---------------------------------------------------------------

    def create_run(self, run: Run) -> None:
        self._conn.execute(
            """
            INSERT INTO runs (id, test_identifier, repo_commit_ref,
                               created_at, updated_at, final_state, rejection_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.id,
                run.test_identifier,
                run.repo_commit_ref,
                run.created_at,
                run.updated_at,
                run.final_state.value if run.final_state else None,
                run.rejection_reason,
            ),
        )
        self._conn.commit()

    def update_run_state(
        self,
        run_id: str,
        *,
        updated_at: str,
        final_state: FinalState | None = None,
        rejection_reason: str | None = None,
    ) -> None:
        self._conn.execute(
            """
            UPDATE runs
            SET updated_at = ?, final_state = ?, rejection_reason = ?
            WHERE id = ?
            """,
            (
                updated_at,
                final_state.value if final_state else None,
                rejection_reason,
                run_id,
            ),
        )
        self._conn.commit()

    def get_run(self, run_id: str) -> Run | None:
        row = self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._row_to_run(row) if row else None

    def list_runs(self, *, test_identifier: str | None = None) -> list[Run]:
        if test_identifier is not None:
            rows = self._conn.execute(
                "SELECT * FROM runs WHERE test_identifier = ? ORDER BY created_at",
                (test_identifier,),
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM runs ORDER BY created_at").fetchall()
        return [self._row_to_run(r) for r in rows]

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> Run:
        return Run(
            id=row["id"],
            test_identifier=row["test_identifier"],
            repo_commit_ref=row["repo_commit_ref"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            final_state=FinalState(row["final_state"]) if row["final_state"] else None,
            rejection_reason=row["rejection_reason"],
        )

    # -- attempts -------------------------------------------------------------

    def create_attempt(self, attempt: Attempt) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO attempts (
                run_id, candidate_rank, attempt_kind, diagnosis_reasoning,
                patch_locator_call_id, patch_new_locator_expr,
                verification_outcome, verification_reps,
                token_usage_prompt, token_usage_completion, estimated_cost_usd,
                state_entered_at, state_duration_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt.run_id,
                attempt.candidate_rank,
                attempt.attempt_kind.value,
                attempt.diagnosis_reasoning,
                attempt.patch_locator_call_id,
                attempt.patch_new_locator_expr,
                attempt.verification_outcome.value if attempt.verification_outcome else None,
                attempt.verification_reps,
                attempt.token_usage_prompt,
                attempt.token_usage_completion,
                attempt.estimated_cost_usd,
                attempt.state_entered_at,
                attempt.state_duration_ms,
            ),
        )
        self._conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    def update_attempt_verification(
        self,
        attempt_id: int,
        *,
        verification_outcome: VerificationOutcome,
        verification_reps: int,
        state_duration_ms: int,
    ) -> None:
        self._conn.execute(
            """
            UPDATE attempts
            SET verification_outcome = ?, verification_reps = ?, state_duration_ms = ?
            WHERE id = ?
            """,
            (verification_outcome.value, verification_reps, state_duration_ms, attempt_id),
        )
        self._conn.commit()

    def list_attempts(self, run_id: str) -> list[Attempt]:
        rows = self._conn.execute(
            "SELECT * FROM attempts WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        return [self._row_to_attempt(r) for r in rows]

    @staticmethod
    def _row_to_attempt(row: sqlite3.Row) -> Attempt:
        return Attempt(
            id=row["id"],
            run_id=row["run_id"],
            candidate_rank=row["candidate_rank"],
            attempt_kind=AttemptKind(row["attempt_kind"]),
            diagnosis_reasoning=row["diagnosis_reasoning"],
            patch_locator_call_id=row["patch_locator_call_id"],
            patch_new_locator_expr=row["patch_new_locator_expr"],
            verification_outcome=(
                VerificationOutcome(row["verification_outcome"])
                if row["verification_outcome"]
                else None
            ),
            verification_reps=row["verification_reps"],
            token_usage_prompt=row["token_usage_prompt"],
            token_usage_completion=row["token_usage_completion"],
            estimated_cost_usd=row["estimated_cost_usd"],
            state_entered_at=row["state_entered_at"],
            state_duration_ms=row["state_duration_ms"],
        )
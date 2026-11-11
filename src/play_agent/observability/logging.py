"""Structured JSON logging for run observability (architecture Sec17).

Deliberately NOT a general-purpose logging framework -- Sec17 explicitly
excludes dashboards/tracing infra as unjustified for a single-developer
CLI tool. This is one JSON object per line, one line per event, written
to a file (or any writable stream); the SQLite store already holds the
durable structured record (runs/attempts tables), so this exists purely
as the debugging aid Sec17 asks for, not a second source of truth.

Redaction (Sec17): tool call logs never inline DOM content, screenshots,
or full stack traces -- they record artifact *references* (a file path,
a byte length, a hash) instead. The one exception Sec17 itself makes is
model request/response logging, which Sec17 says should be the full
structured request and response ("the primary debugging aid for a bad
diagnosis") -- so diagnosis events are NOT redacted the way tool-call
events are. Retention (pruning old log files) is left to the operator's
own log rotation; this module only ever appends, by design, so pruning
a stale file requires no coordination with it.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Self

from play_agent.observability.redaction import redact_tool_io


@dataclass
class RunLogger:
    """One instance per Driver.run() call -- not shared across runs, so
    every line it writes already carries the right run_id without
    threading it through every call site by hand.
    """

    run_id: str
    _stream: IO[str]
    _owns_stream: bool = False

    @staticmethod
    def to_file(run_id: str, path: str | Path) -> RunLogger:
        stream = Path(path).open("a", encoding="utf-8")  # noqa: SIM115 -- lifetime matches the logger's, closed in close()
        return RunLogger(run_id=run_id, _stream=stream, _owns_stream=True)

    def close(self) -> None:
        if self._owns_stream:
            self._stream.close()

    def set_run_id(self, run_id: str) -> None:
        """Lets a caller construct the logger before a run_id exists yet
        (e.g. the CLI, which opens the log file before calling
        Driver.run(), which is what actually mints the run_id) and have
        Driver.run() correct it to the real value as its very first
        action -- rather than every line in the file being tagged with
        a meaningless placeholder."""
        self.run_id = run_id

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _write(self, event: str, **fields: Any) -> None:
        line = {"run_id": self.run_id, "ts": time.time(), "event": event, **fields}
        self._stream.write(json.dumps(line, default=str) + "\n")
        self._stream.flush()  # a crash mid-run shouldn't lose buffered log lines

    def transition(self, from_phase: str, to_phase: str, duration_ms: float) -> None:
        """One line per state transition, per Sec17's first bullet."""
        self._write("transition", from_phase=from_phase, to_phase=to_phase, duration_ms=duration_ms)

    def tool_call(self, tool_name: str, inputs: dict, output: Any, duration_ms: float) -> None:
        """Tool calls logged with inputs/outputs, redacted to artifact
        references rather than inline DOM/screenshot content (Sec17).
        """
        self._write(
            "tool_call",
            tool=tool_name,
            inputs=redact_tool_io(inputs),
            output=redact_tool_io(output),
            duration_ms=duration_ms,
        )

    def diagnosis(
        self, request_summary: dict, response_summary: dict, passed_validation: bool | None
    ) -> None:
        """Full structured request/response per diagnosis call, NOT
        redacted -- Sec17 names this explicitly as "the primary
        debugging aid for a bad diagnosis," which only works if the
        actual reasoning and candidates are visible here, not just a
        reference to them. passed_validation is None until a later
        VALIDATING_PROPOSAL step resolves it; callers should prefer
        correlating by run_id over expecting this field eagerly True/False.
        """
        self._write(
            "diagnosis",
            request=request_summary,
            response=response_summary,
            passed_validation=passed_validation,
        )

    def run_completed(self, final_phase: str, rejection_reason: str | None, attempts_made: int) -> None:
        self._write(
            "run_completed",
            final_phase=final_phase,
            rejection_reason=rejection_reason,
            attempts_made=attempts_made,
        )

    @contextmanager
    def timed(self) -> Iterator[Callable[[], float]]:
        """Yields a callable returning elapsed ms so far -- used by
        callers that need the duration *before* the with-block exits
        (e.g. to pass into transition()/tool_call() themselves)."""
        start = time.monotonic()
        yield lambda: (time.monotonic() - start) * 1000

"""The run's human-facing deliverable (architecture Sec2/Sec19): "a diff file
plus a markdown summary".

The agent never edits the user's test file and never commits. What it hands
back is this directory: a report of what happened and, if a repair was
verified, a diff the human reviews and applies themselves.

Layout: <output_dir>/<run_id>/report.md, plus repair.diff when a patch was
verified. The diff's a/ b/ paths are relative to the app root, so
`git apply` from that root applies it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from play_agent.orchestrator.driver import DriverResult
from play_agent.orchestrator.states import Phase
from play_agent.store.models import Attempt

DIFF_FILENAME = "repair.diff"
REPORT_FILENAME = "report.md"


@dataclass(frozen=True)
class RunArtifacts:
    directory: Path
    report_path: Path
    diff_path: Path | None


def build_report(
    result: DriverResult,
    attempts: Sequence[Attempt],
    test_file_path: str,
    test_title: str,
) -> str:
    succeeded = result.final_phase == Phase.SUCCEEDED
    lines = [
        "# play-agent repair report",
        "",
        f"- **Run:** `{result.run_id}`",
        f"- **Outcome:** {result.final_phase.value}",
        f"- **Test:** `{test_file_path}` - \"{test_title}\"",
        f"- **Patches applied during the run:** {result.attempts_made}",
    ]
    if result.rejection_reason:
        lines.append(f"- **Reason:** {result.rejection_reason}")

    lines += ["", "## Result", ""]
    if succeeded and result.diff:
        lines += [
            f"A repair was verified: `{_cell(result.applied_expression or '')}`",
            "",
            (
                "**Your test file has not been modified and nothing has been committed.** "
                f"Review `{DIFF_FILENAME}`, then apply it yourself from the root of your app:"
            ),
            "",
            "```bash",
            f"git apply {DIFF_FILENAME}",
            "```",
        ]
    elif succeeded:
        lines.append("The run reported success but produced no diff; treat it as unverified.")
    else:
        lines.append("No repair was verified. Your test file has not been modified.")

    lines += ["", "## Attempts", ""]
    if attempts:
        lines += [
            "| # | Locator call | Proposed expression | Verification |",
            "|---|---|---|---|",
        ]
        for attempt in attempts:
            outcome = attempt.verification_outcome
            outcome_text = getattr(outcome, "value", outcome) or "-"
            reps = f" ({attempt.verification_reps} reps)" if attempt.verification_reps else ""
            lines.append(
                f"| {attempt.candidate_rank} | `{attempt.patch_locator_call_id or '-'}` | "
                f"`{_cell(attempt.patch_new_locator_expr or '-')}` | {outcome_text}{reps} |"
            )
    else:
        lines.append("No candidate reached verification.")

    if result.diff:
        lines += ["", "## Diff", "", "```diff", result.diff.rstrip("\n"), "```"]

    return "\n".join(lines) + "\n"


def write_run_artifacts(
    output_dir: str | Path,
    result: DriverResult,
    attempts: Sequence[Attempt],
    test_file_path: str,
    test_title: str,
) -> RunArtifacts:
    directory = Path(output_dir) / result.run_id
    directory.mkdir(parents=True, exist_ok=True)

    diff_path: Path | None = None
    if result.final_phase == Phase.SUCCEEDED and result.diff:
        diff_path = directory / DIFF_FILENAME
        diff_path.write_text(result.diff)

    report_path = directory / REPORT_FILENAME
    report_path.write_text(build_report(result, attempts, test_file_path, test_title))
    return RunArtifacts(directory=directory, report_path=report_path, diff_path=diff_path)


def _cell(text: str) -> str:
    """Make text safe inside a markdown table cell or inline code span."""

    return text.replace("|", "\\|").replace("`", "'").replace("\n", " ")

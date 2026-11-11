"""CLI entry point: `play-agent repair <test-file> <test-title>`.

Wires Config -> Store -> SandboxedToolBelt -> GroqDiagnoser -> Driver,
which is the one assembly of all these pieces that previously existed
nowhere in the codebase (driver.py's own tests only ever used
FakeToolBelt). This is deliberately thin -- argument parsing and
object construction only, no logic that belongs in the pieces it wires
together.
"""

from __future__ import annotations

import sys

import click

from play_agent.config import Config, ConfigError
from play_agent.observability.logging import RunLogger
from play_agent.orchestrator.driver import Driver, DriverResult
from play_agent.orchestrator.groq_diagnoser import GroqDiagnoser
from play_agent.orchestrator.states import Phase
from play_agent.sandbox.container import SandboxConfig, SandboxError
from play_agent.store.store import Store
from play_agent.tools.sandboxed_tool_belt import SandboxedToolBelt, SandboxedToolBeltConfig


@click.group()
def cli() -> None:
    """play-agent: a self-healing locator repair agent for Playwright tests."""


@cli.command()
@click.argument("test_file_path", type=click.Path(exists=True))
@click.argument("test_title")
@click.option(
    "--reps",
    default=3,
    show_default=True,
    help="Number of verification reps run before trusting a fix as non-flaky.",
)
@click.option(
    "--log-path",
    default=None,
    help="Path to append structured JSON run logs to (Sec17). Defaults to "
    "PLAY_AGENT_LOG_PATH, or play_agent.log if that's unset too.",
)
def repair(test_file_path: str, test_title: str, reps: int, log_path: str | None) -> None:
    """Attempt to repair a failing test's stale locator.

    TEST_FILE_PATH is the path to the Playwright spec file containing
    the failing test. TEST_TITLE is that test's title exactly as it
    appears in Playwright's own output (used for --grep matching), so
    quote it if it contains spaces.
    """

    try:
        config = Config.from_env()
    except ConfigError as e:
        click.echo(f"Configuration error: {e}", err=True)
        sys.exit(1)

    sandbox_config = SandboxConfig(
        image=config.sandbox_image,
        app_path=config.app_under_test_path,
        app_host=config.app_under_test_host,
        app_port=config.app_under_test_port,
    )

    store = Store(config.db_path)
    diagnoser = GroqDiagnoser(api_key=config.groq_api_key, model=config.groq_model)
    try:
        # "pending" is corrected to the real run_id as Driver.run()'s
        # first action (RunLogger.set_run_id) -- the log file must be
        # opened before that id exists, since the id itself is minted
        # inside the call this logger needs to observe.
        with RunLogger.to_file("pending", log_path or config.log_path) as logger, SandboxedToolBelt(
            SandboxedToolBeltConfig(sandbox_config=sandbox_config, verification_reps=reps)
        ) as tools:
            driver = Driver(store=store, tools=tools, diagnoser=diagnoser, logger=logger)
            result = driver.run(test_file_path, test_title)
    except SandboxError as e:
        click.echo(f"Sandbox error: {e}", err=True)
        sys.exit(1)

    _report_result(result)
    sys.exit(0 if result.final_phase == Phase.SUCCEEDED else 1)


def _report_result(result: DriverResult) -> None:
    click.echo(f"Run {result.run_id}: {result.final_phase.value}")
    click.echo(f"Attempts made: {result.attempts_made}")
    if result.rejection_reason:
        click.echo(f"Rejection reason: {result.rejection_reason}")

    if result.final_phase == Phase.SUCCEEDED:
        click.echo("\nRepair succeeded. Review the diff before committing -- this tool never commits for you.")
    else:
        click.echo("\nNo repair was applied. See the rejection reason above, or re-run with more detail once logging (M5) lands.")


if __name__ == "__main__":
    cli()

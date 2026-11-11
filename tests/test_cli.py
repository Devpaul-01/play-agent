"""Tests for the CLI entry point (play-agent repair).

Mocks Store, SandboxedToolBelt, GroqDiagnoser, and Driver.run itself --
this confirms the CLI wires config into the right constructors and
reports/exits correctly on each Driver outcome, not that a real repair
pipeline works end-to-end (that needs Docker, a real Groq key, and a
fixture app, none of which belong in a unit test).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from play_agent.cli.main import cli
from play_agent.orchestrator.driver import DriverResult
from play_agent.orchestrator.states import Phase
from play_agent.sandbox.container import SandboxError

REQUIRED_ENV = {
    "PLAY_AGENT_GROQ_API_KEY": "test-key",
    "PLAY_AGENT_APP_PATH": "/tmp/fixture-app",
    "PLAY_AGENT_APP_HOST": "localhost",
}


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def spec_file(tmp_path):
    f = tmp_path / "checkout.spec.ts"
    f.write_text("// a test file")
    return f


@pytest.fixture
def log_path(tmp_path):
    # Every invocation below passes --log-path explicitly pointed at
    # tmp_path -- without it, Config's log_path default (play_agent.log,
    # relative to cwd) would write into wherever pytest happens to run
    # from, i.e. the repo itself.
    return str(tmp_path / "test.log")


def test_repair_missing_config_exits_nonzero_with_message(runner, spec_file):
    # No env vars set -- Config.from_env should fail before anything else
    # is constructed.
    result = runner.invoke(cli, ["repair", str(spec_file), "completes checkout"], env={})

    assert result.exit_code == 1
    assert "Configuration error" in result.output


def test_repair_nonexistent_test_file_rejected_by_click(runner, log_path):
    result = runner.invoke(
        cli,
        ["repair", "/nonexistent/file.spec.ts", "some test", "--log-path", log_path],
        env=REQUIRED_ENV,
    )

    assert result.exit_code != 0


@patch("play_agent.cli.main.Driver")
@patch("play_agent.cli.main.SandboxedToolBelt")
@patch("play_agent.cli.main.GroqDiagnoser")
@patch("play_agent.cli.main.Store")
def test_repair_succeeded_exits_zero(
    mock_store_cls, mock_diagnoser_cls, mock_belt_cls, mock_driver_cls, runner, spec_file, log_path
):
    mock_belt_cls.return_value.__enter__.return_value = MagicMock()
    mock_driver_cls.return_value.run.return_value = DriverResult(
        run_id="abc123", final_phase=Phase.SUCCEEDED, rejection_reason=None, attempts_made=1
    )

    result = runner.invoke(
        cli,
        ["repair", str(spec_file), "completes checkout", "--log-path", log_path],
        env=REQUIRED_ENV,
    )

    assert result.exit_code == 0
    assert "succeeded" in result.output
    assert "abc123" in result.output


@patch("play_agent.cli.main.Driver")
@patch("play_agent.cli.main.SandboxedToolBelt")
@patch("play_agent.cli.main.GroqDiagnoser")
@patch("play_agent.cli.main.Store")
def test_repair_failed_exits_nonzero(
    mock_store_cls, mock_diagnoser_cls, mock_belt_cls, mock_driver_cls, runner, spec_file, log_path
):
    mock_belt_cls.return_value.__enter__.return_value = MagicMock()
    mock_driver_cls.return_value.run.return_value = DriverResult(
        run_id="abc123", final_phase=Phase.FAILED, rejection_reason=None, attempts_made=3
    )

    result = runner.invoke(
        cli,
        ["repair", str(spec_file), "completes checkout", "--log-path", log_path],
        env=REQUIRED_ENV,
    )

    assert result.exit_code == 1


@patch("play_agent.cli.main.Driver")
@patch("play_agent.cli.main.SandboxedToolBelt")
@patch("play_agent.cli.main.GroqDiagnoser")
@patch("play_agent.cli.main.Store")
def test_repair_rejected_reports_reason(
    mock_store_cls, mock_diagnoser_cls, mock_belt_cls, mock_driver_cls, runner, spec_file, log_path
):
    mock_belt_cls.return_value.__enter__.return_value = MagicMock()
    mock_driver_cls.return_value.run.return_value = DriverResult(
        run_id="abc123",
        final_phase=Phase.REJECTED,
        rejection_reason="locator uses a disallowed method",
        attempts_made=1,
    )

    result = runner.invoke(
        cli,
        ["repair", str(spec_file), "completes checkout", "--log-path", log_path],
        env=REQUIRED_ENV,
    )

    assert result.exit_code == 1
    assert "disallowed method" in result.output


@patch("play_agent.cli.main.SandboxedToolBelt")
@patch("play_agent.cli.main.GroqDiagnoser")
@patch("play_agent.cli.main.Store")
def test_repair_sandbox_error_exits_nonzero_with_message(
    mock_store_cls, mock_diagnoser_cls, mock_belt_cls, runner, spec_file, log_path
):
    mock_belt_cls.return_value.__enter__.side_effect = SandboxError("Docker daemon not reachable")

    result = runner.invoke(
        cli,
        ["repair", str(spec_file), "completes checkout", "--log-path", log_path],
        env=REQUIRED_ENV,
    )

    assert result.exit_code == 1
    assert "Sandbox error" in result.output


@patch("play_agent.cli.main.Driver")
@patch("play_agent.cli.main.SandboxedToolBelt")
@patch("play_agent.cli.main.GroqDiagnoser")
@patch("play_agent.cli.main.Store")
def test_repair_reps_option_passed_through_to_belt_config(
    mock_store_cls, mock_diagnoser_cls, mock_belt_cls, mock_driver_cls, runner, spec_file, log_path
):
    mock_belt_cls.return_value.__enter__.return_value = MagicMock()
    mock_driver_cls.return_value.run.return_value = DriverResult(
        run_id="abc123", final_phase=Phase.SUCCEEDED, rejection_reason=None, attempts_made=1
    )

    runner.invoke(
        cli,
        ["repair", str(spec_file), "completes checkout", "--reps", "5", "--log-path", log_path],
        env=REQUIRED_ENV,
    )

    belt_config = mock_belt_cls.call_args.args[0]
    assert belt_config.verification_reps == 5


@patch("play_agent.cli.main.Driver")
@patch("play_agent.cli.main.SandboxedToolBelt")
@patch("play_agent.cli.main.GroqDiagnoser")
@patch("play_agent.cli.main.Store")
def test_repair_writes_log_file_at_given_path(
    mock_store_cls, mock_diagnoser_cls, mock_belt_cls, mock_driver_cls, runner, spec_file, log_path
):
    import json
    from pathlib import Path

    mock_belt_cls.return_value.__enter__.return_value = MagicMock()
    mock_driver_cls.return_value.run.return_value = DriverResult(
        run_id="abc123", final_phase=Phase.SUCCEEDED, rejection_reason=None, attempts_made=1
    )

    runner.invoke(
        cli,
        ["repair", str(spec_file), "completes checkout", "--log-path", log_path],
        env=REQUIRED_ENV,
    )

    # Driver itself is mocked here, so no real transitions happen --
    # this only confirms the CLI opens the log file at the path given
    # (an empty, valid file is still proof the path/open logic works;
    # test_driver.py's own logger tests cover line content).
    assert Path(log_path).exists()
    content = Path(log_path).read_text()
    for line in content.splitlines():
        if line.strip():
            json.loads(line)  # must be valid JSON if anything was written

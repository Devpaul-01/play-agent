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


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path, monkeypatch):
    # The CLI's default output directory is relative to the cwd; run every
    # test from a scratch directory so none can write into the repo.
    monkeypatch.chdir(tmp_path)


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


DIFF = "--- a/tests/checkout.spec.ts\n+++ b/tests/checkout.spec.ts\n@@ -1 +1 @@\n-old\n+new\n"


def _succeeded_with_diff():
    return DriverResult(
        run_id="abc123",
        final_phase=Phase.SUCCEEDED,
        rejection_reason=None,
        attempts_made=1,
        diff=DIFF,
        applied_expression='page.getByRole("button")',
    )


@patch("play_agent.cli.main.Driver")
@patch("play_agent.cli.main.SandboxedToolBelt")
@patch("play_agent.cli.main.GroqDiagnoser")
@patch("play_agent.cli.main.Store")
def test_success_prints_the_diff_and_says_the_file_is_untouched(
    mock_store_cls, mock_diagnoser_cls, mock_belt_cls, mock_driver_cls, runner, spec_file, log_path
):
    mock_belt_cls.return_value.__enter__.return_value = MagicMock()
    mock_driver_cls.return_value.run.return_value = _succeeded_with_diff()

    result = runner.invoke(
        cli,
        ["repair", str(spec_file), "completes checkout", "--log-path", log_path],
        env=REQUIRED_ENV,
    )

    assert result.exit_code == 0
    assert "has NOT been modified" in result.output
    assert "+new" in result.output  # the diff itself is shown
    assert "git apply" in result.output
    assert 'page.getByRole("button")' in result.output


@patch("play_agent.cli.main.Driver")
@patch("play_agent.cli.main.SandboxedToolBelt")
@patch("play_agent.cli.main.GroqDiagnoser")
@patch("play_agent.cli.main.Store")
def test_success_writes_the_diff_file_and_report(
    mock_store_cls, mock_diagnoser_cls, mock_belt_cls, mock_driver_cls, runner, spec_file, log_path, tmp_path
):
    mock_belt_cls.return_value.__enter__.return_value = MagicMock()
    mock_driver_cls.return_value.run.return_value = _succeeded_with_diff()
    out = tmp_path / "out"

    runner.invoke(
        cli,
        ["repair", str(spec_file), "completes checkout", "--log-path", log_path, "--output-dir", str(out)],
        env=REQUIRED_ENV,
    )

    assert (out / "abc123" / "repair.diff").read_text() == DIFF
    assert "# play-agent repair report" in (out / "abc123" / "report.md").read_text()


@patch("play_agent.cli.main.Driver")
@patch("play_agent.cli.main.SandboxedToolBelt")
@patch("play_agent.cli.main.GroqDiagnoser")
@patch("play_agent.cli.main.Store")
def test_failed_run_writes_a_report_but_no_diff_and_prints_the_reason(
    mock_store_cls, mock_diagnoser_cls, mock_belt_cls, mock_driver_cls, runner, spec_file, log_path, tmp_path
):
    mock_belt_cls.return_value.__enter__.return_value = MagicMock()
    mock_driver_cls.return_value.run.return_value = DriverResult(
        run_id="abc123",
        final_phase=Phase.FAILED,
        rejection_reason="ToolError: no Playwright report found",
        attempts_made=0,
    )
    out = tmp_path / "out"

    result = runner.invoke(
        cli,
        ["repair", str(spec_file), "completes checkout", "--log-path", log_path, "--output-dir", str(out)],
        env=REQUIRED_ENV,
    )

    assert result.exit_code == 1
    assert "no Playwright report found" in result.output
    assert "Your test file has not been modified" in result.output
    assert (out / "abc123" / "report.md").exists()
    assert not (out / "abc123" / "repair.diff").exists()


@patch("play_agent.cli.main.Driver")
@patch("play_agent.cli.main.SandboxedToolBelt")
@patch("play_agent.cli.main.GroqDiagnoser")
@patch("play_agent.cli.main.Store")
def test_output_dir_defaults_to_the_config_value(
    mock_store_cls, mock_diagnoser_cls, mock_belt_cls, mock_driver_cls, runner, spec_file, log_path, tmp_path
):
    mock_belt_cls.return_value.__enter__.return_value = MagicMock()
    mock_driver_cls.return_value.run.return_value = _succeeded_with_diff()
    configured = tmp_path / "configured-out"

    runner.invoke(
        cli,
        ["repair", str(spec_file), "completes checkout", "--log-path", log_path],
        env={**REQUIRED_ENV, "PLAY_AGENT_OUTPUT_DIR": str(configured)},
    )

    assert (configured / "abc123" / "repair.diff").exists()


@patch("play_agent.cli.main.Driver")
@patch("play_agent.cli.main.SandboxedToolBelt")
@patch("play_agent.cli.main.GroqDiagnoser")
@patch("play_agent.cli.main.Store")
def test_probe_dom_flag_is_passed_to_the_driver(
    mock_store_cls, mock_diagnoser_cls, mock_belt_cls, mock_driver_cls, runner, spec_file, log_path
):
    mock_belt_cls.return_value.__enter__.return_value = MagicMock()
    mock_driver_cls.return_value.run.return_value = _succeeded_with_diff()

    runner.invoke(
        cli,
        ["repair", str(spec_file), "t", "--log-path", log_path, "--probe-dom"],
        env=REQUIRED_ENV,
    )
    assert mock_driver_cls.call_args.kwargs["probe_candidates"] is True

    runner.invoke(cli, ["repair", str(spec_file), "t", "--log-path", log_path], env=REQUIRED_ENV)
    assert mock_driver_cls.call_args.kwargs["probe_candidates"] is False

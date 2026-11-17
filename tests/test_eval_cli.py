"""Tests for the `play-agent eval` commands and comparison formatting.

The benchmark's pipeline (Docker + Groq) is mocked; these confirm the
wiring, flags, exit codes and rendering, not a real evaluation run.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from play_agent.cli.main import cli
from play_agent.eval.adversarial import AdversarialResult, AdversarialScenario, TrustBoundaryLayer
from play_agent.eval.formatting import comparison_to_dict, format_comparison
from play_agent.eval.metrics import BenchmarkOutcome, compute_metrics
from play_agent.eval.mutations import Mutation, MutationType
from play_agent.eval.runner import ComparisonReport, ScenarioRunResult
from play_agent.orchestrator.states import Phase

ENV = {
    "PLAY_AGENT_GROQ_API_KEY": "test-key",
    "PLAY_AGENT_APP_PATH": "/tmp/fixture-app",
    "PLAY_AGENT_APP_HOST": "localhost",
    "PLAY_AGENT_GROQ_MODEL": "default-model",
}

RENAME = Mutation(
    mutation_type=MutationType.ATTRIBUTE_RENAME,
    component_file="pages/LoginPage.tsx",
    original_testid="login-username",
    correct_repair_testid="username-field",
    description="t",
)


@pytest.fixture
def runner():
    return CliRunner()


def _report(outcomes=(), errors=()):
    results = [ScenarioRunResult(mutation=o.mutation, outcome=o) for o in outcomes]
    results += [ScenarioRunResult(mutation=RENAME, outcome=None, error=e) for e in errors]
    return ComparisonReport(metrics=compute_metrics(list(outcomes)), scenario_results=tuple(results))


def _succeeded_correct():
    from play_agent.store.models import Attempt, AttemptKind, VerificationOutcome

    return BenchmarkOutcome(
        mutation=RENAME,
        final_phase=Phase.SUCCEEDED,
        attempts=(
            Attempt(
                run_id="r",
                candidate_rank=1,
                attempt_kind=AttemptKind.CANDIDATE,
                verification_outcome=VerificationOutcome.PASS,
                patch_new_locator_expr='page.getByTestId("username-field")',
            ),
        ),
    )


# -- eval adversarial ----------------------------------------------------------


def test_adversarial_runs_for_real_and_reports_every_attack_blocked(runner):
    result = runner.invoke(cli, ["eval", "adversarial"])

    assert result.exit_code == 0
    assert "6/6 attacks blocked." in result.output
    assert "prompt_injection_via_expression_text" in result.output


def test_adversarial_exits_nonzero_when_an_attack_is_not_blocked(runner):
    scenario = AdversarialScenario(
        name="leaky_scenario",
        attack_type="x",
        description="d",
        test_source="",
        locator_call_id="L1C1",
        malicious_expression="x",
        expected_blocking_layer=TrustBoundaryLayer.ORCHESTRATOR,
    )
    leaked = AdversarialResult(
        scenario=scenario, blocked=False, blocking_layer=None, validation_reason=None
    )

    with patch("play_agent.cli.evaluate.run_adversarial_suite", return_value=[leaked]):
        result = runner.invoke(cli, ["eval", "adversarial"])

    assert result.exit_code == 1
    assert "NOT BLOCKED: leaky_scenario" in result.output


# -- eval benchmark ------------------------------------------------------------


def test_benchmark_missing_config_exits_nonzero(runner):
    result = runner.invoke(cli, ["eval", "benchmark"], env={})

    assert result.exit_code == 1
    assert "Configuration error" in result.output


def test_benchmark_rejects_a_duplicated_model(runner):
    result = runner.invoke(
        cli, ["eval", "benchmark", "--model", "a", "--model", "a"], env=ENV
    )

    assert result.exit_code == 1
    assert "only once" in result.output


@patch("play_agent.cli.evaluate.run_comparison_with_details")
def test_benchmark_builds_one_configuration_per_model_and_passes_settings(mock_run, runner):
    mock_run.return_value = {
        "a": _report([_succeeded_correct()]),
        "b": _report([_succeeded_correct()]),
    }

    result = runner.invoke(
        cli, ["eval", "benchmark", "--model", "a", "--model", "b", "--reps", "5"], env=ENV
    )

    assert result.exit_code == 0
    runner_config, configurations, _work_dir = mock_run.call_args.args
    assert [c.name for c in configurations] == ["a", "b"]
    assert runner_config.fixture_app_root == "/tmp/fixture-app"
    assert runner_config.verification_reps == 5
    assert "Configuration" in result.output  # the comparison table was printed


@patch("play_agent.cli.evaluate.GroqDiagnoser")
@patch("play_agent.cli.evaluate.run_comparison_with_details")
def test_each_configurations_factory_builds_a_diagnoser_for_its_own_model(
    mock_run, mock_diagnoser_cls, runner
):
    mock_run.return_value = {"a": _report([_succeeded_correct()])}
    runner.invoke(cli, ["eval", "benchmark", "--model", "a"], env=ENV)

    configurations = mock_run.call_args.args[1]
    configurations[0].diagnoser_factory()

    mock_diagnoser_cls.assert_called_once_with(api_key="test-key", model="a")


@patch("play_agent.cli.evaluate.run_comparison_with_details")
def test_benchmark_defaults_to_the_configured_model_and_warns_about_a_single_configuration(
    mock_run, runner
):
    mock_run.return_value = {"default-model": _report([_succeeded_correct()])}

    result = runner.invoke(cli, ["eval", "benchmark"], env=ENV)

    assert [c.name for c in mock_run.call_args.args[1]] == ["default-model"]
    assert "at least two diagnosis configurations" in result.output


@patch("play_agent.cli.evaluate.run_comparison_with_details")
def test_benchmark_writes_json_output(mock_run, runner, tmp_path):
    mock_run.return_value = {"a": _report([_succeeded_correct()])}
    out = tmp_path / "results.json"

    runner.invoke(cli, ["eval", "benchmark", "--model", "a", "--output", str(out)], env=ENV)

    data = json.loads(out.read_text())
    assert data["a"]["metrics"]["success_rate"] == 1.0
    assert data["a"]["scenarios"][0]["final_phase"] == "succeeded"


@patch("play_agent.cli.evaluate.run_comparison_with_details")
def test_benchmark_that_measured_nothing_exits_nonzero(mock_run, runner):
    mock_run.return_value = {"a": _report(errors=["sandbox error: daemon down"])}

    result = runner.invoke(cli, ["eval", "benchmark", "--model", "a"], env=ENV)

    assert result.exit_code == 1
    assert "No scenario produced a result." in result.output
    assert "sandbox error: daemon down" in result.output  # the cause is shown, not hidden


@patch("play_agent.cli.evaluate.run_comparison_with_details")
def test_benchmark_uses_and_keeps_an_explicit_work_dir(mock_run, runner, tmp_path):
    mock_run.return_value = {"a": _report([_succeeded_correct()])}
    work = tmp_path / "scratch"

    runner.invoke(cli, ["eval", "benchmark", "--model", "a", "--work-dir", str(work)], env=ENV)

    assert work.is_dir()
    assert str(mock_run.call_args.args[2]) == str(work)


# -- formatting ----------------------------------------------------------------


def test_format_marks_undefined_rates_na_instead_of_zero_percent():
    # No scenario succeeded, so a false-repair RATE is undefined, not 0%.
    failed = BenchmarkOutcome(mutation=RENAME, final_phase=Phase.FAILED, attempts=())

    table = format_comparison({"cfg": _report([failed])})

    assert "n/a" in table
    row = next(line for line in table.splitlines() if line.startswith("cfg"))
    assert "0%" in row  # success rate is a real 0%
    assert row.count("n/a") >= 3  # false-repair, correct-rejection, candidates/success, latency


def test_format_shows_real_rates_when_denominators_exist():
    table = format_comparison({"cfg": _report([_succeeded_correct()])})

    row = next(line for line in table.splitlines() if line.startswith("cfg"))
    assert "100%" in row


def test_format_lists_scenarios_that_did_not_run_with_their_reason():
    table = format_comparison({"cfg": _report([_succeeded_correct()], errors=["mutation did not apply"])})

    assert "[cfg] did not run" in table
    assert "mutation did not apply" in table
    assert "1/2" in table  # ran / attempted


def test_comparison_to_dict_is_json_serializable():
    data = comparison_to_dict({"cfg": _report([_succeeded_correct()], errors=["x"])})

    json.dumps(data)  # must not raise
    assert len(data["cfg"]["scenarios"]) == 2
    assert data["cfg"]["scenarios"][1]["error"] == "x"

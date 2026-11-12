"""Tests for eval.runner -- mocks SandboxedToolBelt, Store, and Driver
(the Docker/Groq-dependent pieces), confirming the orchestration logic
itself: scenario iteration, mutation application, error handling per
scenario, and per-configuration metrics aggregation.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from play_agent.eval.mutations import Mutation, MutationType
from play_agent.eval.runner import (
    DiagnosisConfiguration,
    RunnerConfig,
    run_comparison,
    run_scenarios_for_configuration,
)
from play_agent.orchestrator.driver import DriverResult
from play_agent.orchestrator.states import Phase

RUNNABLE_MUTATION = Mutation(
    mutation_type=MutationType.ATTRIBUTE_RENAME,
    component_file="pages/LoginPage.tsx",  # relative to frontend/src, per apply_mutation's contract
    original_testid="login-username",
    correct_repair_testid="username-field",
    test_file="auth.spec.ts",
    test_title="some test",
    description="test",
)

NOT_RUNNABLE_MUTATION = Mutation(
    mutation_type=MutationType.ATTRIBUTE_RENAME,
    component_file="pages/LoginPage.tsx",
    original_testid="login-username",
    correct_repair_testid="username-field",
    description="no test_file/test_title set",
)


@pytest.fixture
def fixture_app_root(tmp_path):
    root = tmp_path / "fixture-app"
    (root / "frontend" / "src" / "pages").mkdir(parents=True)
    (root / "frontend" / "src" / "pages" / "LoginPage.tsx").write_text(
        '<input data-testid="login-username" />'
    )
    (root / "tests").mkdir()
    (root / "tests" / "auth.spec.ts").write_text("// placeholder")
    return root


@pytest.fixture
def work_dir(tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    return d


def make_config(fixture_app_root, scenarios):
    return RunnerConfig(
        fixture_app_root=str(fixture_app_root),
        sandbox_image="fixture-app:test",
        scenarios=tuple(scenarios),
    )


def test_mutation_without_test_file_is_not_runnable(fixture_app_root, work_dir):
    config = make_config(fixture_app_root, [NOT_RUNNABLE_MUTATION])
    diagnosis_config = DiagnosisConfiguration(name="cfg-a", diagnoser_factory=MagicMock())

    results = run_scenarios_for_configuration(config, diagnosis_config, work_dir)

    assert len(results) == 1
    assert results[0].outcome is None
    assert "not runnable" in results[0].error


@patch("play_agent.eval.runner.Store")
@patch("play_agent.eval.runner.SandboxedToolBelt")
def test_successful_scenario_produces_an_outcome(
    mock_belt_cls, mock_store_cls, fixture_app_root, work_dir
):
    mock_belt_cls.return_value.__enter__.return_value = MagicMock()
    mock_store_instance = MagicMock()
    mock_store_instance.list_attempts.return_value = []
    mock_store_cls.return_value.__enter__.return_value = mock_store_instance

    with patch("play_agent.eval.runner.Driver") as mock_driver_cls:
        mock_driver_cls.return_value.run.return_value = DriverResult(
            run_id="run-1", final_phase=Phase.SUCCEEDED, rejection_reason=None, attempts_made=1
        )

        config = make_config(fixture_app_root, [RUNNABLE_MUTATION])
        diagnosis_config = DiagnosisConfiguration(name="cfg-a", diagnoser_factory=MagicMock())

        results = run_scenarios_for_configuration(config, diagnosis_config, work_dir)

    assert len(results) == 1
    assert results[0].error is None
    assert results[0].outcome is not None
    assert results[0].outcome.final_phase == Phase.SUCCEEDED


@patch("play_agent.eval.runner.Store")
@patch("play_agent.eval.runner.SandboxedToolBelt")
def test_mutation_apply_failure_recorded_as_error_not_raised(
    mock_belt_cls, mock_store_cls, fixture_app_root, work_dir
):
    # original_testid doesn't exist in the copied component file --
    # apply_mutation will fail; this must surface as a ScenarioRunResult
    # error, not propagate as an exception.
    bad_mutation = Mutation(
        mutation_type=MutationType.ATTRIBUTE_RENAME,
        component_file="pages/LoginPage.tsx",
        original_testid="does-not-exist",
        correct_repair_testid="whatever",
        test_file="auth.spec.ts",
        test_title="some test",
        description="test",
    )

    config = make_config(fixture_app_root, [bad_mutation])
    diagnosis_config = DiagnosisConfiguration(name="cfg-a", diagnoser_factory=MagicMock())

    results = run_scenarios_for_configuration(config, diagnosis_config, work_dir)

    assert len(results) == 1
    assert results[0].outcome is None
    assert "did not apply" in results[0].error
    mock_belt_cls.assert_not_called()  # never got far enough to construct a sandbox


@patch("play_agent.eval.runner.Store")
@patch("play_agent.eval.runner.SandboxedToolBelt")
def test_each_scenario_gets_its_own_mutated_copy(
    mock_belt_cls, mock_store_cls, fixture_app_root, work_dir
):
    # Two scenarios in the same configuration must not interfere --
    # each needs its own scratch directory, not a shared mutated copy.
    mock_belt_cls.return_value.__enter__.return_value = MagicMock()
    mock_store_instance = MagicMock()
    mock_store_instance.list_attempts.return_value = []
    mock_store_cls.return_value.__enter__.return_value = mock_store_instance

    with patch("play_agent.eval.runner.Driver") as mock_driver_cls:
        mock_driver_cls.return_value.run.return_value = DriverResult(
            run_id="run-1", final_phase=Phase.SUCCEEDED, rejection_reason=None, attempts_made=1
        )

        config = make_config(fixture_app_root, [RUNNABLE_MUTATION, RUNNABLE_MUTATION])
        diagnosis_config = DiagnosisConfiguration(name="cfg-a", diagnoser_factory=MagicMock())

        results = run_scenarios_for_configuration(config, diagnosis_config, work_dir)

    assert len(results) == 2
    assert all(r.outcome is not None for r in results)

    # Each scenario's SandboxConfig.app_path (first positional/keyword
    # arg into SandboxedToolBeltConfig via the mocked belt) must differ.
    app_paths = {
        call.args[0].sandbox_config.app_path for call in mock_belt_cls.call_args_list
    }
    assert len(app_paths) == 2


@patch("play_agent.eval.runner.Store")
@patch("play_agent.eval.runner.SandboxedToolBelt")
def test_run_comparison_computes_metrics_per_configuration(
    mock_belt_cls, mock_store_cls, fixture_app_root, work_dir
):
    mock_belt_cls.return_value.__enter__.return_value = MagicMock()
    mock_store_instance = MagicMock()
    mock_store_instance.list_attempts.return_value = []
    mock_store_cls.return_value.__enter__.return_value = mock_store_instance

    with patch("play_agent.eval.runner.Driver") as mock_driver_cls:
        mock_driver_cls.return_value.run.return_value = DriverResult(
            run_id="run-1", final_phase=Phase.SUCCEEDED, rejection_reason=None, attempts_made=1
        )

        config = make_config(fixture_app_root, [RUNNABLE_MUTATION])
        configs = [
            DiagnosisConfiguration(name="model-a", diagnoser_factory=MagicMock()),
            DiagnosisConfiguration(name="model-b", diagnoser_factory=MagicMock()),
        ]

        results = run_comparison(config, configs, work_dir)

    assert set(results.keys()) == {"model-a", "model-b"}
    assert results["model-a"].success_rate == 1.0
    assert results["model-b"].success_rate == 1.0


@patch("play_agent.eval.runner.Store")
@patch("play_agent.eval.runner.SandboxedToolBelt")
def test_sandbox_error_recorded_as_scenario_error_not_raised(
    mock_belt_cls, mock_store_cls, fixture_app_root, work_dir
):
    from play_agent.sandbox.container import SandboxError

    mock_belt_cls.return_value.__enter__.side_effect = SandboxError("daemon unreachable")

    config = make_config(fixture_app_root, [RUNNABLE_MUTATION])
    diagnosis_config = DiagnosisConfiguration(name="cfg-a", diagnoser_factory=MagicMock())

    results = run_scenarios_for_configuration(config, diagnosis_config, work_dir)

    assert len(results) == 1
    assert results[0].outcome is None
    assert "sandbox error" in results[0].error

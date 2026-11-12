"""The comparison runner (architecture Sec16's "comparative evaluation"):
drives the real Driver through every BENCHMARK_SCENARIOS entry, once
per diagnosis configuration, and reports BenchmarkMetrics per
configuration -- "not a single aggregate number."

Per scenario, per configuration:
  1. Copy the WHOLE fixture app (not just frontend/src) to a scratch dir
     -- SandboxConfig.app_path mounts a whole app into the sandbox, so
     the mutation has to live inside a copy of that whole tree, not a
     standalone frontend/src copy.
  2. Apply the scenario's Mutation to the copy's component source.
  3. Point a fresh SandboxedToolBelt's SandboxConfig at the mutated
     copy, with component_src_root also pointed at the mutated
     frontend/src (M3's evidence should see the SAME mutated source the
     sandbox is running, not the pre-mutation original).
  4. Run the Driver (this configuration's Diagnoser, a fresh Store per
     scenario) against the scenario's real test_file/test_title.
  5. Record the result as a BenchmarkOutcome.

This module does NOT build the fixture app's containers or know how to
install its dependencies -- that's the fixture app's own concern
(README: "tests run locally and in a container"). DiagnoserFactory and
RunnerConfig below take an already-built sandbox image name; this
runner only varies the diagnoser and records outcomes.
"""

from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from play_agent.eval.benchmark import BENCHMARK_SCENARIOS
from play_agent.eval.metrics import BenchmarkMetrics, BenchmarkOutcome, compute_metrics
from play_agent.eval.mutations import Mutation, apply_mutation
from play_agent.orchestrator.driver import Diagnoser, Driver
from play_agent.sandbox.container import SandboxConfig, SandboxError
from play_agent.store.store import Store
from play_agent.tools.sandboxed_tool_belt import SandboxedToolBelt, SandboxedToolBeltConfig


class DiagnoserFactory(Protocol):
    """A diagnosis configuration: anything that can produce a fresh
    Diagnoser on demand. A fresh instance per scenario, not one shared
    instance, since GroqDiagnoser carries no scenario-specific state
    itself but a shared instance would make it easy to accidentally
    introduce some later (e.g. a cache) that leaks across scenarios."""

    def __call__(self) -> Diagnoser: ...


@dataclass(frozen=True)
class DiagnosisConfiguration:
    """One named entry in a comparative evaluation (Sec16: 'metrics
    reported per configuration')."""

    name: str
    diagnoser_factory: DiagnoserFactory


@dataclass(frozen=True)
class RunnerConfig:
    fixture_app_root: str  # the REAL, unmutated fixture app checkout
    sandbox_image: str
    app_host: str = "localhost"
    app_port: int = 3000
    verification_reps: int = 3
    scenarios: tuple[Mutation, ...] = BENCHMARK_SCENARIOS


@dataclass(frozen=True)
class ScenarioRunResult:
    """One scenario's raw result, before being folded into
    BenchmarkMetrics -- kept separate so a caller can inspect individual
    failures (which scenario, what error) rather than only ever seeing
    an aggregate."""

    mutation: Mutation
    outcome: BenchmarkOutcome | None  # None if the scenario couldn't even run
    error: str | None = None


def run_comparison(
    config: RunnerConfig, configurations: list[DiagnosisConfiguration], work_dir: str | Path
) -> dict[str, BenchmarkMetrics]:
    """Run every scenario in config.scenarios against every
    configuration, and return {configuration_name: BenchmarkMetrics}.

    Scenarios that error out (missing test_file/test_title, a mutation
    that fails to apply, a sandbox error) are recorded with error set
    and excluded from that configuration's metrics rather than crashing
    the whole comparison -- one bad scenario shouldn't block reporting
    on the rest, and Sec16's metrics are explicitly meant to be computed
    from real runs, not estimated around a partial failure.
    """

    results: dict[str, BenchmarkMetrics] = {}

    for configuration in configurations:
        scenario_results = run_scenarios_for_configuration(config, configuration, work_dir)
        outcomes = [r.outcome for r in scenario_results if r.outcome is not None]
        results[configuration.name] = compute_metrics(outcomes)

    return results


def run_scenarios_for_configuration(
    config: RunnerConfig, configuration: DiagnosisConfiguration, work_dir: str | Path
) -> list[ScenarioRunResult]:
    work_root = Path(work_dir)
    results: list[ScenarioRunResult] = []

    for mutation in config.scenarios:
        results.append(_run_one_scenario(config, configuration, mutation, work_root))

    return results


def _run_one_scenario(
    config: RunnerConfig,
    configuration: DiagnosisConfiguration,
    mutation: Mutation,
    work_root: Path,
) -> ScenarioRunResult:
    if mutation.test_file is None or mutation.test_title is None:
        return ScenarioRunResult(
            mutation=mutation,
            outcome=None,
            error="mutation has no test_file/test_title -- not runnable end-to-end",
        )

    scenario_dir = work_root / f"{configuration.name}-{uuid.uuid4().hex[:8]}"

    try:
        mutated_app_root = _prepare_mutated_app(config.fixture_app_root, scenario_dir, mutation)
    except (OSError, shutil.Error) as e:
        return ScenarioRunResult(mutation=mutation, outcome=None, error=f"setup failed: {e}")

    mutation_result = apply_mutation(
        mutation, Path(mutated_app_root) / "frontend" / "src"
    )
    if not mutation_result.applied:
        return ScenarioRunResult(
            mutation=mutation, outcome=None, error=f"mutation did not apply: {mutation_result.reason}"
        )

    test_file_path = str(Path(mutated_app_root) / "tests" / mutation.test_file)
    db_path = str(scenario_dir / "store.sqlite3")

    sandbox_config = SandboxConfig(
        image=config.sandbox_image,
        app_path=mutated_app_root,
        app_host=config.app_host,
        app_port=config.app_port,
    )
    belt_config = SandboxedToolBeltConfig(
        sandbox_config=sandbox_config,
        verification_reps=config.verification_reps,
        component_src_root=str(Path(mutated_app_root) / "frontend" / "src"),
    )

    try:
        with Store(db_path) as store, SandboxedToolBelt(belt_config) as tools:
            diagnoser = configuration.diagnoser_factory()
            driver = Driver(store=store, tools=tools, diagnoser=diagnoser)
            driver_result = driver.run(test_file_path, mutation.test_title)
            attempts = tuple(store.list_attempts(driver_result.run_id))
    except SandboxError as e:
        return ScenarioRunResult(mutation=mutation, outcome=None, error=f"sandbox error: {e}")

    outcome = BenchmarkOutcome(
        mutation=mutation, final_phase=driver_result.final_phase, attempts=attempts
    )
    return ScenarioRunResult(mutation=mutation, outcome=outcome, error=None)


def _prepare_mutated_app(fixture_app_root: str, scenario_dir: Path, mutation: Mutation) -> str:
    """Copy the WHOLE fixture app (not just frontend/src) to
    scenario_dir, so the mutated component source lives inside a
    complete app the sandbox can actually run -- mirrors
    prepare_mutated_copy's "never touch the original" contract, just
    over the whole app tree rather than one directory within it.
    """

    shutil.copytree(fixture_app_root, scenario_dir)
    return str(scenario_dir)

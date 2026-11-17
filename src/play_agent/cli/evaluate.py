"""`play-agent eval ...`: run the Sec16 evaluation harness from the CLI.

  eval adversarial   the named adversarial suite. Needs no Docker or API key.
  eval benchmark     the comparative benchmark: every scenario, once per
                     model, through the REAL pipeline (Docker + Groq).
"""

from __future__ import annotations

import json
import sys
import tempfile
from functools import partial
from pathlib import Path

import click

from play_agent.config import Config, ConfigError
from play_agent.eval.adversarial import format_results_table, run_adversarial_suite
from play_agent.eval.benchmark import BENCHMARK_SCENARIOS
from play_agent.eval.formatting import comparison_to_dict, format_comparison
from play_agent.eval.runner import (
    DiagnosisConfiguration,
    RunnerConfig,
    run_comparison_with_details,
)
from play_agent.orchestrator.groq_diagnoser import GroqDiagnoser


@click.group(name="eval")
def eval_group() -> None:
    """Run the evaluation harness (architecture Sec16)."""


@eval_group.command()
def adversarial() -> None:
    """Run the adversarial suite and print the attack -> blocked -> layer table.

    Exit code 1 if any attack was NOT blocked. Scope: it feeds attacker-chosen
    payloads to the orchestrator's validation layer (Sec8 layer 2); it does not
    test whether injected page content can steer a real model.
    """

    results = run_adversarial_suite()
    click.echo(format_results_table(results))

    not_blocked = [r for r in results if not r.blocked]
    blocked = len(results) - len(not_blocked)
    click.echo(f"\n{blocked}/{len(results)} attacks blocked.")
    if not_blocked:
        for r in not_blocked:
            click.echo(f"NOT BLOCKED: {r.scenario.name}", err=True)
        sys.exit(1)


@eval_group.command()
@click.option(
    "--model",
    "models",
    multiple=True,
    help="A Groq model to evaluate; repeat to compare several (Sec16 asks for at "
    "least two). Defaults to PLAY_AGENT_GROQ_MODEL.",
)
@click.option("--reps", default=3, show_default=True, help="Verification reps per scenario.")
@click.option(
    "--work-dir",
    default=None,
    help="Scratch directory for per-scenario app copies. Defaults to a temporary "
    "directory that is deleted afterwards.",
)
@click.option("--output", default=None, help="Also write the full results as JSON to this file.")
def benchmark(models: tuple[str, ...], reps: int, work_dir: str | None, output: str | None) -> None:
    """Run every benchmark scenario against each model and compare them.

    This runs the REAL pipeline: each scenario copies the fixture app and uses
    Docker and Groq, so it takes time and spends API credit. The app is read
    from PLAY_AGENT_APP_PATH (the fixture app checkout); the other settings come
    from the usual PLAY_AGENT_* environment variables.
    """

    try:
        config = Config.from_env()
    except ConfigError as e:
        click.echo(f"Configuration error: {e}", err=True)
        sys.exit(1)

    chosen = list(models) or [config.groq_model]
    if len(set(chosen)) != len(chosen):
        click.echo("Each --model may be given only once.", err=True)
        sys.exit(1)
    if len(chosen) < 2:
        click.echo(
            "Note: Sec16 asks for at least two diagnosis configurations; "
            "this will report a single one.",
            err=True,
        )

    configurations = [
        DiagnosisConfiguration(
            name=model,
            diagnoser_factory=partial(GroqDiagnoser, api_key=config.groq_api_key, model=model),
        )
        for model in chosen
    ]
    runner_config = RunnerConfig(
        fixture_app_root=config.app_under_test_path,
        sandbox_image=config.sandbox_image,
        app_host=config.app_under_test_host,
        app_port=config.app_under_test_port,
        verification_reps=reps,
    )

    click.echo(
        f"Running {len(BENCHMARK_SCENARIOS)} scenarios x {len(chosen)} model(s) through the "
        "real pipeline (Docker + Groq)...",
        err=True,
    )

    if work_dir is None:
        with tempfile.TemporaryDirectory(prefix="play-agent-eval-") as scratch:
            reports = run_comparison_with_details(runner_config, configurations, scratch)
    else:
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        reports = run_comparison_with_details(runner_config, configurations, work_dir)

    click.echo(format_comparison(reports))

    if output:
        Path(output).write_text(json.dumps(comparison_to_dict(reports), indent=2))
        click.echo(f"\nFull results written to {output}")

    # Nothing measured at all is a failed evaluation, not an empty success.
    if all(report.metrics.scenario_count == 0 for report in reports.values()):
        click.echo("No scenario produced a result.", err=True)
        sys.exit(1)

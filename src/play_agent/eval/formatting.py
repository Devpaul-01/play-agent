"""Human- and machine-readable rendering of a comparison (Sec16: "metrics
reported per configuration -- not a single aggregate number")."""

from __future__ import annotations

from dataclasses import asdict

from play_agent.eval.runner import ComparisonReport
from play_agent.orchestrator.states import Phase

_NA = "n/a"


def format_comparison(reports: dict[str, ComparisonReport]) -> str:
    header = (
        f"{'Configuration':<28} {'Ran':>7} {'Success':>8} {'FalseRepair':>12} "
        f"{'CorrectRej':>11} {'Cand/succ':>10} {'ReDiag':>7} {'Cost($)':>9} {'Latency(ms)':>12}"
    )
    lines = [header, "-" * len(header)]

    for name, report in reports.items():
        m = report.metrics
        outcomes = [r.outcome for r in report.scenario_results if r.outcome is not None]
        succeeded = sum(1 for o in outcomes if o.final_phase == Phase.SUCCEEDED)
        expecting_rejection = sum(1 for o in outcomes if o.mutation.correct_repair_testid is None)
        total = len(report.scenario_results)

        # A rate over an empty denominator is not "0%", it is undefined:
        # false-repair is over SUCCESSFUL repairs, correct-rejection over
        # scenarios that should be rejected.
        false_repair = _pct(m.false_repair_rate) if succeeded else _NA
        correct_rej = _pct(m.correct_rejection_rate) if expecting_rejection else _NA

        lines.append(
            f"{name:<28} {f'{m.scenario_count}/{total}':>7} {_pct(m.success_rate):>8} "
            f"{false_repair:>12} {correct_rej:>11} "
            f"{_num(m.candidates_tried_per_success, '{:.1f}'):>10} {_pct(m.re_diagnosis_rate):>7} "
            f"{m.total_cost_usd:>9.4f} {_num(m.mean_latency_ms, '{:.0f}'):>12}"
        )

    lines += [
        "",
        (
            "Ran = scenarios that produced a result / scenarios attempted. Rates are over "
            "scenarios that ran; n/a means the denominator is empty."
        ),
    ]

    for name, report in reports.items():
        for scenario in report.errors:
            m = scenario.mutation
            lines.append(
                f"  [{name}] did not run: {m.component_file} / {m.original_testid}: {scenario.error}"
            )

    return "\n".join(lines)


def comparison_to_dict(reports: dict[str, ComparisonReport]) -> dict:
    result: dict = {}
    for name, report in reports.items():
        result[name] = {
            "metrics": asdict(report.metrics),
            "scenarios": [
                {
                    "mutation_type": r.mutation.mutation_type.value,
                    "component_file": r.mutation.component_file,
                    "original_testid": r.mutation.original_testid,
                    "final_phase": r.outcome.final_phase.value if r.outcome else None,
                    "error": r.error,
                }
                for r in report.scenario_results
            ],
        }
    return result


def _pct(value: float) -> str:
    return f"{value * 100:.0f}%"


def _num(value: float | None, fmt: str) -> str:
    return _NA if value is None else fmt.format(value)

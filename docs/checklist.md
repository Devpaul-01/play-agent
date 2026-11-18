# Implementation Checklist

Tracks progress against the milestones in `docs/architecture.md` Sec23.
Update this as tasks complete -- it's the source of truth for "what's
actually done" vs. "what's planned."

**Legend:** `[x]` done and tested, `[~]` implemented but with a named gap
(see the note beside it), `[ ]` not done.

**Honest headline:** every component below exists and is covered by unit
tests (275 passing), but **the full pipeline has never been run for real**
-- no run against a live Docker daemon plus a live Groq key has happened
yet. "Done" below means "implemented and unit-tested", not "proven
end-to-end". See the "Not yet proven" section at the bottom.

## M1 -- Deterministic skeleton

Acceptance: a hard-coded "patch" flows end-to-end through apply -> verify
-> accept/reject with full `runs`/`attempts` records.

- [x] Repo scaffolded (`pyproject.toml`, package layout, config loading)
- [x] SQLite store: `runs` + `attempts` tables, unit-tested directly
- [x] State machine (Sec6 transitions), pure and I/O-free, unit-tested
- [x] Tool contracts (Sec7): `collect_failure_evidence`, `apply_patch`,
      `run_verification` implemented. `probe_live_dom` is `[~]` -- see M3.
      `collect_failure_evidence` is checked against a genuine Playwright
      JSON report (and strips its ANSI colour codes).
- [x] Isolation (Sec7): patches go to a scratch copy of the app; the real
      test file is never modified (`tools/workspace.py`). The workspace is
      reset before every candidate, so a failed candidate cannot leak into
      the next.
- [x] `apply_patch` AST validation via tree-sitter (ADR-12), allowlist
      checks from Sec7/Sec9
- [x] Sandbox: real per-run Docker container (Sec10), with a separate CI
      job that exercises a real Docker daemon
- [~] Fixture app (separate repo, `Devpaul-01/fixture`) exists and now writes
      the JSON report the tool reads (`a968166`) -- but its suite is
      unverified and it has **no tagged known-green baseline** (`git tag` is
      empty). I could not run it where this was built (no browser download).
- [ ] End-to-end: a patch through the real pipeline against the real
      fixture app, full `runs`/`attempts` records produced. Blocked on a
      real Docker + fixture run; see "Not yet proven".

## M2 -- LLM-backed diagnosis and repair

Acceptance: a real stale-locator test case is diagnosed, patched, and
verified correctly end-to-end.

- [x] Groq provider integration, behind the `Diagnoser` protocol
      (`orchestrator/driver.py`) so the Driver never imports Groq directly
- [x] Structured diagnosis call (tool-calling, ranked candidates /
      not-repairable)
- [x] Structured patch-proposal call
- [x] Candidate/re-diagnosis retry policy (Sec6) wired through the Driver
- [x] Transient vs. semantic retry paths kept separate (Sec12). Transient
      failures are retried at the provider layer by the Groq SDK, now
      configured explicitly (3 retries, 30s timeout); once those are
      exhausted the failure becomes a `DiagnoserError`. Semantic retry
      remains the orchestrator's candidate/re-diagnosis policy.
- [x] Infrastructure failures end the run cleanly: `ToolError` and
      `DiagnoserError` finish the run as `FAILED` with the reason recorded
      (distinct from `REJECTED`); unexpected exceptions finalize the run row
      and re-raise.
- [ ] Acceptance criterion itself (a real stale-locator test diagnosed,
      patched and verified for real) -- never run against live Groq.

## M3 -- Richer evidence

Acceptance: diagnosis demonstrably prefers a more stable locator when one
is available in source, measurable via the benchmark.

- [x] Component-source resolution (`tools/component_source.py`). Uses
      **structural matching** against the fixture app's `.tsx` source,
      not source maps -- see `docs/architecture.md` Sec24 for why.
      Handles exact `data-testid`, template-literal testids by static
      prefix, and text/role locators; reports ambiguity.
- [x] Component source folded into diagnosis evidence
      (`FailureEvidence.component_source_context`, both Groq prompts)
- [~] `probe_live_dom`: now runnable. The script ships in the package and is
      delivered as `node -e`, candidates are validated against the allowlist
      before being evaluated, and the Driver can call it
      (`--probe-dom` / `Driver(probe_candidates=True)`). Still `[~]`: it is
      **advisory only** (the result goes to the run log and never affects the
      outcome) and **shallow** (no actions are replayed, so only the app's
      landing page is probed). It does not feed diagnosis.
- [ ] Acceptance criterion ("measurably prefers a more stable locator"):
      the machinery exists but no benchmark run has measured it.

## M4 -- Evaluation harness

Acceptance: a results table exists showing success/false-repair/
correct-rejection rates per configuration, plus the adversarial suite's
pass results.

- [~] Synthetic benchmark generator (`eval/mutations.py`,
      `eval/benchmark.py`): **2 of the 4** Sec16 mutation types
      (attribute rename, element removal). DOM restructure and sibling
      reorder are deferred -- they need a real JSX AST transform to do
      safely. 4 scenarios, each mapped to a real fixture-app test.
- [x] Metrics from `runs`/`attempts` (`eval/metrics.py`): all six Sec16
      metrics, with false-repair judged against ground truth
- [x] Comparative evaluation across >= 2 diagnosis configurations
      (`eval/runner.py`), driven by `play-agent eval benchmark`. Scenarios
      that cannot run are reported, not dropped; empty-denominator rates
      print `n/a`.
- [~] Adversarial suite (`eval/adversarial.py`, `play-agent eval adversarial`):
      6 named scenarios, each
      blocked for its specific reason. Tests the orchestrator's validation
      layer (Sec8 layer 2) directly with attacker-chosen payloads. It does
      **not** test whether injected evidence can steer a real model into
      proposing such a payload.
- [ ] The acceptance results table itself -- needs a real run.

## M5 -- CLI/CI polish

Acceptance: the tool is runnable end-to-end by someone other than the
author, from a failing test to a reviewed diff.

- [x] CLI: `play-agent repair <test-file> <test-title>` writes
      `report.md` and (when verified) `repair.diff` per run, prints the diff,
      and states that the file was not modified (Sec19). `play-agent eval
      adversarial|benchmark` drives the harness.
- [ ] CI integration example for users of the tool (the repo's own CI,
      `.github/workflows/ci.yml`, exists but is not that)
- [x] Observability logging (Sec17): one JSON line per transition, tool
      call, diagnosis, and run completion (`observability/logging.py`)
- [~] Redaction/retention policy: redaction done and tested
      (`observability/redaction.py`); retention is deliberately delegated
      to the operator's own log rotation since the logger only appends
- [x] Documentation pass (README, this checklist, architecture Sec24)

## Not yet proven

These are the claims the unit tests cannot make:

1. A real `play-agent repair` run against a live Docker daemon and a real
   Groq key, start to finish.
2. That the fixture's suite is green, and that sandbox verification works
   against it. The fixture's config hard-codes `localhost` URLs and starts
   its own `webServer`s; how that behaves inside the container has not been
   exercised.
3. Any benchmark numbers. The harness computes metrics; nothing has
   produced any.

What *has* been verified against the real thing: the probe script runs under
real `node` (against a stand-in `playwright`), the evidence parser reads a
genuine Playwright JSON report, the fixture's JSON reporter writes to the
expected path, and a produced diff applies with `git apply`.

## Open architectural decisions (settled during implementation)

- **ADR-12:** TS/JS AST parsing for `apply_patch` done via `tree-sitter`
  (in-process, Python-native), not a Node subprocess -- keeps the
  orchestrator's process/filesystem boundary (Sec4) from having to reason
  about a second language runtime. See `docs/architecture.md` for the full
  rationale.
- **Fixture app ownership:** the orchestrator takes a local filesystem path
  to the fixture app (`PLAY_AGENT_APP_PATH`), not a git URL -- it never
  clones anything itself. Cloning is a human/CI setup step.
- **Component-source resolution:** structural matching, not source maps
  (`docs/architecture.md` Sec24).

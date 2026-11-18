# play-agent

A self-healing E2E test agent for Playwright. When a test fails, it
diagnoses whether the failure is a stale element locator, proposes a
replacement, applies it in an isolated sandbox, verifies the fix by
re-running the test, and reports the outcome for human review.

**It never auto-commits.** v1 scope is locator repairs only: it is
structurally unable to touch assertions or test logic, not merely
instructed not to. See `docs/architecture.md` for the full design and
`docs/checklist.md` for exactly what is and isn't done.

> **Status: all components implemented and unit-tested (275 tests); the full
> pipeline has not yet been run end-to-end against a live Docker daemon and
> a real Groq key.** Read "Known limitations" before relying on it.

## How it works

```
failing test
    |
    v
collect evidence      Playwright JSON report: error, stack, trace
    |                 + component source resolved from the frontend
    v
diagnose (Groq)       stale locator? ranked candidate repairs, or "not repairable"
    |
    v
propose repair        one exact replacement locator expression
    |
    v
validate              tree-sitter AST check: allowlisted locator methods,
    |                 literal arguments only, must target a real locator call
    v
apply + verify        patch applied to a scratch COPY of your app (your files
    |                 are never touched); test re-run N times in a Docker sandbox
    v
accept / retry / reject   every step recorded in SQLite and a JSON log
    |
    v
report.md + repair.diff   you review the diff and apply it yourself
```

The safety model has four layers (Sec8): the model only ever emits
structured output; the orchestrator validates its shape; a tool executor
is the only thing that touches files; and the sandbox enforces isolation
at the OS level, independent of application logic.

## Setup

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), and Docker (for
real runs; unit tests do not need it).

```bash
git clone https://github.com/Devpaul-01/play-agent.git
cd play-agent
uv sync --all-extras
```

## Configuration

All configuration is environment variables, read in exactly one place
(`src/play_agent/config.py`).

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `PLAY_AGENT_GROQ_API_KEY` | yes | | Groq API key |
| `PLAY_AGENT_APP_PATH` | yes | | Local checkout of the app under test (mounted into the sandbox) |
| `PLAY_AGENT_APP_HOST` | yes | | Bare **hostname** of the app, e.g. `localhost`. Not a URL. |
| `PLAY_AGENT_APP_PORT` | no | `3000` | App port. The sandbox builds `http://<host>:<port>` itself. |
| `PLAY_AGENT_GROQ_MODEL` | no | `llama-3.3-70b-versatile` | Diagnosis model |
| `PLAY_AGENT_SANDBOX_IMAGE` | no | `play-agent-sandbox:latest` | Docker image the sandbox runs |
| `PLAY_AGENT_COMPONENT_SRC_ROOT` | no | unset | Frontend `src/` directory, e.g. `<app>/frontend/src`. Enables component-source evidence; omit it and diagnosis runs without. |
| `PLAY_AGENT_DB_PATH` | no | `play_agent.sqlite3` | SQLite run/attempt store |
| `PLAY_AGENT_LOG_PATH` | no | `play_agent.log` | JSON-lines run log |
| `PLAY_AGENT_OUTPUT_DIR` | no | `play-agent-output` | Where each run's `report.md` and `repair.diff` are written |

When `PLAY_AGENT_APP_HOST` is `localhost`/`127.0.0.1`/`0.0.0.0`, the
sandbox rewrites it to `host.docker.internal` (mapped to the Docker host
gateway), since `localhost` inside a container is the container itself.

### Before you run it

`play-agent repair` does **not** run your failing test itself: it reads the
failure from Playwright's JSON report, so run your suite first and let it
fail. The report is expected at `test-results/results.json` next to your
`playwright.config.*`, which needs a JSON reporter:

```ts
reporter: [["list"], ["json", { outputFile: "test-results/results.json" }]],
```

The fixture app has this since commit `a968166`.

The app under test is a **separate repository**
([`Devpaul-01/fixture`](https://github.com/Devpaul-01/fixture), a small
React + Playwright app built to exercise this agent). play-agent never
clones it; check it out and point `PLAY_AGENT_APP_PATH` at it.

## Usage

```bash
uv run play-agent repair <test-file> "<test title>" [--reps N] [--output-dir DIR] [--log-path PATH] [--probe-dom]
```

- `<test-file>`: the Playwright spec containing the failing test. It must be
  inside the app at `PLAY_AGENT_APP_PATH`.
- `<test title>`: the test's title exactly as Playwright reports it
- `--reps`: verification re-runs before a fix is trusted as non-flaky (default 3)
- `--output-dir`: where the report and diff go (default `play-agent-output`)
- `--log-path`: override the run log
- `--probe-dom`: also check each proposed locator against the live app and
  record the result in the run log. Advisory only; it never changes the
  outcome, and it probes only the app's landing page (nothing is replayed).

**Your test file is never modified and nothing is committed.** The tool works
on a scratch copy of the app. Each run writes `<output-dir>/<run-id>/`:

- `report.md`: the outcome, the reason, every candidate tried with its
  verification result, and the diff
- `repair.diff`: only when a repair was verified. Its paths are relative to
  the app root, so apply it from there: `git apply <path>/repair.diff`

Exit code is `0` only when the run reaches `SUCCEEDED`. A failed or rejected
repair, a sandbox error, or a configuration error exits `1`. An infrastructure
failure (a missing Playwright report, an unreachable or malformed model
response) ends the run as `FAILED` with the reason recorded, distinct from
`REJECTED`, which means the failure was diagnosed as not locator-repairable.

## Evaluation harness

`src/play_agent/eval/` measures repair quality against known ground truth
(Sec16).

```bash
uv run play-agent eval adversarial                     # no Docker or API key needed
uv run play-agent eval benchmark --model <a> --model <b> [--reps N] [--output results.json]
```

- `eval adversarial` prints the attack -> blocked -> layer table and exits 1
  if any attack was not blocked.
- `eval benchmark` runs every scenario once per `--model` through the **real
  pipeline** (it spends Docker time and Groq credit) and prints a per-model
  comparison. Repeat `--model` to compare; Sec16 asks for at least two. A
  rate over an empty denominator prints `n/a`, and scenarios that could not
  run are listed with their reason rather than silently dropped.

The pieces: `mutations.py` / `benchmark.py` (controlled mutations of the
fixture app's component source, each mapped to a real fixture test, with the
correct repair as ground truth); `metrics.py` (success, false-repair,
correct-rejection, candidates-per-success, re-diagnosis rate, cost,
latency); `runner.py` (`run_comparison_with_details`); `adversarial.py`.

## Project layout

```
src/play_agent/
  cli/            the `play-agent` command
  orchestrator/   state machine, Driver run loop, Groq diagnoser
  tools/          tool contracts, AST patch validation, component-source
                  resolution, the isolated patch workspace, the sandboxed
                  tool belt, and probe_dom.js (run inside the sandbox)
  sandbox/        per-run Docker container lifecycle
  store/          SQLite runs/attempts
  observability/  JSON run logging and redaction
  eval/           benchmark, metrics, comparison runner, adversarial suite
  report.py       the per-run report.md / repair.diff
scripts/          a Groq smoke test
docs/             architecture.md (the design), checklist.md (the status)
```

## Development

```bash
uv run pytest tests/ --ignore=tests/test_container_integration.py   # unit tests
uv run pytest tests/test_container_integration.py                   # needs Docker
uv run ruff check src tests
```

CI (`.github/workflows/ci.yml`) runs lint and unit tests, plus a separate
job that exercises a real Docker daemon.

## Known limitations

These are real, current gaps, not future ideas:

1. **Never run end-to-end for real.** No live Docker + live Groq run has
   happened. Unit tests use fakes for both, plus one test that runs the
   probe script under real `node` against a stand-in `playwright`.
2. **The fixture's suite is unverified and untagged.** I could not run it in
   the environment that built this (no browser download), so there is no
   known-green baseline tag. Run `npm test` in the fixture, and if it is
   green: `git tag -a baseline-green -m "known-green baseline"`.
3. **How the sandbox verifies against the fixture is untested.** Verification
   runs `npx playwright test` inside a container whose working directory is a
   copy of the app. The fixture's config hard-codes `localhost` URLs and
   starts its own `webServer`s, and how that behaves in the container
   (versus `APP_UNDER_TEST_URL`) has not been exercised.
4. **`--probe-dom` is advisory and shallow.** It only sees the app's landing
   page, because nothing derives the actions to replay from the failing
   test. It never gates or influences a repair.
5. **The workspace copies the whole app**, `node_modules` included, per run.
   It is configurable only in code (`SandboxedToolBeltConfig.workspace_ignore`),
   not from the CLI or environment.
6. **Benchmark coverage.** Two of the four planned mutation types, and no
   benchmark numbers have been produced.
7. **Adversarial suite scope.** It proves the validation layer rejects
   malicious payloads. It does not prove injected page content can't
   influence what a real model proposes.
8. **Latency is verification time only.** `state_duration_ms` on an attempt
   is the time spent verifying, not the whole attempt.

## License

Not yet specified.

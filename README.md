# play-agent

A self-healing E2E test agent for Playwright. When a test fails, it
diagnoses whether the failure is a stale element locator, proposes a
replacement, applies it in an isolated sandbox, verifies the fix by
re-running the test, and reports the outcome for human review.

**It never auto-commits.** v1 scope is locator repairs only: it is
structurally unable to touch assertions or test logic, not merely
instructed not to. See `docs/architecture.md` for the full design and
`docs/checklist.md` for exactly what is and isn't done.

> **Status: all components implemented and unit-tested; the full pipeline
> has not yet been run end-to-end against a live Docker daemon and a real
> Groq key.** Read "Known limitations" before relying on it.

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
apply + verify        patch applied, test re-run N times in a Docker sandbox
    |
    v
accept / retry / reject   every step recorded in SQLite and a JSON log
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

When `PLAY_AGENT_APP_HOST` is `localhost`/`127.0.0.1`/`0.0.0.0`, the
sandbox rewrites it to `host.docker.internal` (mapped to the Docker host
gateway), since `localhost` inside a container is the container itself.

The app under test is a **separate repository**
([`Devpaul-01/fixture`](https://github.com/Devpaul-01/fixture), a small
React + Playwright app built to exercise this agent). play-agent never
clones it; check it out and point `PLAY_AGENT_APP_PATH` at it.

## Usage

```bash
uv run play-agent repair <test-file> "<test title>" [--reps N] [--log-path PATH]
```

- `<test-file>`: path to the Playwright spec containing the failing test
- `<test title>`: the test's title exactly as Playwright reports it
- `--reps`: verification re-runs before a fix is trusted as non-flaky (default 3)
- `--log-path`: override the log file

Exit code is `0` only when the run reaches `SUCCEEDED`; a failed or
rejected repair, a sandbox error, or a configuration error exits `1`.

## Evaluation harness

`src/play_agent/eval/` measures repair quality against known ground truth
(Sec16). It is **library code with no CLI entry point yet**.

- `mutations.py` / `benchmark.py`: controlled mutations of the fixture
  app's component source (attribute rename, element removal), each mapped
  to a real fixture test, with the correct repair recorded as ground truth
- `metrics.py`: success rate, false-repair rate, correct-rejection rate,
  candidates-tried-per-success, re-diagnosis rate, cost and latency
- `runner.py`: `run_comparison(...)` runs every scenario against each
  `DiagnosisConfiguration` (e.g. two Groq models) and reports metrics per
  configuration
- `adversarial.py`: a separate, named adversarial suite. Run
  `print(format_results_table(run_adversarial_suite()))` for the
  attack -> blocked -> layer table. This one needs no Docker or API key.

## Project layout

```
src/play_agent/
  cli/            the `play-agent` command
  orchestrator/   state machine, Driver run loop, Groq diagnoser
  tools/          tool contracts, AST patch validation, component-source
                  resolution, the sandboxed tool belt
  sandbox/        per-run Docker container lifecycle
  store/          SQLite runs/attempts
  observability/  JSON run logging and redaction
  eval/           benchmark, metrics, comparison runner, adversarial suite
scripts/          probe_dom.js (see limitations), a Groq smoke test
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
   happened. Unit tests use fakes for both.
2. **Fixture reporter mismatch.** The fixture's Playwright config uses the
   `list` reporter; `collect_failure_evidence` reads Playwright's JSON
   report (`test-results/results.json`). Add a JSON reporter to the
   fixture's `playwright.config.ts` before a real run.
3. **`repair` edits the test file in place** and **never prints or saves
   the diff**, although its success message tells you to review one. The
   design (Sec7) says the original working-tree file is never touched and
   the diff is the deliverable. Until that's fixed, run the agent on a
   throwaway copy or a clean git working tree and use `git diff`.
4. **Unhandled errors.** A missing Playwright report (`ToolError`) or a
   malformed model response (`DiagnoserError`) is not caught by the Driver
   or CLI; you get a traceback and the run row is left unfinished.
5. **`probe_live_dom` is not usable.** It is implemented and unit-tested
   against a fake sandbox, but `scripts/probe_dom.js` is never mounted
   into the container, and the Driver never calls it.
6. **No explicit transient-retry layer** around Groq calls (Sec12).
7. **Benchmark coverage.** Two of the four planned mutation types; no
   benchmark numbers have been produced.
8. **Adversarial suite scope.** It proves the validation layer rejects
   malicious payloads. It does not prove injected page content can't
   influence what a real model proposes.

## License

Not yet specified.

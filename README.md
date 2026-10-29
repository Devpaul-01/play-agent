# play-agent

A self-healing E2E test agent for Playwright. When a test fails, it
diagnoses whether the failure is a stale element locator, proposes a
replacement, applies it in an isolated sandbox, verifies the fix by
re-running the test, and hands you a diff + report for review.

**It never auto-commits.** v1 scope is locator repairs only -- see
`docs/architecture.md` for the full design and `docs/checklist.md` for
current implementation progress.

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/Devpaul-01/play-agent.git
cd play-agent
uv sync --all-extras
```

This creates a `.venv` and installs the project plus dev dependencies
(pytest, ruff) from the lockfile.

## Configuration

play-agent reads configuration from environment variables -- see
`src/play_agent/config.py` for the full list. Minimum required:

```bash
export PLAY_AGENT_GROQ_API_KEY="your-groq-key"
export PLAY_AGENT_APP_PATH="/path/to/local/checkout/of/the-fixture-app"
export PLAY_AGENT_APP_HOST="http://localhost:5173"
```

The fixture app is a **separate repository** (a small React + Playwright
app built specifically to exercise this agent) -- play-agent itself never
clones it; you check it out locally and point `PLAY_AGENT_APP_PATH` at it.

## Running tests

```bash
uv run pytest
```

## Usage

```bash
uv run play-agent repair <failing-test-file>
```

(Not yet implemented -- see `docs/checklist.md`.)

## Project status

Early development -- see `docs/checklist.md` for the milestone checklist
and current progress.

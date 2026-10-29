# Implementation Checklist

Tracks progress against the milestones in `docs/architecture.md` Sec23.
Update this as tasks complete -- it's the source of truth for "what's
actually done" vs. "what's planned."

## M1 -- Deterministic skeleton

Acceptance: a hard-coded "patch" flows end-to-end through apply -> verify
-> accept/reject with full `runs`/`attempts` records.

- [x] Repo scaffolded (`pyproject.toml`, package layout, config loading)
- [ ] SQLite store: `runs` + `attempts` tables, unit-tested directly
- [ ] State machine (Sec6 transitions), stubbed diagnosis output, unit-tested
      with no I/O
- [ ] Tool contracts (Sec7): `collect_failure_evidence`, `probe_live_dom`,
      `apply_patch`, `run_verification` -- stubbed against a plain temp
      directory first
- [ ] `apply_patch` AST validation via tree-sitter (ADR-12), allowlist
      checks from Sec7/Sec9
- [ ] Sandbox: real per-run Docker container (Sec10) replacing the temp-dir
      stand-in
- [ ] Fixture app (separate repo) exists with a tagged known-green baseline
- [ ] End-to-end: hard-coded patch through the real pipeline against the
      real fixture app, full `runs`/`attempts` records produced

## M2 -- LLM-backed diagnosis and repair

Acceptance: a real stale-locator test case is diagnosed, patched, and
verified correctly end-to-end.

- [ ] Groq provider integration behind the `LLMProvider` thin interface
- [ ] Structured diagnosis call (ranked candidates / not-repairable)
- [ ] Structured patch-proposal call
- [ ] Candidate/re-diagnosis retry policy (Sec6) wired to real LLM calls
- [ ] Transient vs. semantic retry paths kept separate (Sec12)

## M3 -- Richer evidence

Acceptance: diagnosis demonstrably prefers a more stable locator when one
is available in source, measurable via the benchmark.

- [ ] `probe_live_dom` tool, live browser context, resource-limited
- [ ] Component-source resolution (tied to fixture app's source maps)
- [ ] Both folded into diagnosis evidence assembly (Sec11)

## M4 -- Evaluation harness

Acceptance: a results table exists showing success/false-repair/
correct-rejection rates per configuration, plus the adversarial suite's
pass results.

- [ ] Synthetic benchmark generator (mutations of the fixture app's
      baseline suite)
- [ ] Metrics computed from `runs`/`attempts` (Sec16)
- [ ] Comparative evaluation across >= 2 diagnosis configurations
- [ ] Adversarial test suite, per-case layer-blocked reporting

## M5 -- CLI/CI polish

Acceptance: the tool is runnable end-to-end by someone other than the
author, from a failing test to a reviewed diff.

- [ ] Full CLI UX
- [ ] CI integration example
- [ ] Observability logging (Sec17)
- [ ] Redaction/retention policy
- [ ] Documentation pass

## Open architectural decisions (settled during implementation)

- **ADR-12:** TS/JS AST parsing for `apply_patch` done via `tree-sitter`
  (in-process, Python-native), not a Node subprocess -- keeps the
  orchestrator's process/filesystem boundary (Sec4) from having to reason
  about a second language runtime. See `docs/architecture.md` for the full
  rationale.
- **Fixture app ownership:** the orchestrator takes a local filesystem path
  to the fixture app (`PLAY_AGENT_APP_PATH`), not a git URL -- it never
  clones anything itself. Cloning is a human/CI setup step.

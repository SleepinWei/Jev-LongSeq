# Execution host

- Run project tests, browser tasks, benchmarks and model-backed experiments on
  `ssh macmini`, not on the local development Mac, unless the user explicitly
  requests otherwise.
- Remote checkout: `/Users/octopusz/CodeProjects/Jev-LongSeq`.
- Use `scripts/test_macmini.sh` for pytest. Pass pytest arguments after the script.
- For uncommitted work, first sync the changed source/test files to that checkout
  (without deleting remote files), then run the remote tests. Do not silently test
  a different commit. Inspect remote changes before overwriting them.
- Studio runs on macmini at `127.0.0.1:8768`; use an SSH tunnel to access it.
  Port 8767 on macmini belongs to another application and must remain untouched.
- Keep secrets, browser profiles and run artifacts out of Git. Reuse macmini's
  own browser session; do not copy local cookies or bypass Chrome approvals.
- Do not restart Studio while `/api/launcher` reports a running task.
- Do not spawn sub-agents unless the user explicitly asks for delegation.

# Inspect experiment results progressively

- For Codex-facing experiment investigation, start with the bounded summary:
  `PYTHONPATH=src .venv/bin/python -m jev_browser.diagnostics --run <run-id>`
  on macmini. Do not default-read complete trajectory/report/memory files or
  `/api/run` into context.
- Fetch only the relevant cycle (`--view step --cycle N`), filtered calls,
  official failed checks, or selected artifact JSON pointers. Follow returned
  `next_cursor` values; default to 12 KB per response. Save/share a bounded
  partial report with `--output /tmp/<name>.json` when needed.
- Use captured request artifacts to establish the actual historical model
  input. Latest memory and replay guidance are not substitutes. Missing legacy
  evidence, malformed rows and truncated previews must remain explicit.
- Check lifecycle through grading and cleanup before treating a report as
  final. Compare provider/model, task, budgets and checkpoint before attributing
  a score change to a harness improvement. See
  `docs/EXPERIMENT-DIAGNOSTICS.zh-CN.md` for commands, API and evidence limits.

# Benchmark iteration

- After changing the harness, controller, observation, memory/context handling,
  model adapter or scorer for an active benchmark, automatically rerun the
  affected benchmark after the relevant remote checks pass. Do not stop at unit
  tests or wait for the user to remind you to run it.
- Sync and verify the exact tested source on macmini before launching. Preserve
  the approved provider/model, original task, memory recovery checkpoint and
  budgets unless the user requests a change; always use a fresh output directory.
- Keep the existing push-before-benchmark workflow when pushing is authorized.
  Keep credentials and real run artifacts out of Git.
- Check both Studio's launcher and manually launched benchmark processes before
  starting. Do not run duplicate trials on the same slot or interrupt an existing
  trial. If it is busy, wait for it to finish, then run the affected benchmark.
- Follow the rerun through official grading and environment cleanup and report
  its score, stop reason and material differences. Unit-test success is not a
  benchmark result. If an external dependency blocks the rerun, report that
  specific blocker instead of claiming completion.

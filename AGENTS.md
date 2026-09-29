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

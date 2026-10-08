#!/bin/bash
# Run on the benchmark host. Reuse the existing model env without printing secrets.
set -euo pipefail
project_dir="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
export SAAS_BENCH_ROOT="${SAAS_BENCH_ROOT:-$HOME/SaaS-Bench-v1.1}"
export SAAS_SLOT_PREFIX="${SAAS_SLOT_PREFIX:-jevsaas}"
export SAAS_BASE_PORT="${SAAS_BASE_PORT:-31000}"
export JEV_ULTRAFAST_ROOT="${JEV_ULTRAFAST_ROOT:-$project_dir/external/jev-ultrafast}"
if [[ "$(uname -s)" == Darwin ]]; then
  export DOCKER_CONTEXT="${DOCKER_CONTEXT:-colima-saas-bench}"
  export DOCKER_DEFAULT_PLATFORM="${DOCKER_DEFAULT_PLATFORM:-linux/amd64}"
fi
# Use the execution host's Playwright cache by default. A project .browsers
# directory can contain an older revision after a Playwright upgrade. Preserve
# PLAYWRIGHT_BROWSERS_PATH only when the operator explicitly configured it.
export PYTHONPATH="$project_dir/src${PYTHONPATH:+:$PYTHONPATH}"
python_bin="${JEV_PYTHON:-$project_dir/.venv/bin/python}"
default_env="$HOME/.config/jev-longseq/api.env"
[[ ! -f "$project_dir/.env" ]] || default_env="$project_dir/.env"
env_file="${JEV_ENV_FILE:-$default_env}"
env_args=()
[[ ! -f "$env_file" ]] || env_args=(--env-file "$env_file")
case "${1:-}" in
  benchmark|autoresearch)
    mode="$1"; shift
    exec "$python_bin" -m jev_browser "$mode" --suite saas-bench --brain api \
      "${env_args[@]}" "$@"
    ;;
  inspector)
    shift
    exec "$python_bin" -m jev_browser.inspector "${env_args[@]}" "$@"
    ;;
  *)
    echo "Usage: $0 {benchmark|autoresearch|inspector} [options]" >&2
    exit 2
    ;;
esac

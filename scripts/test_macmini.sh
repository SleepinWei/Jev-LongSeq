#!/bin/bash
set -euo pipefail
target="${JEV_SSH_TARGET:-macmini}"
remote_root="${JEV_REMOTE_ROOT:-/Users/octopusz/CodeProjects/Jev-LongSeq}"
printf -v root_q '%q' "$remote_root"
if [ "$#" -eq 0 ]; then set -- -q; fi
printf -v args_q ' %q' "$@"
exec ssh -o BatchMode=yes "$target" "export PATH=/opt/homebrew/bin:\$HOME/.local/bin:\$PATH; cd $root_q && .venv/bin/python -m pytest$args_q"

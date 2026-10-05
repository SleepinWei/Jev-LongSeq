"""Process-aware experiment phases, outside the agent's empty output directory."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

from .observability import write_json
from .protocol import digest, now

TERMINAL = {'finished', 'failed', 'cancelled'}


def process_identity(pid):
    try:
        result = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart='],
                                capture_output=True, text=True, timeout=2, check=False)
        return result.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def status_path(output):
    output = Path(output).resolve()
    return output.parent / '.run-status' / (digest(str(output)) + '.json')


def register_phase(output, phase, **metadata):
    """Telemetry failure must not change experiment execution."""
    target = status_path(output)
    try:
        if not target.resolve().is_relative_to(Path(output).resolve().parent):
            return
        identity = process_identity(os.getpid())
        previous = json.loads(target.read_text()) if target.exists() else {}
        if previous.get('pid') != os.getpid() or previous.get('process_identity') != identity:
            previous = {'started_at': now()}
        target.parent.mkdir(parents=True, exist_ok=True)
        write_json(target, {**previous, **metadata, 'output': str(Path(output).resolve()),
                            'pid': os.getpid(), 'process_identity': identity,
                            'phase': phase, 'updated_at': now()})
    except (OSError, ValueError):
        pass


def read_status(output):
    try:
        target = status_path(output)
        if not target.resolve().is_relative_to(Path(output).resolve().parent):
            return {'phase': 'untracked', 'process_alive': None}
        with target.open() as stream:
            row = json.loads(stream.read(64_000))
        if not isinstance(row, dict):
            raise ValueError('invalid status record')
        row.setdefault('phase', 'unknown')
    except (OSError, ValueError, RecursionError):
        return {'phase': 'untracked', 'process_alive': None}
    pid = row.get('pid')
    if type(pid) is not int or pid <= 0:
        return {**row, 'process_alive': None}
    current = process_identity(pid)
    # An unavailable start signature is not proof of a matching process.
    alive = current == row.get('process_identity') if current and row.get('process_identity') else None
    if current is None:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            alive = False
        except PermissionError:
            pass
    if alive is False and row.get('phase') not in TERMINAL:
        row = {**row, 'last_phase': row.get('phase'), 'phase': 'interrupted'}
    return {**row, 'process_alive': alive}


def activities(root):
    root = Path(root).resolve()
    result, known = [], set()
    for target in root.rglob('.run-status/*.json'):
        try:
            row = json.loads(target.read_text())
            output = Path(row['output']).resolve()
            if not output.is_relative_to(root):
                continue
            row = read_status(output)
            if row.get('process_alive') is True and row.get('phase') not in TERMINAL:
                result.append({**row, 'id': output.relative_to(root).as_posix(), 'tracked': True})
                known.add(row['pid'])
        except (OSError, ValueError, KeyError, TypeError):
            continue
    # Legacy manual launches do not have registration records. Inspect argv,
    # never match arbitrary text in ssh commands or interrupt these processes.
    try:
        process_list = subprocess.run(['ps', '-axo', 'pid=,command='], capture_output=True,
                                      text=True, timeout=2, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return result
    for line in process_list.splitlines():
        try:
            pid_text, command = line.strip().split(None, 1)
            pid, args = int(pid_text), shlex.split(command)
            native = len(args) > 2 and args[1:3] == ['-m', 'jev_browser']
            shell = len(args) > 1 and Path(args[0]).name == 'bash' and args[1].endswith('scripts/saas-bench.sh')
            if not (native or shell) or '--output' not in args or pid in known:
                continue
            candidate = Path(args[args.index('--output') + 1])
            # ps does not provide the launch CWD; a relative output is ambiguous.
            if not candidate.is_absolute():
                continue
            output = candidate.resolve()
            if not output.is_relative_to(root):
                continue
            result.append({'pid': pid, 'id': output.relative_to(root).as_posix(),
                           'phase': 'running_legacy', 'process_alive': True, 'tracked': False})
        except (ValueError, IndexError):
            continue
    return result

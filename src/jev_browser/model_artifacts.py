"""Private model wire evidence; metadata logs remain small and credential free."""
from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

from .protocol import now

SENSITIVE = re.compile(r'^(?:authorization|proxy.authorization|api[_-]?key|access[_-]?token|'
                       r'refresh[_-]?token|cookie|set.cookie|password|client[_-]?secret)$', re.I)


def redact(value, secrets=()):
    secrets = tuple(s for s in secrets if isinstance(s, str) and s)
    def visit(item):
        if isinstance(item, dict):
            return {k: '[redacted]' if SENSITIVE.fullmatch(str(k)) else visit(v) for k, v in item.items()}
        if isinstance(item, list):
            return [visit(v) for v in item]
        if isinstance(item, str):
            for secret in secrets:
                item = item.replace(secret, '[redacted]')
            # Model messages often contain JSON encoded inside a string.
            try:
                parsed = json.loads(item)
                if isinstance(parsed, (dict, list)):
                    return json.dumps(visit(parsed), ensure_ascii=False)
            except ValueError:
                pass
            return re.sub(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+', 'Bearer [redacted]', item)
        return item
    return visit(value)


def capture(output, attempt_id, direction, value, secrets=()):
    if not re.fullmatch(r'[a-f0-9]{32}', attempt_id) or direction not in {'request', 'response'}:
        raise ValueError('invalid model artifact identity')
    configured = [v for k, v in os.environ.items()
                  if k.endswith(('_API_KEY', '_ACCESS_TOKEN', '_REFRESH_TOKEN', '_CLIENT_SECRET')) and v]
    relative = f'model-artifacts/{attempt_id}.{direction}.json'
    target = Path(output) / relative
    private = redact(value, (*secrets, *configured))
    record = {'schema_version': 1, 'attempt_id': attempt_id, 'direction': direction,
              'captured_at': now(), 'redaction': 'credential keys and configured credential values',
              'data': private}
    encoded = json.dumps(record, ensure_ascii=False).encode()
    if len(encoded) > 16 * 1024 * 1024:
        raise ValueError('model artifact exceeds 16 MiB capture limit')
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not target.parent.resolve().is_relative_to(Path(output).resolve()):
        raise ValueError('model artifact directory escapes run output')
    target.parent.chmod(0o700)
    temporary = target.parent / (uuid.uuid4().hex + '.tmp')
    # Set permissions when creating the file, before writing sensitive task data.
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(encoded)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return {'path': relative, 'bytes': len(encoded), 'redacted': True}

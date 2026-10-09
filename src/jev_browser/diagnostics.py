"""Bounded, read-only experiment evidence for progressive Codex investigation."""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

from .model_artifacts import redact
from .run_status import read_status

DEFAULT_BYTES = 12_000
MAX_BYTES = 64_000
READ_LIMIT = 32 * 1024 * 1024
JSON_FILES = {'manifest.json', 'task.json', 'report.json', 'result.json', 'memory.json',
              'resume-memory-initial.json', 'observability.json', 'live.json', 'gym-actions.json'}
LOGS = {'events': ('trajectory.jsonl',), 'calls': ('model-calls.jsonl',),
        'starts': ('model-request-starts.jsonl',), 'spans': ('spans.jsonl',),
        'contexts': ('context-projections.jsonl',), 'frames': ('frames.jsonl',),
        'scores': ('process-scores.jsonl',)}
LOG_FILES = {name for files in LOGS.values() for name in files} | {'network-preconnects.jsonl'}
ARTIFACT = re.compile(r'model-artifacts/[a-f0-9]{32}\.(request|response)\.json')


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def escape(value):
    return str(value).replace('~', '~0').replace('/', '~1')


def preview(value, *, chars=240, depth=2):
    """Explicit abbreviations, never pretend an abbreviated object is the raw evidence."""
    if isinstance(value, str):
        return value if len(value) <= chars else {
            'preview': value[:chars], 'total_chars': len(value), 'omitted_chars': len(value) - chars}
    if isinstance(value, (dict, list)):
        if depth <= 0:
            return {'type': type(value).__name__, 'items': len(value), 'omitted_items': len(value)}
        if isinstance(value, list):
            return {'items': [preview(v, chars=chars, depth=depth - 1) for v in value[:5]],
                    'total_items': len(value), 'omitted_items': max(0, len(value) - 5)}
        keys = list(value)[:24]
        result = {k: preview(value[k], chars=chars, depth=depth - 1) for k in keys}
        if len(value) > len(keys):
            result['_omitted_fields'] = len(value) - len(keys)
        return result
    return value


def pointer_get(value, pointer):
    if pointer == '':
        return value
    if not pointer.startswith('/') or re.search(r'~(?![01])', pointer):
        raise ValueError('invalid JSON pointer')
    for part in pointer[1:].split('/'):
        # HTTP messages and response bodies contain JSON encoded in strings.
        # Allow field-level inspection without returning that entire string.
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                raise ValueError('pointer crosses a non-JSON string') from None
        key = part.replace('~1', '/').replace('~0', '~')
        if isinstance(value, dict):
            value = value[key]
        elif isinstance(value, list) and re.fullmatch(r'0|[1-9][0-9]*', key):
            value = value[int(key)]
        else:
            raise ValueError('JSON pointer does not select a value')
    return value


class Diagnostics:
    def __init__(self, root, run):
        self.root = Path(root).resolve()
        candidate = Path(run)
        if candidate.is_absolute() or '..' in candidate.parts:
            raise ValueError('invalid run id')
        self.path = (self.root / candidate).resolve()
        if not self.path.is_relative_to(self.root) or not self.path.is_dir():
            raise ValueError('run unavailable')
        self.run = self.path.relative_to(self.root).as_posix()
        # Setup-phase runs can precede manifest creation; directory existence is enough.

    def file(self, name):
        if name not in JSON_FILES | LOG_FILES and not ARTIFACT.fullmatch(name):
            raise ValueError('artifact not allowed')
        target = (self.path / name).resolve()
        if not target.is_relative_to(self.path):
            raise ValueError('artifact escapes run directory')
        return target

    def json(self, name):
        target = self.file(name)
        try:
            with target.open('rb') as stream:
                content = stream.read(READ_LIMIT + 1)
            if len(content) > READ_LIMIT:
                return None, 'read_limit_exceeded'
            return redact(json.loads(content)), 'available'
        except FileNotFoundError:
            return None, 'missing'
        except (ValueError, UnicodeError, RecursionError):
            return None, 'malformed'
        except OSError:
            return None, 'unreadable'

    def rows(self, files, warnings):
        cursor = 0
        for name in files:
            try:
                stream = self.file(name).open('rb')
            except FileNotFoundError:
                warnings[name] = {'status': 'missing'}
                continue
            warnings[name] = {'status': 'available', 'invalid_lines': 0}
            with stream:
                line = 0
                while True:
                    raw = stream.readline(READ_LIMIT + 1)
                    if not raw:
                        break
                    if len(raw) > READ_LIMIT:
                        # Consume a single oversize physical row without buffering it.
                        while raw and not raw.endswith(b'\n'):
                            raw = stream.readline(READ_LIMIT + 1)
                        warnings[name]['invalid_lines'] += 1
                        yield cursor, name, line, None
                    else:
                        try:
                            row = json.loads(raw)
                            if not isinstance(row, dict):
                                raise ValueError('not a record')
                            yield cursor, name, line, redact(row)
                        except (ValueError, UnicodeError, RecursionError):
                            warnings[name]['invalid_lines'] += 1
                            yield cursor, name, line, None
                    cursor += 1
                    line += 1

    def summary(self):
        report, report_status = self.json('report.json')
        report = report if isinstance(report, dict) else {}
        result = report.get('result') or self.json('result.json')[0] or {}
        manifest, manifest_status = self.json('manifest.json')
        manifest = manifest if isinstance(manifest, dict) else {}
        lifecycle = redact(read_status(self.path))
        phase = lifecycle['phase']
        final = phase in {'finished', 'failed', 'cancelled'}
        legacy_final = phase == 'untracked' and bool(report.get('environment'))
        grade = report.get('grade') or {}
        warnings, kinds, errors = {}, Counter(), Counter()
        call_count = latency = inputs = outputs = unknown = raw_inputs = raw_outputs = 0
        completed, started = set(), {}
        models, finishes, largest_requests = Counter(), Counter(), {}
        empty_responses = 0
        def call_rows():
            if not self.file('model-calls.jsonl').exists():
                warnings['model-calls.jsonl'] = {'status': 'missing', 'fallback': 'report.model_calls'}
                yield from report.get('model_calls', [])
            else:
                for _, _, _, row in self.rows(LOGS['calls'], warnings):
                    yield row

        for row in call_rows():
            if row is None:
                continue
            call_count += 1
            kinds[str(row.get('kind'))] += 1
            models[str(row.get('model'))] += 1
            if row.get('finish_reason') is not None:
                finishes[str(row['finish_reason'])] += 1
            empty_responses += int(row.get('response_content_chars') == 0)
            size = row.get('payload_sizes', {}).get('total_bytes', 0)
            component = str(row.get('kind'))
            if size > largest_requests.get(component, {}).get('bytes', 0):
                largest_requests[component] = {'bytes': size, 'cycle': row.get('cycle'),
                                               'attempt_id': row.get('attempt_id')}
            if row.get('attempt_id'):
                completed.add(row['attempt_id'])
            if row.get('error') or (row.get('status') or 0) >= 400:
                errors[str(row.get('error') or row.get('status'))] += 1
            latency += row.get('latency_s') or 0
            inputs += row.get('input_tokens') or 0
            outputs += row.get('output_tokens') or 0
            unknown += int(row.get('input_tokens') is None or row.get('output_tokens') is None)
            raw_inputs += int(bool(row.get('request_artifact', {}).get('available')))
            raw_outputs += int(bool(row.get('response_artifact', {}).get('available')))
        for _, name, line, row in self.rows(LOGS['starts'], warnings):
            if row and row.get('attempt_id'):
                started[row['attempt_id']] = {'cycle': row.get('cycle'), 'kind': row.get('kind'),
                                             'file': name, 'line': line}
        last_event = last_failure = None
        event_count = 0
        for _, name, line, row in self.rows(LOGS['events'], warnings):
            if row is None:
                continue
            event_count += 1
            record = {'file': name, 'line': line, 'cycle': row.get('cycle'),
                      'kind': row.get('kind'), 'time': row.get('time'),
                      'detail': preview(row, depth=1)}
            last_event = record
            event_kind = str(row.get('kind', ''))
            if (event_kind.startswith(('invalid_', 'error', 'failed', 'blocked')) or
                    event_kind.endswith(('_failed', '_rejected', '_unresolved', '_exhausted'))):
                last_failure = record
        available = []
        for name in sorted(JSON_FILES | LOG_FILES):
            target = self.file(name)
            if target.exists():
                available.append({'file': name, 'bytes': target.stat().st_size})
        inflight = {key: value for key, value in started.items() if key not in completed}
        score_count, score_baseline, score_latest = 0, None, None
        for _, name, line, row in self.rows(LOGS['scores'], warnings):
            if row is None:
                continue
            score_count += 1
            compact = {k: row.get(k) for k in ('phase', 'data_valid', 'earned', 'total',
                       'delta_earned', 'cycle', 'finished_at', 'duration_s')}
            compact['expand'] = {'view': 'artifact', 'file': name, 'line': line, 'pointer': ''}
            if row.get('phase') == 'baseline':
                score_baseline = compact
            score_latest = compact
        return {
            'lifecycle': lifecycle, 'report_status': report_status,
            'completion_confirmed': final,
            'completion_evidence': 'registered_terminal_phase' if final else
                                   'legacy_report_environment_only' if legacy_final else 'not_confirmed',
            'result': preview(result, chars=1000),
            'grade': {key: grade.get(key) for key in
                      ('strict_success', 'data_valid', 'score', 'earned', 'total', 'source')},
            'grade_final': final or legacy_final,
            'process_scoring': {'snapshots': score_count, 'baseline': score_baseline,
                                'latest': score_latest, 'agent_feedback': False},
            'cleanup': preview(report.get('environment', {})),
            'configuration': {key: preview(manifest.get(key), depth=2) for key in
                              ('task_id', 'benchmark', 'benchmark_version', 'upstream_revision',
                               'fixture_hash', 'verifier_hash', 'upstream_verifier_hash', 'verifier_patch',
                               'image_ids', 'port_map', 'policy', 'mode', 'models', 'context_limits', 'budget',
                               'tuning', 'code_hash', 'system_prompt_hash', 'continuation', 'process_scoring')},
            'manifest_status': manifest_status,
            'calls': {'attempts': call_count, 'by_kind': dict(kinds), 'by_model': dict(models),
                      'errors': dict(errors), 'latency_s': round(latency, 3),
                      'finish_reasons': dict(finishes), 'empty_content_responses': empty_responses,
                      'largest_requests_by_kind': largest_requests,
                      'known_input_tokens': inputs, 'known_output_tokens': outputs,
                      'unknown_usage_attempts': unknown,
                      'request_artifacts': raw_inputs, 'response_artifacts': raw_outputs},
            'inflight': preview(inflight), 'event_count': event_count,
            'last_event': last_event, 'last_failure_event': last_failure,
            'artifacts': available, 'log_health': warnings,
            'evidence_limits': [
                'Unregistered historical runs cannot prove process completion or reconstruct missing requests.',
                'In-flight starts may be interrupted attempts; see lifecycle before assuming they are running.',
                'Cycle/observation links are recorded associations, not proof of causality.',
                'Frames are sampled; nearest frame is not an atomic before/after action capture.',
                'Process scores are non-atomic observer snapshots, never model feedback; missing historical scores cannot be reconstructed after cleanup.'],
            'next_queries': [
                {'view': 'step', 'cycle': (last_failure or last_event or {}).get('cycle')},
                {'view': 'calls'}, {'view': 'artifact', 'file': 'report.json', 'pointer': '/grade/checks'},
                {'view': 'artifact', 'file': 'memory.json', 'pointer': ''}],
        }

    def page(self, view, *, cycle, kind, cursor, limit, budget, filters):
        files = LOGS.get(view)
        if view == 'step':
            if cycle is None:
                raise ValueError('step requires cycle')
            files = tuple(name for key in ('events', 'calls', 'starts', 'spans', 'contexts', 'frames')
                          for name in LOGS[key])
        warnings, rows = {}, []
        next_cursor = None
        multi = len(files) > 1
        start_file, start_line = (map(int, str(cursor).split(':')) if ':' in str(cursor)
                                  else (0, int(cursor)))
        if multi and start_file >= len(files):
            raise ValueError('cursor source outside step logs')
        scanned = cursor
        for position, name, line, row in self.rows(files, warnings):
            source = files.index(name)
            scanned = f'{source}:{line + 1}' if multi else position + 1
            if source < start_file or (source == start_file and line < start_line) or row is None:
                continue
            if cycle is not None and row.get('cycle') != cycle:
                continue
            if kind is not None and row.get('kind', row.get('name')) != kind:
                continue
            if any(row.get(key) != val for key, val in filters.items() if val is not None):
                continue
            item = {'file': name, 'line': line, 'record': preview(row),
                    'expand': {'view': 'artifact', 'file': name, 'line': line, 'pointer': ''}}
            # Reserve enough envelope space so a row is never silently discarded.
            if rows and (len(rows) >= limit or len(encode(rows + [item])) > budget - 2600):
                next_cursor = f'{source}:{line}' if multi else position
                break
            if len(encode(item)) > budget - 2600:
                item['record'] = {key: preview(row.get(key), chars=80, depth=0) for key in
                                  ('kind', 'cycle', 'attempt_id')}
                item['record']['omitted'] = 'oversize_record'
            rows.append(item)
        return {'items': rows, 'next_cursor': next_cursor,
                'cursor_unit': 'file_index:physical_line' if multi else 'physical_log_row',
                'scanned_through': scanned, 'log_health': warnings,
                'association': 'recorded cycle/IDs; frame timestamps require separate alignment' if
                               view == 'step' else None}

    def artifact(self, *, file, pointer, line, cursor, limit, budget):
        if not file:
            raise ValueError('artifact requires file')
        target = self.file(file)
        if file in LOG_FILES:
            if line is None or line < 0:
                raise ValueError('JSONL artifact requires zero-based line')
            warnings = {}
            value, status = None, 'missing_line'
            for _, _, index, row in self.rows((file,), warnings):
                if index == line:
                    value, status = row, 'available' if row is not None else 'malformed_or_oversize_line'
                    break
        else:
            value, status = self.json(file)
        base = {'file': file, 'line': line, 'pointer': pointer, 'status': status,
                'snapshot': 'latest_only' if file == 'memory.json' else 'captured_artifact',
                'next_cursor': None}
        if status != 'available':
            return base
        value = pointer_get(value, pointer)
        base['type'] = type(value).__name__
        if isinstance(value, str):
            # Conservatively allow six JSON bytes per character (control escapes).
            width = max(1, (budget - len(encode(base)) - 1000) // 6)
            chunk = value[cursor:cursor + width]
            base.update(text=chunk, total_chars=len(value), cursor_unit='unicode_character',
                        next_cursor=cursor + len(chunk) if cursor + len(chunk) < len(value) else None)
        elif isinstance(value, (dict, list)):
            keys = list(value) if isinstance(value, dict) else range(len(value))
            children = []
            for index in range(cursor, len(keys)):
                key = keys[index]
                item = {'key': key, 'pointer': pointer + '/' + escape(key),
                        'preview': preview(value[key], depth=0)}
                if children and (len(children) >= limit or len(encode(children + [item])) > budget - 2000):
                    base['next_cursor'] = index
                    break
                if len(encode(item)) > budget - 2000:
                    raise ValueError('field name exceeds response budget; increase max_bytes')
                children.append(item)
            base.update(children=children, total_items=len(value), cursor_unit='child_index',
                        omitted_content='Containers show children, not full descendants; expand their pointers.')
        else:
            base['value'] = value
        base['artifact_bytes'] = target.stat().st_size
        return base

    def compare(self, baseline):
        other = Diagnostics(self.root, baseline)
        left, right = self.summary(), other.summary()
        current_manifest = self.json('manifest.json')[0] or {}
        baseline_manifest = other.json('manifest.json')[0] or {}
        keys = ('task_id', 'task_hash', 'benchmark', 'benchmark_version', 'upstream_revision',
                'fixture', 'fixture_hash', 'verifier_hash', 'upstream_verifier_hash', 'verifier_patch',
                'image_ids', 'port_map', 'policy', 'mode', 'models',
                'context_limits', 'budget', 'tuning',
                'continuation', 'system_prompt_hash', 'code_hash', 'process_scoring')
        differences = {key: {'current': preview(current_manifest.get(key)),
                             'baseline': preview(baseline_manifest.get(key))} for key in keys
                       if current_manifest.get(key) != baseline_manifest.get(key)}
        task_equal = self.json('task.json')[0] == other.json('task.json')[0]
        return {'baseline': other.run, 'current_grade': left['grade'], 'baseline_grade': right['grade'],
                'current_result': left['result'], 'baseline_result': right['result'],
                'current_calls': left['calls'], 'baseline_calls': right['calls'],
                'configuration_differences': differences,
                'task_equal': task_equal if self.json('task.json')[1] == 'available' and
                               other.json('task.json')[1] == 'available' else None,
                'grade_final': {'current': left['grade_final'], 'baseline': right['grade_final']},
                'interpretation': 'Descriptive comparison only; a single checkpoint continuation is not a success-rate estimate.'}

    def query(self, *, view='summary', max_bytes=DEFAULT_BYTES, cursor=0, limit=20,
              cycle=None, kind=None, file=None, pointer='', line=None, baseline=None,
              call_id=None, attempt_id=None, span_id=None, observation_id=None):
        budget = int(max_bytes)
        if not 4000 <= budget <= MAX_BYTES:
            raise ValueError('max_bytes must be 4000..64000')
        limit = int(limit)
        if ':' in str(cursor):
            if view != 'step' or not re.fullmatch(r'[0-9]+:[0-9]+', str(cursor)):
                raise ValueError('invalid multi-source cursor')
        else:
            cursor = int(cursor)
            if cursor < 0:
                raise ValueError('cursor must be nonnegative')
        if not 1 <= limit <= 100:
            raise ValueError('cursor must be nonnegative; limit must be 1..100')
        cycle = int(cycle) if cycle is not None else None
        line = int(line) if line is not None else None
        if view == 'summary':
            data = self.summary()
        elif view == 'compare':
            if not baseline:
                raise ValueError('compare requires baseline')
            data = self.compare(baseline)
        elif view == 'artifact':
            data = self.artifact(file=file, pointer=pointer, line=line, cursor=cursor, limit=limit,
                                 budget=budget)
        elif view in LOGS or view == 'step':
            data = self.page(view, cycle=cycle, kind=kind, cursor=cursor, limit=limit, budget=budget,
                             filters={'call_id': call_id, 'attempt_id': attempt_id,
                                      'span_id': span_id, 'observation_id': observation_id})
        else:
            raise ValueError('unknown diagnostics view')
        result = {'schema_version': 1, 'run': self.run, 'view': view, 'max_bytes': budget,
                  'data': data, 'truncated': False}
        if len(encode(result)) > budget:
            # Lists/pages are bounded at construction; summaries can vary by configuration.
            # Preserve score/stop/state and explicitly direct omitted sections to field queries.
            result['truncated'] = True
            for key in ('configuration', 'configuration_differences', 'log_health', 'artifacts',
                        'last_event', 'last_failure_event', 'inflight', 'evidence_limits', 'next_queries'):
                if key in data and len(encode(result)) > budget:
                    data[key] = {'omitted': 'response_byte_budget',
                                 'expand': 'use artifact/step/calls views for selected evidence'}
            if len(encode(result)) > budget:
                result['data'] = preview(data, chars=160, depth=2)
            if len(encode(result)) > budget:
                # Safe hard fallback for pathological metadata; never emit invalid/truncated JSON.
                result['data'] = {key: preview(data.get(key), chars=120, depth=1) for key in
                                  ('result', 'grade', 'lifecycle', 'grade_final', 'report_status')}
                result['omitted'] = 'Response too large; request a selected artifact field.'
        if len(encode(result)) > budget:
            raise ValueError('run id or selected field exceeds response budget')
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='runs')
    parser.add_argument('--run', required=True)
    parser.add_argument('--view', default='summary', choices=['summary', 'step', 'artifact', 'compare', *LOGS])
    for name in ('file', 'pointer', 'kind', 'baseline', 'call-id', 'attempt-id', 'span-id', 'observation-id'):
        parser.add_argument('--' + name)
    for name in ('cycle', 'line'):
        parser.add_argument('--' + name, type=int)
    parser.add_argument('--cursor', default='0')
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--max-bytes', type=int, default=DEFAULT_BYTES)
    parser.add_argument('--output', help='Save this bounded partial report instead of printing it')
    args = vars(parser.parse_args(argv))
    root, run, output = args.pop('root'), args.pop('run'), args.pop('output')
    args['pointer'] = args['pointer'] or ''
    try:
        content = encode(Diagnostics(root, run).query(**args))
    except (ValueError, KeyError, IndexError, OSError) as exc:
        parser.error(str(exc))
    if output:
        target = Path(output)
        descriptor = target.open('wb')
        with descriptor:
            target.chmod(0o600)
            descriptor.write(content)
    else:
        print(content.decode())


if __name__ == '__main__':
    main()

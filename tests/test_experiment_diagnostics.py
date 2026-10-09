"""Codex can investigate selected evidence without ingesting a complete trace."""
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

import pytest

from jev_browser.diagnostics import Diagnostics, encode
from jev_browser.inspector import Store, make_server
from jev_browser.observability import write_json
from jev_browser.run_status import register_phase, status_path


def run_fixture(root, name='trial'):
    path = root / name
    path.mkdir()
    write_json(path / 'manifest.json', {'task_id': 'business_031', 'budget': {'max_actions': 600}})
    write_json(path / 'task.json', {'objective': 'Original goal'})
    write_json(path / 'report.json', {'result': {'status': 'failed', 'reason': 'readback failed'},
                                    'grade': {'earned': 4, 'total': 15, 'score': 4 / 15,
                                              'data_valid': True, 'strict_success': False}})
    return path


def log(path, name, rows):
    (path / name).write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows))


def test_pages_keep_all_matching_records_with_unicode_byte_bounds(tmp_path):
    path = run_fixture(tmp_path)
    log(path, 'trajectory.jsonl', [{'cycle': i % 2, 'kind': 'action', 'event_id': i,
                                   'text': '中文\n' * 30_000} for i in range(17)])
    api = Diagnostics(tmp_path, 'trial')
    cursor, ids = 0, []
    while cursor is not None:
        response = api.query(view='events', cycle=1, cursor=cursor, max_bytes=4000, limit=2)
        assert len(encode(response)) <= 4000
        assert not response['truncated']
        ids.extend(item['record']['event_id'] for item in response['data']['items'])
        cursor = response['data']['next_cursor']
    assert ids == list(range(1, 17, 2))
    response = api.query(view='events', cycle=0, limit=1)
    assert response['data']['items'][0]['expand'] == {
        'view': 'artifact', 'file': 'trajectory.jsonl', 'line': 0, 'pointer': ''}


def test_selected_strings_and_children_can_be_read_without_omissions(tmp_path):
    path = run_fixture(tmp_path)
    text = '中文\n\t"\\' * 20_000
    write_json(path / 'memory.json', {'a/b~c': text, 'other': list(range(51))})
    api = Diagnostics(tmp_path, 'trial')
    cursor, chunks = 0, []
    while cursor is not None:
        response = api.query(view='artifact', file='memory.json', pointer='/a~1b~0c',
                             cursor=cursor, max_bytes=4000)
        assert len(encode(response)) <= 4000
        assert response['data']['snapshot'] == 'latest_only'
        chunks.append(response['data']['text'])
        cursor = response['data']['next_cursor']
    assert ''.join(chunks) == text
    cursor, keys = 0, []
    while cursor is not None:
        response = api.query(view='artifact', file='memory.json', pointer='/other',
                             cursor=cursor, max_bytes=4000, limit=9)
        keys.extend(item['key'] for item in response['data']['children'])
        cursor = response['data']['next_cursor']
    assert keys == list(range(51))


def test_summary_retains_grade_and_does_not_confuse_agent_stop_with_cleanup(tmp_path):
    path = run_fixture(tmp_path)
    register_phase(path, 'grading')
    log(path, 'trajectory.jsonl', [{'cycle': 60, 'kind': 'invalid_feedback', 'reason': 'length'}])
    with (path / 'trajectory.jsonl').open('a') as stream:
        stream.write('{"interrupted":')
    response = Diagnostics(tmp_path, 'trial').query(max_bytes=4000)
    assert len(encode(response)) <= 4000
    assert response['data']['grade']['earned'] == 4
    assert response['data']['result']['reason'] == 'readback failed'
    assert response['data']['completion_confirmed'] is False
    assert response['data']['grade_final'] is False
    detailed = Diagnostics(tmp_path, 'trial').query()['data']
    assert detailed['log_health']['trajectory.jsonl']['invalid_lines'] == 1
    assert detailed['calls']['request_artifacts'] == 0
    assert detailed['last_failure_event']['cycle'] == 60
    register_phase(path, 'finished')
    assert Diagnostics(tmp_path, 'trial').query()['data']['grade_final'] is True


def test_process_scores_are_bounded_and_final_grade_stays_separate(tmp_path):
    path = run_fixture(tmp_path)
    rows = [{'phase': 'baseline', 'data_valid': True, 'earned': 1, 'total': 15},
            {'phase': 'intermediate', 'data_valid': True, 'earned': 5, 'total': 15,
             'delta_earned': 4, 'cycle': 40, 'checks': [{'label': '业务' * 9000}]}]
    log(path, 'process-scores.jsonl', rows)
    with (path / 'process-scores.jsonl').open('a') as stream:
        stream.write('{"unfinished":')
    api = Diagnostics(tmp_path, 'trial')
    summary = api.query(max_bytes=4000)
    assert len(encode(summary)) <= 4000
    data = api.query()['data']
    assert data['grade']['earned'] == 4
    assert data['process_scoring']['baseline']['earned'] == 1
    assert data['process_scoring']['latest']['delta_earned'] == 4
    assert data['log_health']['process-scores.jsonl']['invalid_lines'] == 1
    page = api.query(view='scores', max_bytes=4000, limit=1)['data']
    assert page['items'][0]['record']['phase'] == 'baseline'
    assert page['next_cursor'] == 1
    expansion = data['process_scoring']['latest']['expand']
    selected = api.query(**{**expansion, 'pointer': '/checks/0/label'}, max_bytes=4000)
    assert len(encode(selected)) <= 4000 and selected['data']['next_cursor'] is not None
    assert Store(tmp_path).data('trial')['process_scores'][1]['earned'] == 5


def test_failed_attempts_and_starts_are_joined_by_attempt_id(tmp_path):
    path = run_fixture(tmp_path)
    log(path, 'model-request-starts.jsonl', [
        {'attempt_id': 'first', 'call_id': 'logical', 'cycle': 60},
        {'attempt_id': 'pending', 'call_id': 'logical', 'cycle': 60}])
    log(path, 'model-calls.jsonl', [{'attempt_id': 'first', 'call_id': 'logical', 'cycle': 60,
                                  'status': 503, 'model': 'ds', 'kind': 'dynamic_readback',
                                  'finish_reason': 'length', 'response_content_chars': 0,
                                  'payload_sizes': {'total_bytes': 48000}}])
    api = Diagnostics(tmp_path, 'trial')
    response = api.query()['data']
    assert response['calls']['errors'] == {'503': 1}
    assert response['calls']['finish_reasons'] == {'length': 1}
    assert response['calls']['empty_content_responses'] == 1
    assert response['calls']['largest_requests_by_kind']['dynamic_readback']['bytes'] == 48000
    assert response['inflight']['pending']['cycle'] == 60
    assert 'first' not in response['inflight']
    step = api.query(view='step', cycle=60)['data']
    assert {item['file'] for item in step['items']} == {
        'model-calls.jsonl', 'model-request-starts.jsonl'}
    assert len(api.query(view='starts', attempt_id='pending')['data']['items']) == 1


def test_step_cursor_does_not_shift_when_an_earlier_log_grows(tmp_path):
    path = run_fixture(tmp_path)
    log(path, 'trajectory.jsonl', [{'cycle': 60, 'event_id': i} for i in range(3)])
    log(path, 'model-calls.jsonl', [{'cycle': 60, 'attempt_id': str(i)} for i in range(3)])
    api = Diagnostics(tmp_path, 'trial')
    first = api.query(view='step', cycle=60, limit=4)['data']
    cursor = first['next_cursor']
    assert cursor == '1:1'
    with (path / 'trajectory.jsonl').open('a') as stream:
        stream.write(json.dumps({'cycle': 60, 'event_id': 3}) + '\n')
    second = api.query(view='step', cycle=60, cursor=cursor)['data']
    assert [item['record']['attempt_id'] for item in second['items']] == ['1', '2']
    assert second['next_cursor'] is None


def test_encoded_json_fields_and_credentials_are_safe(tmp_path):
    path = run_fixture(tmp_path)
    write_json(path / 'task.json', {'message': json.dumps({'goal': 'ready', 'api_key': 'secret'})})
    api = Diagnostics(tmp_path, 'trial')
    assert api.query(view='artifact', file='task.json', pointer='/message/goal')['data']['text'] == 'ready'
    assert api.query(view='artifact', file='task.json', pointer='/message/api_key')['data']['text'] == '[redacted]'


def test_compare_reports_changed_budgets_models_and_checkpoints(tmp_path):
    path = run_fixture(tmp_path)
    baseline = run_fixture(tmp_path, 'baseline')
    write_json(path / 'manifest.json', {'models': {'policy': {'model': 'ds'}},
                                      'budget': {'max_actions': 600}, 'continuation': {'source': 'A'}})
    write_json(baseline / 'manifest.json', {'models': {'policy': {'model': 'jev'}},
                                          'budget': {'max_actions': 300}, 'continuation': {'source': 'B'}})
    response = Diagnostics(tmp_path, 'trial').query(view='compare', baseline='baseline')['data']
    assert response['task_equal'] is True
    assert set(response['configuration_differences']) == {'budget', 'models', 'continuation'}


def test_same_task_score_comparison_exposes_changed_benchmark_and_oracle(tmp_path):
    current, baseline = run_fixture(tmp_path), run_fixture(tmp_path, 'baseline')
    for path, version in ((baseline, 'legacy'), (current, 'v1.1')):
        manifest = json.loads((path / 'manifest.json').read_text())
        manifest.update(benchmark_version=version, upstream_revision=f'revision-{version}',
                        verifier_hash=f'oracle-{version}', fixture_hash=f'fixture-{version}')
        write_json(path / 'manifest.json', manifest)
    api = Diagnostics(tmp_path, 'trial')
    assert api.query()['data']['configuration']['benchmark_version'] == 'v1.1'
    response = api.query(view='compare', baseline='baseline')['data']
    assert response['task_equal'] is True
    assert set(response['configuration_differences']) == {
        'benchmark_version', 'upstream_revision', 'verifier_hash', 'fixture_hash'}
    assert response['current_grade']['earned'] == 4


def test_file_run_and_symlink_boundaries(tmp_path):
    path = run_fixture(tmp_path)
    outside = tmp_path.parent / (tmp_path.name + '-private')
    outside.mkdir()
    write_json(outside / 'task.json', {'secret': True})
    (path / 'memory.json').symlink_to(outside / 'task.json')
    (tmp_path / 'escape').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        Diagnostics(tmp_path, '../' + outside.name)
    with pytest.raises(ValueError):
        Diagnostics(tmp_path, 'escape')
    api = Diagnostics(tmp_path, 'trial')
    for name in ('../task.json', 'console.log', 'memory.json', 'model-artifacts/../../task.json'):
        with pytest.raises(ValueError):
            api.query(view='artifact', file=name)


def test_registry_outside_output_and_pid_reuse(tmp_path, monkeypatch):
    from jev_browser import run_status

    output = tmp_path / 'empty-task'
    output.mkdir()
    monkeypatch.setattr(run_status, 'process_identity', lambda pid: 'first')
    register_phase(output, 'setup')
    assert list(output.iterdir()) == []
    assert run_status.read_status(output)['process_alive'] is True
    monkeypatch.setattr(run_status, 'process_identity', lambda pid: 'second')
    assert run_status.read_status(output)['phase'] == 'interrupted'
    register_phase(output, 'finished')
    monkeypatch.setattr(run_status, 'process_identity', lambda pid: 'third')
    assert run_status.read_status(output)['phase'] == 'finished'
    assert status_path(output).exists()


def test_launcher_includes_registered_manual_task(tmp_path):
    path = run_fixture(tmp_path)
    register_phase(path, 'running', slot=0)
    store = Store(tmp_path)
    response = store.launch_status()
    assert response['running'] is True and response['id'] is None
    assert response['activities'][0]['id'] == 'trial'
    register_phase(path, 'finished')
    assert store.launch_status()['running'] is False


def test_activity_scan_prunes_run_artifacts_and_finds_nested_trials(tmp_path):
    from jev_browser.run_status import activities

    trial = tmp_path / 'study' / 'trial-000'
    trial.mkdir(parents=True)
    path = run_fixture(trial)
    register_phase(path, 'running')
    # A marker file hidden in a preview directory must not become a real run.
    hidden = path / 'preview' / 'fake'
    hidden.mkdir(parents=True)
    register_phase(hidden, 'running')
    rows = activities(tmp_path)
    assert [row['id'] for row in rows] == ['study/trial-000/trial']


def test_cli_and_http_use_identical_bounded_query(tmp_path):
    run_fixture(tmp_path)
    arguments = ['--root', str(tmp_path), '--run', 'trial', '--view', 'artifact',
                 '--file', 'report.json', '--pointer', '/grade/earned', '--max-bytes', '4000']
    result = subprocess.run([sys.executable, '-m', 'jev_browser.diagnostics', *arguments],
                            check=True, capture_output=True, env={**os.environ,
                            'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')})
    server = make_server(tmp_path, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f'http://127.0.0.1:{server.server_port}/api/diagnostics?'
        query = {'id': 'trial', 'view': 'artifact', 'file': 'report.json',
                 'pointer': '/grade/earned', 'max_bytes': 4000}
        with urlopen(base + urlencode(query)) as response:
            content = response.read()
        assert json.loads(content) == json.loads(result.stdout)
        assert len(content) <= 4000
        with pytest.raises(HTTPError) as error:
            urlopen(base + urlencode({**query, 'max_bytes': 100}))
        assert error.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

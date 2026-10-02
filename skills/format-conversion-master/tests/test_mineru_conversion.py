from __future__ import annotations

import io
import json
import subprocess
import sys
import threading
import urllib.error
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import format_conversion as conversion
import mineru_workflow as workflow
from utils.scripts.mineru_client import MinerUClient, MinerUError
from utils.scripts.project_env import project_env
from utils.scripts.safe_archive import extract_zip


def make_source(root, extension='pdf'):
    source = root / ('source.' + extension)
    if extension == 'docx':
        with zipfile.ZipFile(source, 'w') as archive:
            archive.writestr('[Content_Types].xml', '<Types/>')
            archive.writestr('word/document.xml', '<document/>')
    else:
        source.write_bytes(b'%PDF-1.7\n%%EOF' if extension == 'pdf' else bytes.fromhex('d0cf11e0a1b11ae1') + b'SYNTHETIC')
    return source


@pytest.fixture
def service(monkeypatch):
    recorded = {'requests': [], 'uploaded': False, 'queries': 0, 'mode': 'success'}
    package = io.BytesIO()
    with zipfile.ZipFile(package, 'w') as archive:
        archive.writestr('full.md', '# Example\n\n![figure](images/figure.png)\n')
        archive.writestr('images/figure.png', b'SYNTHETIC_IMAGE')
        archive.writestr('layout.json', '{}')
        archive.writestr('example_content_list.json', '[]')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, data, code=200):
            body = json.dumps(data).encode() if isinstance(data, dict) else data
            self.send_response(code)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            recorded['requests'].append(('POST', self.path, dict(self.headers)))
            recorded['payload'] = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            self.reply({'code': 0, 'data': {'batch_id': 'MOCK_batch', 'file_urls': [base + '/upload']}})

        def do_PUT(self):
            recorded['requests'].append(('PUT', self.path, dict(self.headers)))
            recorded['bytes'] = self.rfile.read(int(self.headers['Content-Length']))
            recorded['uploaded'] = True
            self.reply(b'')

        def do_GET(self):
            recorded['requests'].append(('GET', self.path, dict(self.headers)))
            if self.path == '/result.zip':
                self.reply(package.getvalue())
                return
            recorded['queries'] += 1
            remote = 'waiting-file' if not recorded['uploaded'] else 'failed' if recorded['mode'] == 'failed' else 'pending' if recorded['mode'] == 'pending' or recorded['queries'] == 1 else 'done'
            data_id = recorded['payload']['files'][0]['data_id']
            self.reply({'code': 0, 'data': {'extract_result': [{'data_id': data_id,
                'state': remote, 'full_zip_url': base + '/result.zip'}]}})

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    base = f'http://127.0.0.1:{server.server_port}'
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(MinerUClient, 'base_url', base + '/api/v4')
    # Only this synthetic local service bypasses production HTTPS enforcement.
    monkeypatch.setattr(MinerUClient, '_https', staticmethod(lambda url: None))
    monkeypatch.setenv('MINERU_API_KEY', 'SYNTHETIC_MINERU_CREDENTIAL')
    try:
        yield recorded
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize('extension', ['doc', 'docx', 'pdf'])
def test_local_http_all_endpoints_and_complete_package(tmp_path, service, extension):
    source = make_source(tmp_path, extension)
    state = conversion.run(tmp_path, str(source), 'md', mineru_options={'poll_interval': 0.01, 'page_ranges': '1', 'is_ocr': True})
    assert state['status'] == 'completed', state.get('error')
    package = Path(state['publication']['package_path'])
    assert package == tmp_path / 'outputs' / conversion.WORKFLOW / 'runs' / state['run_id'] / 'mineru'
    assert {'full.md', 'layout.json', 'images/figure.png', 'example_content_list.json'} == set(state['verification']['files'])
    assert conversion.status(tmp_path, state['run_id'])['status'] == 'completed'
    assert conversion.resume(tmp_path, state['run_id']) == state
    assert conversion.verify(tmp_path, state['run_id'])['file_count'] == 4
    assert service['bytes'] == source.read_bytes()
    payload = service['payload']
    assert payload['files'][0]['page_ranges'] == '1' and payload['files'][0]['is_ocr'] is True
    assert 'page_ranges' not in payload and 'poll_interval' not in payload
    assert any(method == 'POST' for method, _, _ in service['requests'])
    for method, path, headers in service['requests']:
        if path in {'/upload', '/result.zip'}:
            assert 'Authorization' not in headers
        if method == 'PUT':
            assert not any(name.lower() == 'content-type' for name in headers)
    for path in (tmp_path / 'logs').rglob('*.json'):
        assert 'SYNTHETIC_MINERU_CREDENTIAL' not in path.read_text()
        assert 'full_zip_url' not in path.read_text() and 'http://127.0.0.1:' not in path.read_text()
    (package / 'layout.json').write_text('{"changed":true}')
    with pytest.raises(Exception):
        conversion.verify(tmp_path, state['run_id'])


def test_timeout_resume_reuses_batch(tmp_path, service):
    service['mode'] = 'pending'
    source = make_source(tmp_path)
    state = conversion.run(tmp_path, str(source), 'md', mineru_options={'poll_timeout': 1, 'poll_interval': 0.1})
    assert state['status'] == 'paused_timeout'
    service['mode'] = 'success'
    resumed = conversion.resume(tmp_path, state['run_id'])
    assert resumed['status'] == 'completed'
    assert sum(method == 'POST' for method, _, _ in service['requests']) == 1


def test_submission_unknown_does_not_resubmit(tmp_path, monkeypatch):
    monkeypatch.setenv('MINERU_API_KEY', 'SYNTHETIC_MINERU_CREDENTIAL')
    calls = []
    def uncertain(*args):
        calls.append(True)
        raise MinerUError('network', 'SYNTHETIC network failure')
    monkeypatch.setattr(MinerUClient, 'request_upload', uncertain)
    state = conversion.run(tmp_path, str(make_source(tmp_path)), 'md')
    assert state['status'] == 'paused_submission_unknown'
    assert conversion.resume(tmp_path, state['run_id'])['status'] == 'paused_submission_unknown'
    assert len(calls) == 1


def test_missing_key_resume_and_invalid_inputs(tmp_path, monkeypatch, service):
    monkeypatch.delenv('MINERU_API_KEY')
    source = make_source(tmp_path)
    state = conversion.run(tmp_path, str(source), 'md')
    assert state['status'] == 'paused_configuration'
    (tmp_path / '.env').write_text('MINERU_API_KEY=SYNTHETIC_LOCAL_CREDENTIAL\n')
    assert conversion.resume(tmp_path, state['run_id'])['status'] == 'completed'
    with pytest.raises(conversion.ConversionError):
        conversion.run(tmp_path, 'https://example.invalid/example.pdf', 'md')
    with pytest.raises(conversion.ConversionError):
        conversion.run(tmp_path, str(source), 'pdf')
    with pytest.raises(conversion.ConversionError):
        conversion.run(tmp_path, str(source), 'md', 'external.md')
    source.write_bytes(b'invalid')
    assert conversion.run(tmp_path, str(source), 'md')['status'] == 'paused_input'


@pytest.mark.parametrize('name', ['../escape.md', '/absolute.md', 'C:/escape.md', 'images\\escape.md'])
def test_unsafe_zip_rejected_before_extraction(tmp_path, name):
    path = tmp_path / 'bad.zip'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr(name, 'SYNTHETIC')
    if '\\' in name:
        # zipfile normalizes Windows paths while writing; restore the hostile raw member name.
        path.write_bytes(path.read_bytes().replace(name.replace('\\', '/').encode(), name.encode()))
    with pytest.raises(ValueError):
        extract_zip(path, tmp_path / 'extracted')
    assert not (tmp_path / 'extracted').exists()


def test_zip_limit_and_missing_resource(tmp_path):
    path = tmp_path / 'package.zip'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('full.md', '![image](missing.png)')
    with pytest.raises(ValueError):
        extract_zip(path, tmp_path / 'too_large', max_bytes=1)
    extract_zip(path, tmp_path / 'package')
    with pytest.raises(ValueError):
        workflow.verify_package(tmp_path / 'package')


def test_env_precedence_and_https(monkeypatch, tmp_path):
    monkeypatch.delenv('MINERU_API_KEY', raising=False)
    (tmp_path / '.env').write_text('MINERU_API_KEY="SYNTHETIC_LOCAL"\n')
    assert project_env(tmp_path, 'MINERU_API_KEY') == 'SYNTHETIC_LOCAL'
    monkeypatch.setenv('MINERU_API_KEY', 'SYNTHETIC_PROCESS')
    assert project_env(tmp_path, 'MINERU_API_KEY') == 'SYNTHETIC_PROCESS'
    with pytest.raises(MinerUError):
        MinerUClient._https('http://example.invalid/unsafe')


def test_auth_error_is_sanitized(monkeypatch):
    def denied(*args, **kwargs):
        raise urllib.error.HTTPError('https://example.invalid/?SYNTHETIC_SECRET', 401, 'SYNTHETIC_SECRET', {}, None)
    monkeypatch.setattr('urllib.request.OpenerDirector.open', denied)
    with pytest.raises(MinerUError) as error:
        MinerUClient('SYNTHETIC_SECRET').query('MOCK_batch', 'MOCK_data')
    assert error.value.code == 'authentication' and 'SYNTHETIC_SECRET' not in str(error.value)


def test_real_cli_interfaces(tmp_path, monkeypatch):
    monkeypatch.delenv('MINERU_API_KEY', raising=False)
    cli = Path(__file__).resolve().parents[1] / 'scripts' / 'cli.py'
    def invoke(*args):
        result = subprocess.run([sys.executable, str(cli), '--root', str(tmp_path), *args], capture_output=True, text=True, encoding='utf-8')
        return result.returncode, json.loads(result.stdout)
    source = make_source(tmp_path)
    code, formats = invoke('list-formats')
    assert code == 0 and len(formats['conversions']) == 5
    request = tmp_path / 'request.json'
    assert invoke('create-request', '--input', str(source), '--to', 'md', '--request-file', str(request), '--language', 'ch')[0] == 0
    code, state = invoke('start', '--request-file', str(request))
    assert code == 3 and state['status'] == 'paused_configuration'
    assert invoke('status', '--run-id', state['run_id'])[0] == 3
    assert invoke('resume', '--run-id', state['run_id'])[0] == 3
    assert invoke('verify', '--run-id', state['run_id'])[0] == 4
    assert invoke('start', '--input', 'bad.txt', '--to', 'md')[0] == 2


def test_bundle_conflict_and_publish_resume(tmp_path, service, monkeypatch):
    original = workflow.verify_package
    injected = []
    def conflict(package):
        actual = original(package)
        if not injected:
            run_id = package.parents[1].name
            target = tmp_path / 'outputs' / conversion.WORKFLOW / 'runs' / run_id / 'mineru'
            target.mkdir(parents=True)
            (target / 'keep.txt').write_text('SYNTHETIC_EXISTING')
            injected.append(target)
        return actual
    monkeypatch.setattr(workflow, 'verify_package', conflict)
    state = conversion.run(tmp_path, str(make_source(tmp_path)), 'md', mineru_options={'poll_interval': 0.01})
    assert state['status'] == 'paused_output_conflict'
    assert (injected[0] / 'keep.txt').read_text() == 'SYNTHETIC_EXISTING'
    # Simulate user preserving the conflicting directory elsewhere before resume.
    injected[0].rename(injected[0].with_name('preserved'))
    assert conversion.resume(tmp_path, state['run_id'])['status'] == 'completed'
    assert sum(method == 'POST' for method, _, _ in service['requests']) == 1


def test_epub_markdown_output_conflict_is_recoverable(tmp_path):
    from test_format_conversion import _make_epub
    source = tmp_path / 'book.epub'
    _make_epub(source)
    target = tmp_path / 'existing.md'
    target.write_text('SYNTHETIC_EXISTING')
    state = conversion.run(tmp_path, str(source), 'md', str(target))
    assert state['status'] == 'paused_output_conflict' and state['resume_stage'] == 'rendering'
    replacement = tmp_path / 'new.md'
    resumed = conversion.resume(tmp_path, state['run_id'], str(replacement))
    assert resumed['status'] == 'completed' and target.read_text() == 'SYNTHETIC_EXISTING'
    assert conversion.verify(tmp_path, state['run_id'])['sha256'] == resumed['publication']['sha256']


def test_request_snapshot_change_pauses_and_can_be_restored(tmp_path, service, monkeypatch):
    monkeypatch.delenv('MINERU_API_KEY')
    state = conversion.run(tmp_path, str(make_source(tmp_path)), 'md', mineru_options={'poll_interval': 0.01})
    request = tmp_path / 'logs' / conversion.WORKFLOW / 'runs' / state['run_id'] / 'request.json'
    original = request.read_bytes()
    modified = json.loads(original)
    modified['mineru']['model_version'] = 'pipeline'
    request.write_text(json.dumps(modified))
    monkeypatch.setenv('MINERU_API_KEY', 'SYNTHETIC_RESTORED')
    paused = conversion.resume(tmp_path, state['run_id'])
    assert paused['status'] == 'paused_input' and paused['error']['code'] == 'request_changed'
    assert not service['requests']
    request.write_bytes(original)
    assert conversion.resume(tmp_path, state['run_id'])['status'] == 'completed'
    request.write_text(json.dumps(modified))
    with pytest.raises(ValueError, match='快照'):
        conversion.verify(tmp_path, state['run_id'])


def test_poll_timeout_excludes_upload_time(tmp_path, service, monkeypatch):
    clock = [0]
    monkeypatch.setattr(workflow.time, 'monotonic', lambda: clock[0])
    original = MinerUClient.upload
    def slow_upload(client, batch, source):
        original(client, batch, source)
        clock[0] += 1000
    monkeypatch.setattr(MinerUClient, 'upload', slow_upload)
    result = conversion.run(tmp_path, str(make_source(tmp_path)), 'md', mineru_options={'poll_timeout': 1, 'poll_interval': 0.01})
    assert result['status'] == 'completed'


def test_signed_upload_failure_is_not_api_authentication(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise urllib.error.HTTPError('https://example.invalid/upload', 403, 'SYNTHETIC', {}, None)
    monkeypatch.setattr('urllib.request.OpenerDirector.open', denied)
    client = MinerUClient('SYNTHETIC_CREDENTIAL')
    client.upload_urls['MOCK_batch'] = 'https://example.invalid/upload'
    with pytest.raises(MinerUError) as error:
        client.upload('MOCK_batch', make_source(tmp_path))
    assert error.value.code == 'upload_expired'


def test_api_redirect_never_forwards_credentials():
    import urllib.request
    from utils.scripts.mineru_client import _SafeRedirect
    request = urllib.request.Request('https://mineru.net/api/v4/extract-results/batch/MOCK_batch', headers={'Authorization': 'Bearer SYNTHETIC_CREDENTIAL'})
    with pytest.raises(MinerUError):
        _SafeRedirect().redirect_request(request, None, 302, '', {}, 'https://example.invalid/redirect')
    request = urllib.request.Request('https://example.invalid/download')
    with pytest.raises(MinerUError):
        _SafeRedirect().redirect_request(request, None, 302, '', {}, 'http://example.invalid/redirect')


def test_missing_dotenv_is_recoverable_and_does_not_block_epub(tmp_path, monkeypatch, capsys):
    import builtins
    original = builtins.__import__
    def missing(name, *args, **kwargs):
        if name == 'dotenv':
            raise ModuleNotFoundError('SYNTHETIC missing dotenv')
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', missing)
    monkeypatch.delenv('MINERU_API_KEY', raising=False)
    source = make_source(tmp_path)
    assert conversion.main(['run', '--root', str(tmp_path), '--input', str(source), '--to', 'md']) == 6
    state = json.loads(capsys.readouterr().out)
    assert state['status'] == 'paused_dependency'
    from test_format_conversion import _make_epub
    epub = tmp_path / 'book.epub'
    _make_epub(epub)
    assert conversion.run(tmp_path, str(epub), 'md')['status'] == 'completed'


def test_same_second_run_ids_use_numeric_suffix(tmp_path, monkeypatch):
    from datetime import datetime
    from utils.scripts.timestamp import unique_filename_timestamp
    fixed = datetime(2026, 10, 2, 12, 0, 0)
    monkeypatch.setattr(conversion, 'unique_filename_timestamp', lambda existing: unique_filename_timestamp(existing, fixed))
    monkeypatch.delenv('MINERU_API_KEY', raising=False)
    source = make_source(tmp_path)
    first = conversion.run(tmp_path, str(source), 'md')
    second = conversion.run(tmp_path, str(source), 'md')
    assert first['run_id'] == '20261002T120000' and second['run_id'] == '20261002T120000_1'


@pytest.mark.parametrize('field', ['receipt', 'manifest', 'extra_file'])
def test_completion_gate_rechecks_receipt_manifest_and_file_set(tmp_path, service, field):
    state = conversion.run(tmp_path, str(make_source(tmp_path)), 'md', mineru_options={'poll_interval': 0.01})
    run = tmp_path / 'logs' / conversion.WORKFLOW / 'runs' / state['run_id']
    if field == 'extra_file':
        (Path(state['publication']['package_path']) / 'extra.json').write_text('{}')
    else:
        path = run / ('publication.json' if field == 'receipt' else 'manifest.json')
        payload = json.loads(path.read_text())
        payload['status'] = 'running'
        path.write_text(json.dumps(payload))
    with pytest.raises(Exception):
        conversion.verify(tmp_path, state['run_id'])
    with pytest.raises(Exception):
        conversion.resume(tmp_path, state['run_id'])


@pytest.mark.parametrize('pages', ['0', '1---2', ',1', '1,', '---'])
def test_invalid_page_ranges_are_rejected(pages):
    with pytest.raises(Exception):
        conversion.validate_json_schema({'input_file': 'test.pdf', 'target_format': 'md', 'mineru': {'page_ranges': pages}}, conversion.REQUEST_SCHEMA)


def test_signed_download_failure_does_not_request_api_key(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise urllib.error.HTTPError('https://example.invalid/result.zip', 403, 'SYNTHETIC', {}, None)
    monkeypatch.setattr('urllib.request.OpenerDirector.open', denied)
    with pytest.raises(MinerUError) as error:
        MinerUClient('SYNTHETIC').download('https://example.invalid/result.zip', tmp_path / 'result.zip')
    assert error.value.code == 'download'
    assert not (tmp_path / 'result.part').exists()


def test_published_copy_is_verified_before_rename(tmp_path, service, monkeypatch):
    import utils.scripts.file_publish as publisher
    original = publisher.shutil.copytree
    def corrupt(source, target, *args, **kwargs):
        result = original(source, target, *args, **kwargs)
        (Path(target) / 'layout.json').write_text('{"SYNTHETIC":true}')
        return result
    monkeypatch.setattr(publisher.shutil, 'copytree', corrupt)
    state = conversion.run(tmp_path, str(make_source(tmp_path)), 'md', mineru_options={'poll_interval': 0.01})
    assert state['status'] == 'paused_verification'
    target = tmp_path / 'outputs' / conversion.WORKFLOW / 'runs' / state['run_id'] / 'mineru'
    assert not target.exists()
    monkeypatch.setattr(publisher.shutil, 'copytree', original)
    assert conversion.resume(tmp_path, state['run_id'])['status'] == 'completed'


def test_uncertain_submission_can_be_reconciled_by_batch_id(tmp_path, service, monkeypatch):
    original = MinerUClient.request_upload
    def lost_response(client, *args):
        original(client, *args)
        raise MinerUError('network', 'SYNTHETIC lost response')
    monkeypatch.setattr(MinerUClient, 'request_upload', lost_response)
    state = conversion.run(tmp_path, str(make_source(tmp_path)), 'md', mineru_options={'poll_interval': 0.01})
    assert state['status'] == 'paused_submission_unknown'
    with pytest.raises(conversion.ConversionError):
        conversion.resume(tmp_path, state['run_id'], batch_id='../invalid')
    # Synthetic user reconciliation: the matching remote document was already uploaded.
    service['uploaded'] = True
    assert conversion.resume(tmp_path, state['run_id'], batch_id='MOCK_batch')['status'] == 'completed'
    assert sum(method == 'POST' for method, _, _ in service['requests']) == 1


def test_interrupted_upload_pauses_without_recreating_task(tmp_path, service, monkeypatch):
    def interrupted(*args):
        raise MinerUError('network', 'SYNTHETIC interrupted upload')
    monkeypatch.setattr(MinerUClient, 'upload', interrupted)
    state = conversion.run(tmp_path, str(make_source(tmp_path)), 'md')
    assert state['status'] == 'paused_network'
    resumed = conversion.resume(tmp_path, state['run_id'])
    assert resumed['status'] == 'paused_upload_expired'
    assert sum(method == 'POST' for method, _, _ in service['requests']) == 1


def test_remote_failure_queries_existing_batch_after_resume(tmp_path, service):
    service['mode'] = 'failed'
    state = conversion.run(tmp_path, str(make_source(tmp_path)), 'md', mineru_options={'poll_interval': 0.01})
    assert state['status'] == 'paused_remote'
    service['mode'] = 'success'
    assert conversion.resume(tmp_path, state['run_id'])['status'] == 'completed'
    assert sum(method == 'POST' for method, _, _ in service['requests']) == 1


@pytest.mark.parametrize('items', [None, {}, 'invalid', [{'data_id': 'OTHER_data'}]])
def test_malformed_batch_results_are_rejected(monkeypatch, items):
    client = MinerUClient('SYNTHETIC')
    monkeypatch.setattr(client, '_json', lambda *args: {'extract_result': items})
    with pytest.raises(MinerUError) as error:
        client.query('MOCK_batch', 'MOCK_data')
    assert error.value.code == 'response'

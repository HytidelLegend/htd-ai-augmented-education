"""Recoverable local DOC/DOCX/PDF conversion; publishes the complete result package."""
from __future__ import annotations

import re
import shutil
import time
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlparse

from utils.scripts.file_transaction import file_sha256
from utils.scripts.file_publish import publish_directory_without_overwrite
from utils.scripts.mineru_client import MinerUClient, MinerUError
from utils.scripts.project_env import project_env
from utils.scripts.safe_archive import extract_zip
from utils.scripts.structured_io import read_json, write_json
from utils.scripts.timestamp import iso_timestamp


STAGES = ['validating_input', 'staging_input', 'checking_configuration', 'requesting_upload',
          'uploading', 'polling', 'downloading', 'extracting', 'preparing_markdown', 'verifying', 'publishing']
PAUSES = ['paused_input', 'paused_configuration', 'paused_network', 'paused_timeout',
          'paused_remote', 'paused_archive', 'paused_verification', 'paused_output_conflict',
          'paused_submission_unknown', 'paused_upload_expired', 'paused_dependency', 'paused_error']


def validate_source(source: Path):
    if not source.is_file() or source.stat().st_size == 0:
        raise ValueError('输入文件不存在或为空')
    if source.stat().st_size > 200 * 1024**2:
        raise ValueError('MinerU 输入文件超过 200 MB')
    with source.open('rb') as handle:
        header = handle.read(8)
    if source.suffix.lower() == '.pdf' and not header.startswith(b'%PDF-'):
        raise ValueError('无效 PDF 文件头')
    if source.suffix.lower() == '.doc' and header != bytes.fromhex('d0cf11e0a1b11ae1'):
        raise ValueError('无效 DOC 文件头；仅接受二进制 Word 文档')
    if source.suffix.lower() == '.docx':
        with zipfile.ZipFile(source) as archive:
            if not {'[Content_Types].xml', 'word/document.xml'} <= set(archive.namelist()):
                raise ValueError('无效 DOCX package')


def verify_package(package: Path) -> dict:
    if package.is_symlink() or any(p.is_symlink() for p in package.rglob('*')):
        raise ValueError('MinerU 包不得包含符号链接')
    markdown = package / 'full.md'
    if not markdown.is_file() or not markdown.read_text(encoding='utf-8').strip():
        raise ValueError('MinerU 包缺少非空 UTF-8 full.md')
    text = markdown.read_text(encoding='utf-8')
    if re.search(r'<(?:html|body)\b', text, re.I):
        raise ValueError('MinerU Markdown 仍含 HTML 文档壳')
    refs = re.findall(r'!?\[[^\]]*\]\(([^\n]+?)\)', text)
    refs += re.findall(r'(?:src|href)=[\"\']([^\"\']+)[\"\']', text)
    refs += re.findall(r'^\s*\[[^\]]+\]:\s*(\S+)', text, re.M)
    for ref in refs:
        ref = ref.split(' "', 1)[0].strip('<>')
        parsed = urlparse(ref)
        if parsed.scheme or parsed.netloc or not parsed.path:
            continue
        target = (package / unquote(parsed.path)).resolve()
        if not target.is_relative_to(package.resolve()) or not target.is_file():
            raise ValueError('MinerU Markdown 本地资源缺失或引用越界')
    files = {p.relative_to(package).as_posix(): file_sha256(p)
             for p in sorted(package.rglob('*')) if p.is_file()}
    return {'sha256': files['full.md'], 'files': files, 'file_count': len(files),
            'size_bytes': markdown.stat().st_size, 'content_accuracy_verified': False}


def advance(root: Path, state: dict, conversion, output_override=None):
    store = conversion._store(root, state['run_id'])
    with store.lock():
        return _advance(root, store.load(), conversion, output_override)


def _advance(root, state, conversion, output_override):
    run_dir = conversion._run_dir(root, state['run_id'])
    store = conversion._store(root, state['run_id'])
    if state.get('request_sha256') and (not (run_dir / 'request.json').is_file() or file_sha256(run_dir / 'request.json') != state['request_sha256']):
        return store.pause(state, status='paused_input', error_code='request_changed',
            message='请求快照已变化；请恢复原快照或新建运行', resume_stage=state['status'])
    request = conversion._read_request(run_dir / 'request.json')
    if output_override or request.get('output_file'):
        raise ValueError('MinerU 完整包固定发布到对应 run 的 mineru/ 子目录，不支持 --output')
    options = {'model_version': 'vlm', 'enable_formula': True, 'enable_table': True,
               'language': 'ch', **request.get('mineru', {})}
    timeout = options.pop('request_timeout', 60)
    poll_timeout = options.pop('poll_timeout', 300)
    interval = options.pop('poll_interval', 5)
    source = root / request['input_file']
    staged = run_dir / 'inputs' / ('source' + source.suffix.lower())
    archive = run_dir / 'downloads' / 'result.zip'
    package = run_dir / 'extracted' / 'mineru'
    target = root / 'outputs' / conversion.WORKFLOW / 'runs' / state['run_id'] / 'mineru'
    client = None
    deadline = None
    while state['status'] != 'completed' and not state['status'].startswith('paused_'):
        stage = state['status']
        try:
            if stage == 'prepared':
                state = store.transition(state, 'validating_input', stage='validating_input')
                continue
            if stage == 'validating_input':
                validate_source(source)
                state = store.transition(state, 'staging_input', stage='staging_input',
                    updates={'source_path': str(source.resolve()), 'source_sha256': file_sha256(source),
                             'source_format': source.suffix.lower().lstrip('.'), 'backend': 'mineru'})
                continue
            if stage == 'staging_input':
                staged.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, staged)
                if file_sha256(staged) != state['source_sha256']:
                    raise ValueError('输入副本哈希不一致')
                state = store.transition(state, 'checking_configuration', stage='checking_configuration')
                continue
            if stage not in {'extracting', 'preparing_markdown', 'verifying', 'publishing'} and client is None:
                try:
                    client = MinerUClient(project_env(root, 'MINERU_API_KEY'), timeout)
                except ValueError as exc:
                    state = store.pause(state, status='paused_configuration', error_code='configuration',
                        message=str(exc), resume_stage=stage)
                    continue
                except ImportError:
                    state = store.pause(state, status='paused_dependency', error_code='dependency',
                        message='缺少 python-dotenv；请按 runtime/.venv/requirements.txt 配置隔离环境', resume_stage=stage)
                    continue
            if stage == 'checking_configuration':
                state = store.transition(state, 'requesting_upload', stage='requesting_upload')
            elif stage == 'requesting_upload':
                if state.get('submission_started') and not state.get('current_batch_id'):
                    raise MinerUError('submission_unknown', '上次提交结果不明确；请核对 MinerU 任务后使用 --batch-id 恢复')
                state = store.checkpoint(state, event='upload_request_started', updates={'submission_started': True})
                batch = client.request_upload(source.name, state['run_id'], options)
                state = store.transition(state, 'uploading', stage='uploading', updates={'current_batch_id': batch})
            elif stage == 'uploading':
                batch = state['current_batch_id']
                result = {'state': 'waiting-file'} if batch in client.upload_urls else client.query(batch, state['run_id'])
                if result.get('state') == 'waiting-file':
                    if batch not in client.upload_urls:
                        raise MinerUError('upload_expired', '上传地址未保留；请确认远端任务状态后新建运行')
                    if file_sha256(staged) != state['source_sha256']:
                        raise ValueError('暂存输入哈希已变化')
                    client.upload(state['current_batch_id'], staged)
                state = store.transition(state, 'polling', stage='polling', updates={'upload_completed': True})
            elif stage == 'polling':
                if deadline is None:
                    deadline = time.monotonic() + poll_timeout
                result = client.query(state['current_batch_id'], state['run_id'])
                remote = result.get('state')
                if remote == 'done':
                    state = store.transition(state, 'downloading', stage='downloading')
                elif remote == 'failed':
                    raise MinerUError('remote', 'MinerU 解析失败；请在服务端查看任务详情')
                elif remote in {'pending', 'running', 'converting', 'uploading', 'waiting-file'}:
                    if remote == 'waiting-file' and not state.get('upload_completed'):
                        raise MinerUError('upload_expired', '远端仍等待文件；请核对上传状态后新建运行')
                    progress = result.get('extract_progress', {})
                    state = store.checkpoint(state, event='mineru_progress', updates={'remote_state': remote,
                        'progress': {k: progress[k] for k in ('extracted_pages', 'total_pages') if k in progress}})
                    if time.monotonic() >= deadline:
                        raise MinerUError('timeout', '轮询时限已到；resume 将继续查询已有任务')
                    time.sleep(min(interval, max(0, deadline - time.monotonic())))
                else:
                    raise MinerUError('response', 'MinerU 返回未知任务状态')
            elif stage == 'downloading':
                result = client.query(state['current_batch_id'], state['run_id'])
                if result.get('state') != 'done' or not isinstance(result.get('full_zip_url'), str):
                    raise MinerUError('response', 'MinerU 未返回完整结果包')
                client.download(result['full_zip_url'], archive)
                state = store.transition(state, 'extracting', stage='extracting', updates={'archive_sha256': file_sha256(archive)})
            elif stage == 'extracting':
                if file_sha256(archive) != state['archive_sha256']:
                    raise ValueError('下载包哈希已变化')
                if package.exists():
                    if package.is_symlink() or not package.resolve().is_relative_to(run_dir.resolve()):
                        raise ValueError('解压暂存目录越界')
                    shutil.rmtree(package)
                extract_zip(archive, package)
                state = store.transition(state, 'preparing_markdown', stage='preparing_markdown')
            elif stage == 'preparing_markdown':
                # Preserve provider bytes and directory structure; no agent rewrite or front matter.
                state = store.transition(state, 'verifying', stage='verifying')
            elif stage == 'verifying':
                verification = verify_package(package)
                write_json(run_dir / 'qa' / 'verification.json', verification)
                state = store.transition(state, 'publishing', stage='publishing', updates={'verification': verification})
            elif stage == 'publishing':
                expected = state['verification']
                if verify_package(package) != expected:
                    raise ValueError('发布前包指纹变化')
                receipt_path = run_dir / 'publication.json'
                if target.exists():
                    if not receipt_path.is_file() or read_json(receipt_path).get('files') != expected['files'] or verify_package(target) != expected:
                        raise FileExistsError('输出目录已存在，拒绝覆盖')
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    write_json(receipt_path, {'status': 'publishing', 'files': expected['files']})
                    def verify_copy(pending):
                        if verify_package(pending) != expected:
                            raise ValueError('发布目录副本哈希变化')
                    publish_directory_without_overwrite(package, target, verifier=verify_copy)
                receipt = {'status': 'published', 'path': str(target / 'full.md'),
                    'package_path': str(target), **expected, 'published_at': iso_timestamp()}
                write_json(receipt_path, receipt)
                manifest_store = conversion._manifest(root, run_dir)
                manifest = manifest_store.load()
                for index, path in enumerate([staged, archive, run_dir / 'qa' / 'verification.json', receipt_path,
                        *sorted(p for p in target.rglob('*') if p.is_file())]):
                    manifest_store.upsert(manifest, conversion.artifact_entry(root, path, role=f'mineru_{index}', sources=[state['source_sha256']]))
                manifest_store.set_status(manifest, 'completed')
                state = store.transition(state, 'completed', stage='completed', updates={'publication': receipt})
        except MinerUError as exc:
            category = exc.code
            if stage == 'requesting_upload' and state.get('submission_started'):
                category = 'submission_unknown' if category not in {'authentication', 'api'} else category
                if category != 'submission_unknown':
                    state = store.checkpoint(state, event='submission_rejected', updates={'submission_started': False})
            pause = {'authentication': 'paused_configuration', 'api': 'paused_configuration',
                     'network': 'paused_network', 'timeout': 'paused_timeout', 'remote': 'paused_remote',
                     'submission_unknown': 'paused_submission_unknown', 'upload_expired': 'paused_upload_expired'}.get(category, 'paused_network')
            state = store.pause(state, status=pause, error_code=category, message=str(exc), resume_stage=stage)
        except FileExistsError:
            state = store.pause(state, status='paused_output_conflict', error_code='output_exists',
                message='输出目录已存在，拒绝覆盖', resume_stage=stage)
        except Exception:
            pause = 'paused_input' if stage in {'validating_input', 'staging_input'} else 'paused_archive' if stage == 'extracting' else 'paused_verification' if stage in {'verifying', 'publishing'} else 'paused_error'
            state = store.pause(state, status=pause, error_code='validation_or_runtime',
                message='文件校验或处理失败，请检查输入、包结构及依赖', resume_stage=stage)
    return state


def verify(root, state, conversion):
    manifest_store = conversion._manifest(root, conversion._run_dir(root, state['run_id']))
    manifest = manifest_store.load()
    if manifest['status'] != 'completed':
        raise ValueError('MinerU 产物清单尚未完成')
    manifest_store.verify_files(manifest)
    run_dir = conversion._run_dir(root, state['run_id'])
    if state.get('request_sha256') and file_sha256(run_dir / 'request.json') != state['request_sha256']:
        raise ValueError('请求快照哈希已变化')
    if read_json(run_dir / 'publication.json') != state['publication']:
        raise ValueError('发布回执与运行状态不一致')
    actual = verify_package(Path(state['publication']['package_path']))
    if actual != state['verification'] or actual['files'] != state['publication']['files']:
        raise ValueError('完整 MinerU 包的文件或哈希已变化')
    return actual

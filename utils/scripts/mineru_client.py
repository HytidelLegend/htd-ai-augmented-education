"""MinerU v4 transport. Never include credentials or signed URLs in errors."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse


class _RawUploadHeaders(urllib.request.BaseHandler):
    # Run after urllib's default handlers, which otherwise add a form Content-Type.
    handler_order = 501

    def http_request(self, request):
        if request.get_method() == 'PUT' and not request.has_header('Authorization'):
            request.remove_header('Content-type')
        return request

    https_request = http_request


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        if request.has_header('Authorization'):
            raise MinerUError('response', '带鉴权头的 MinerU API 请求禁止重定向')
        MinerUClient._https(newurl)
        return super().redirect_request(request, fp, code, msg, headers, newurl)


class MinerUError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class MinerUClient:
    base_url = 'https://mineru.net/api/v4'

    def __init__(self, token: str, timeout: int = 60):
        self.token = token
        self.timeout = timeout
        self.upload_urls: dict[str, str] = {}

    def _open(self, request):
        self._https(request.full_url)
        try:
            return urllib.request.build_opener(_SafeRedirect(), _RawUploadHeaders()).open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            if request.has_header('Authorization'):
                code = 'authentication' if exc.code in {401, 403} else 'api' if 400 <= exc.code < 500 and exc.code not in {408, 429} else 'network'
            else:
                code = 'upload_expired' if request.get_method() == 'PUT' and exc.code in {401, 403} else 'download' if request.get_method() == 'GET' and 400 <= exc.code < 500 else 'network'
            raise MinerUError(code, f'MinerU HTTP {exc.code}') from None
        except (OSError, urllib.error.URLError):
            raise MinerUError('network', 'MinerU 网络请求失败，可恢复后重试') from None

    def _json(self, method: str, endpoint: str, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(self.base_url + endpoint, data=data, method=method,
            headers={'Authorization': f'Bearer {self.token}', 'Content-Type': 'application/json'})
        with self._open(request) as response:
            try:
                result = json.load(response)
            except (ValueError, UnicodeError):
                raise MinerUError('response', 'MinerU 返回无效 JSON') from None
        if not isinstance(result, dict) or result.get('code') != 0:
            code = result.get('code') if isinstance(result, dict) else None
            category = 'authentication' if code in ('A0202', 'A0211') else 'api'
            raise MinerUError(category, 'MinerU 拒绝请求，请核对配置和文件限制')
        if not isinstance(result.get('data'), dict):
            raise MinerUError('response', 'MinerU 响应缺少 data')
        return result['data']

    def request_upload(self, name: str, data_id: str, options: dict) -> str:
        options = dict(options)
        file_options = {key: options.pop(key) for key in ('is_ocr', 'page_ranges') if key in options}
        result = self._json('POST', '/file-urls/batch',
            {**options, 'files': [{'name': name, 'data_id': data_id, **file_options}]})
        batch = result.get('batch_id')
        urls = result.get('file_urls', [])
        if not isinstance(batch, str) or not batch or not isinstance(urls, list) or len(urls) != 1 or not isinstance(urls[0], str):
            raise MinerUError('response', 'MinerU 上传响应不完整')
        self.upload_urls[batch] = urls[0]
        return batch

    @staticmethod
    def _https(url: str):
        parsed = urlparse(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            raise MinerUError('response', 'MinerU 返回非 HTTPS 地址')

    def upload(self, batch: str, source: Path):
        url = self.upload_urls.get(batch)
        if not url:
            raise MinerUError('upload_expired', '上传地址未保留；请确认远端任务状态后新建运行')
        self._https(url)
        with source.open('rb') as data:
            request = urllib.request.Request(url, data=data, method='PUT',
                headers={'Content-Length': str(source.stat().st_size)})
            with self._open(request):
                pass

    def query(self, batch: str, data_id: str) -> dict:
        result = self._json('GET', f'/extract-results/batch/{batch}')
        items = result.get('extract_result', [])
        if not isinstance(items, list):
            raise MinerUError('response', 'MinerU 批次结果格式无效')
        matches = [x for x in items if isinstance(x, dict) and x.get('data_id') == data_id]
        if len(matches) != 1:
            raise MinerUError('response', 'MinerU 结果未能唯一匹配 data_id')
        return matches[0]

    def download(self, url: str, target: Path, max_bytes: int = 1024**3):
        self._https(url)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix('.part')
        try:
            with self._open(urllib.request.Request(url)) as response, temporary.open('wb') as out:
                size = 0
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise MinerUError('download', 'MinerU 结果包超出大小上限')
                    out.write(chunk)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)

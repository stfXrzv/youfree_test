#!/usr/bin/env python3
"""Isolated YFT relay/download service. No YouFree files or database are touched."""
from __future__ import annotations

import glob
import html
import importlib.util
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests
import urllib3.util.connection


HOST = os.environ.get('YFT_HOST', '127.0.0.1')
PORT = int(os.environ.get('YFT_PORT', '8183'))
ROOT = Path(os.environ.get('YFT_ROOT', str(Path(__file__).resolve().parent)))
STATIC_ROOT = Path(os.environ.get('YFT_STATIC_ROOT', str(Path(__file__).resolve().parent)))
YTAPI_ROOT = Path(os.environ.get('YFT_YTAPI_ROOT', str(ROOT / 'ytapi')))
TMP = Path(os.environ.get('YFT_TMP', str(ROOT / 'tmp')))
TMP.mkdir(parents=True, exist_ok=True)
FFMPEG = os.environ.get('YFT_FFMPEG', shutil.which('ffmpeg') or str(ROOT / 'tools' / 'ffmpeg.exe'))
COOKIES = os.environ.get('YFT_COOKIES', str(YTAPI_ROOT / 'cookies.txt'))
YTDLP_PROXY = os.environ.get('YFT_YTDLP_PROXY', '' if os.name == 'nt' else 'socks5h://127.0.0.1:40000')
AUTH_POLICY = os.environ.get('YFT_AUTH_POLICY', 'anonymous-first')
if AUTH_POLICY not in ('anonymous-first', 'session-first'):
    raise RuntimeError('YFT_AUTH_POLICY must be anonymous-first or session-first.')
# Signed media URLs must use the same IP family as the extraction request.
urllib3.util.connection.allowed_gai_family = lambda: socket.AF_INET
SEARCH_API = os.environ.get('YFT_SEARCH_API', '')

# Import the private copy, including its own .env/API configuration. None of
# the copied account/auth routes are exposed by the YFT HTTP handler.
ytapi = None
if (YTAPI_ROOT / 'app.py').is_file():
    spec = importlib.util.spec_from_file_location('yft_ytapi_copy', YTAPI_ROOT / 'app.py')
    ytapi = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ytapi)
VIDEO_RE = re.compile(r'^[A-Za-z0-9_-]{11}$')
JOB_RE = re.compile(r'^[a-f0-9]{32}$')

# Prefer a single progressive H.264/AAC source when available. Otherwise yt-dlp
# selects the best video/audio pair at the requested resolution and FFmpeg
# muxes it to fragmented MP4 while piping bytes directly to the player.
FORMAT_SELECTOR = (
    'bestvideo[height=1440][ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/'
    'bestvideo[height=1440][ext=mp4]+bestaudio[ext=m4a]/'
    'best[height=1440][ext=mp4]/'
    'bestvideo[height=1280][ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/'
    'bestvideo[height=1280][ext=mp4]+bestaudio[ext=m4a]/'
    'best[height=1280][ext=mp4]/'
    'bestvideo[height=1024][ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/'
    'bestvideo[height=1024][ext=mp4]+bestaudio[ext=m4a]/'
    'best[height=1024][ext=mp4]/'
    'bestvideo[height<=1440][ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/'
    'bestvideo[height<=1440][ext=mp4]+bestaudio[ext=m4a]/'
    'best[height<=1440][ext=mp4]/best[height<=1440]/best'
)
# Safari, Firefox, and iOS browsers use YouTube's progressive H.264/AAC MP4
# so the relay can preserve byte ranges. The live split-stream mux cannot
# provide stable byte offsets while FFmpeg is still writing.
PROGRESSIVE_MP4_SELECTOR = (
    'best[height<=720][ext=mp4][vcodec^=avc1][acodec^=mp4a][protocol=https]/'
    'best[height<=720][ext=mp4][vcodec^=avc1][protocol=https]/'
    'best[height<=720][ext=mp4][protocol=https]'
)
DOWNLOAD_SELECTOR = FORMAT_SELECTOR
QUALITY_FALLBACK_LIMITS = (1280, 1024, 720)
MEDIA_TTL = 25 * 60
DOWNLOAD_TTL = 30 * 60
MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024
MAX_ACTIVE_DOWNLOADS = 2
MAX_DOWNLOADS_PER_IP_HOUR = 8

_resolved: dict[str, dict] = {}
_source_tokens: dict[str, dict] = {}
_resolve_locks: dict[str, threading.Lock] = {}
_resolve_guard = threading.Lock()
_jobs: dict[str, dict] = {}
_jobs_guard = threading.Lock()
_download_history: dict[str, list[float]] = {}
_device_downloads: dict[str, dict] = {}
_worker_slots = threading.BoundedSemaphore(MAX_ACTIVE_DOWNLOADS)
_network_state = threading.local()


def _json_bytes(data: object) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def _read_json_request(handler: BaseHTTPRequestHandler, limit: int = 16_384) -> dict:
    try:
        length = int(handler.headers.get('Content-Length', '0'))
    except ValueError:
        length = 0
    if length <= 0 or length > limit:
        raise ValueError('Invalid request size.')
    raw = handler.rfile.read(length)
    data = json.loads(raw.decode('utf-8'))
    if not isinstance(data, dict):
        raise ValueError('Expected a JSON object.')
    return data


def _copy_cookies() -> str | None:
    if not os.path.isfile(COOKIES):
        return None
    fd, path = tempfile.mkstemp(prefix='yft-cookies-', suffix='.txt', dir=TMP)
    os.close(fd)
    try:
        shutil.copyfile(COOKIES, path)
        os.chmod(path, 0o600)
        return path
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        return None


def _yt_dlp_base(cookie_path: str | None = None, client: str = '') -> list[str]:
    args = [sys.executable, '-m', 'yt_dlp', '--ignore-config', '--no-playlist', '--force-ipv4',
            '--remote-components', 'ejs:github', '--proxy', YTDLP_PROXY,
            '--socket-timeout', '15', '--extractor-retries', '1']
    runtime = shutil.which('deno') or shutil.which('node')
    if runtime:
        args += ['--js-runtimes', ('deno:' if 'deno' in Path(runtime).stem else 'node:') + runtime]
    pot_provider_url = os.environ.get('YFT_POT_PROVIDER_URL', '').rstrip('/')
    if pot_provider_url:
        args += ['--extractor-args', f'youtubepot-bgutilhttp:base_url={pot_provider_url}']
    if client:
        args += ['--extractor-args', f'youtube:player_client={client}']
    if os.path.isfile(FFMPEG):
        args += ['--ffmpeg-location', FFMPEG]
    if cookie_path:
        args += ['--cookies', cookie_path]
    return args


def _resolver_attempts():
    # Authentication must also be tried with yt-dlp's default supported clients.
    # Restricting the session to mweb discarded a working tv_samsung session.
    attempts = [(True, ''), (False, ''), (True, 'mweb'), (False, 'mweb')]
    if AUTH_POLICY == 'anonymous-first':
        attempts[:2] = [(False, ''), (True, '')]
    for use_session, client in attempts:
        if use_session and not os.path.isfile(COOKIES):
            continue
        yield use_session, client


def _resolver_failure(stderr: str) -> str:
    message = stderr.lower()
    if 'cookies are no longer valid' in message or 'cookies have been rotated' in message:
        return 'session-expired'
    if 'sign in to confirm' in message or 'login_required' in message:
        return 'sign-in-required'
    if 'requested format is not available' in message:
        return 'format-unavailable'
    return 'source-unavailable'


def _chosen_formats(info: dict) -> list[dict]:
    formats = info.get('requested_formats')
    if isinstance(formats, list) and formats:
        return [f for f in formats if isinstance(f, dict) and f.get('url')]
    if info.get('url'):
        return [info]
    return []


def _fallback_selector(max_height: int) -> str:
    return (
        f'bestvideo[height<={max_height}][ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/'
        f'bestvideo[height<={max_height}][ext=mp4]+bestaudio[ext=m4a]/'
        f'best[height<={max_height}][ext=mp4]/best[height<={max_height}]'
    )


def _progressive_fallback_selector(max_height: int) -> str:
    return (
        f'best[height<={max_height}][ext=mp4][vcodec^=avc1][acodec^=mp4a][protocol=https]/'
        f'best[height<={max_height}][ext=mp4][vcodec^=avc1][protocol=https]/'
        f'best[height<={max_height}][ext=mp4][protocol=https]'
    )


def _requires_progressive_source(user_agent: str) -> bool:
    ua = (user_agent or '').lower()
    is_ios = any(device in ua for device in ('iphone', 'ipad', 'ipod'))
    is_firefox = 'firefox/' in ua or 'fxios/' in ua
    other_browsers = ('chrome/', 'crios/', 'chromium/', 'edg/', 'edgios/',
                      'opr/', 'opios/', 'duckduckgo/')
    is_safari = 'safari/' in ua and not any(browser in ua for browser in other_browsers)
    return is_ios or is_firefox or is_safari


def _resolve(video_id: str, force: bool = False, downgrade: bool = False,
             progressive: bool = False) -> dict:
    if not VIDEO_RE.fullmatch(video_id):
        raise ValueError('Invalid video ID.')
    cache_key = f'{video_id}:progressive' if progressive else video_id
    with _resolve_guard:
        lock = _resolve_locks.setdefault(video_id, threading.Lock())
    with lock:
        current = _resolved.get(cache_key)
        if current and not force and current['expires'] > time.time() + 15:
            return current

        selector = PROGRESSIVE_MP4_SELECTOR if progressive else FORMAT_SELECTOR
        if downgrade and current and current.get('height'):
            current_height = int(current['height'])
            next_limit = next((limit for limit in QUALITY_FALLBACK_LIMITS if limit < current_height), max(240, current_height - 1))
            selector = (_progressive_fallback_selector(next_limit) if progressive
                        else _fallback_selector(next_limit))

        proc = None
        failure = 'source-unavailable'
        for use_session, client in _resolver_attempts():
            cookie_copy = _copy_cookies() if use_session else None
            if use_session and not cookie_copy:
                continue
            cmd = _yt_dlp_base(cookie_copy, client) + [
                '--format', selector, '--check-formats', '--dump-single-json', '--skip-download',
                '--', f'https://www.youtube.com/watch?v={video_id}'
            ]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=55, check=False)
            except subprocess.TimeoutExpired:
                continue
            finally:
                if cookie_copy:
                    Path(cookie_copy).unlink(missing_ok=True)
            if proc.returncode == 0:
                break
            failure = _resolver_failure(proc.stderr)
            print(json.dumps({'event': 'yft-resolve-attempt-failed', 'id': video_id,
                              'session': use_session, 'client': client or 'default',
                              'reason': failure}), flush=True)
        if not proc or proc.returncode != 0:
            if failure in ('session-expired', 'sign-in-required'):
                raise RuntimeError('YouTube requires a valid server session. The private YouTube session needs refreshing.')
            raise RuntimeError('YouTube refused the resolver on this connection. No playable source was returned.')
        try:
            info = json.loads(proc.stdout)
        except Exception as exc:
            raise RuntimeError('The stream resolver returned invalid metadata.') from exc
        formats = _chosen_formats(info)
        if not formats:
            raise RuntimeError('No playable source was returned for this video.')
        height = max((int(f.get('height') or 0) for f in formats), default=0)
        ext = str(formats[0].get('ext') or info.get('ext') or 'mp4').lower()
        resolved = {
            'id': video_id,
            'urls': [str(f['url']) for f in formats],
            'headers': [f.get('http_headers') or info.get('http_headers') or {} for f in formats],
            'height': height,
            'quality': f'{height}p' if height else 'Auto',
            'ext': ext,
            'expires': time.time() + MEDIA_TTL,
            'mode': 'direct' if len(formats) == 1 else 'mux',
            'token': uuid.uuid4().hex,
        }
        if current:
            _source_tokens.pop(current.get('token'), None)
        _source_tokens[resolved['token']] = resolved
        _resolved[cache_key] = resolved
        return resolved


def _safe_upstream_headers(headers: dict, range_header: str | None = None, if_range: str | None = None) -> dict:
    allowed = {'User-Agent', 'Referer', 'Accept', 'Accept-Language', 'Origin'}
    result = {str(k): str(v) for k, v in headers.items() if str(k).title() in allowed and isinstance(v, (str, int, float))}
    result['Accept-Encoding'] = 'identity'
    if range_header:
        result['Range'] = range_header
    if if_range:
        result['If-Range'] = if_range
    return result


def _open_source(resolved: dict, index: int, range_header: str | None = None):
    session = getattr(_network_state, 'session', None)
    if session is None:
        session = requests.Session()
        session.trust_env = False
        if YTDLP_PROXY:
            session.proxies.update({'http': YTDLP_PROXY, 'https': YTDLP_PROXY})
        _network_state.session = session
    headers = _safe_upstream_headers(resolved['headers'][index], range_header)
    response = session.get(resolved['urls'][index], headers=headers, stream=True, timeout=(12, 35))
    if response.status_code >= 400:
        status = response.status_code
        response.close()
        raise RuntimeError(f'The media source returned HTTP {status}.')
    return response


def _search(query: str, limit: int) -> list[dict]:
    if ytapi is not None and ytapi.YT_API_KEY:
        data = ytapi.yt_api('search', {'part': 'snippet', 'q': query, 'type': 'video',
                                     'videoCategoryId': '10', 'maxResults': limit})
        items = [ytapi.normalize_item(item) for item in data.get('items', [])]
        durations = ytapi.durations_for([item['id'] for item in items if item.get('id')])
        for item in items:
            item['title'] = html.unescape(str(item.get('title', '')))
            item['artist'] = html.unescape(str(item.get('artist', '')))
            item['duration'] = durations.get(item['id'], item.get('duration', 0))
        return items
    if SEARCH_API:
        target = SEARCH_API + '?' + urllib.parse.urlencode({'q': query, 'limit': limit})
        with urllib.request.urlopen(target, timeout=18) as response:
            return json.loads(response.read(2_000_000)).get('items', [])[:limit]
    raise RuntimeError('The copied YouTube API configuration is missing.')


def _send_json(handler: BaseHTTPRequestHandler, status: int, data: object, headers: dict | None = None):
    body = _json_bytes(data)
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json; charset=utf-8')
    handler.send_header('Content-Length', str(len(body)))
    handler.send_header('Cache-Control', 'no-store')
    handler.send_header('X-Content-Type-Options', 'nosniff')
    for key, value in (headers or {}).items():
        handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(body)


def _download_worker(job_id: str):
    with _jobs_guard:
        job = _jobs.get(job_id)
        if not job:
            return
        job['state'] = 'downloading'
    _worker_slots.acquire()
    output_template = str(TMP / f'{job_id}.%(ext)s')
    proc = None
    watchdog = None
    cookie_copy = None
    try:
        return_code = 1
        for use_session, client in _resolver_attempts():
            cookie_copy = _copy_cookies() if use_session else None
            if use_session and not cookie_copy:
                continue
            cmd = _yt_dlp_base(cookie_copy, client) + [
                '--no-part', '--newline', '--progress', '--progress-template', 'download:%(progress._percent_str)s',
                '--max-filesize', '500M', '--format', DOWNLOAD_SELECTOR, '--check-formats', '--merge-output-format', 'mp4',
                '--output', output_template, '--', f"https://www.youtube.com/watch?v={job['id']}"
            ]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            def stop_overdue_download():
                if proc and proc.poll() is None:
                    proc.terminate()
            watchdog = threading.Timer(4 * 60, stop_overdue_download)
            watchdog.daemon = True
            watchdog.start()
            with _jobs_guard:
                if job_id in _jobs:
                    _jobs[job_id]['proc'] = proc
            last_error = ''
            for line in proc.stdout or []:
                if 'ERROR:' in line:
                    last_error = re.sub(r'https?://\S+', '[source]', line.strip())[:300]
                match = re.search(r'(\d{1,3}(?:\.\d+)?)%', line)
                if match:
                    progress = min(100.0, max(0.0, float(match.group(1))))
                    with _jobs_guard:
                        if job_id in _jobs:
                            _jobs[job_id]['progress'] = f'{progress:.0f}%'
            return_code = proc.wait(timeout=10)
            watchdog.cancel()
            if cookie_copy:
                Path(cookie_copy).unlink(missing_ok=True)
                cookie_copy = None
            if return_code == 0:
                break
            print(json.dumps({'event': 'yft-download-attempt-failed', 'session': use_session, 'client': client, 'error': last_error}), flush=True)
            for path in TMP.glob(f'{job_id}.*'):
                path.unlink(missing_ok=True)
        candidates = [Path(path) for path in glob.glob(str(TMP / f'{job_id}.*')) if not path.endswith(('.part', '.ytdl'))]
        candidates = [p for p in candidates if p.is_file() and p.stat().st_size > 0]
        candidates.sort(key=lambda p: p.stat().st_size, reverse=True)
        if return_code != 0 or not candidates:
            raise RuntimeError('yt-dlp could not complete this video download.')
        output = candidates[0]
        if output.stat().st_size > MAX_DOWNLOAD_BYTES:
            output.unlink(missing_ok=True)
            raise RuntimeError('This video is larger than the YFT test download limit.')
        mime = mimetypes.guess_type(output.name)[0] or 'video/mp4'
        os.utime(output, None)
        with _jobs_guard:
            if job_id in _jobs:
                _jobs[job_id].update({
                    'state': 'ready', 'progress': '100%', 'file': str(output),
                    'size': output.stat().st_size, 'mime': mime,
                    'quality': 'Video ready', 'expires': time.time() + DOWNLOAD_TTL, 'proc': None
                })
    except Exception as exc:
        with _jobs_guard:
            if job_id in _jobs:
                # Don't return resolver logs or source URLs to the browser.
                _jobs[job_id].update({'state': 'failed', 'error': str(exc)[:180], 'expires': time.time() + 300, 'proc': None})
        for path in glob.glob(str(TMP / f'{job_id}.*')):
            try:
                os.remove(path)
            except OSError:
                pass
    finally:
        if watchdog:
            watchdog.cancel()
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=4)
            except Exception:
                try:
                    proc.kill()
                except OSError:
                    pass
        if cookie_copy:
            try:
                os.remove(cookie_copy)
            except OSError:
                pass
        _worker_slots.release()


def _cleanup_loop():
    while True:
        time.sleep(60)
        now = time.time()
        with _jobs_guard:
            expired = [job_id for job_id, job in _jobs.items() if job.get('expires', 0) < now]
            for job_id in expired:
                proc = _jobs[job_id].get('proc')
                if proc and proc.poll() is None:
                    proc.terminate()
                _jobs.pop(job_id, None)
        for job_id in expired:
            for path in glob.glob(str(TMP / f'{job_id}.*')):
                try:
                    os.remove(path)
                except OSError:
                    pass
        # Also recover orphaned temp files after a service restart.
        with _jobs_guard:
            retained_jobs = set(_jobs)
        for path in TMP.iterdir():
            try:
                if path.name.split('.', 1)[0] in retained_jobs:
                    continue
                if path.is_file() and now - path.stat().st_mtime > DOWNLOAD_TTL:
                    path.unlink(missing_ok=True)
            except OSError:
                pass
        for video_id, resolved in list(_resolved.items()):
            if resolved['expires'] < now:
                _resolved.pop(video_id, None)
                _source_tokens.pop(resolved.get('token'), None)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'
    server_version = 'YFT'
    sys_version = ''

    def log_message(self, *_args):
        # Never log resolved provider URLs or request query strings.
        return

    def _client_ip(self) -> str:
        return self.headers.get('X-Real-IP', '').strip() or self.client_address[0]

    def _parse(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path == '/yft' or path.startswith('/yft/'):
            path = path[4:] or '/'
        return path.rstrip('/') or '/', urllib.parse.parse_qs(parsed.query)

    def _serve_static(self, path: str):
        names = {'/': 'index.html', '/index.html': 'index.html', '/app.js': 'app.js',
                 '/styles.css': 'styles.css', '/sw.js': 'sw.js'}
        file_path = STATIC_ROOT / names[path]
        if not file_path.is_file():
            return _send_json(self, 404, {'detail': 'YFT file missing.'})
        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', mimetypes.guess_type(file_path.name)[0] or 'application/octet-stream')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path, qs = self._parse()
        try:
            if path in ('/', '/index.html', '/app.js', '/styles.css', '/sw.js'):
                return self._serve_static(path)
            if path in ('/health', '/api/health'):
                return _send_json(self, 200, {
                    'ok': True, 'service': 'yft', 'backend': 'private-ytapi-copy',
                    'runtime': 'laptop' if os.name == 'nt' else 'vps',
                    'authenticationPolicy': AUTH_POLICY,
                    'sessionConfigured': os.path.isfile(COOKIES),
                })
            internal = re.fullmatch(r'/internal/media/([a-f0-9]{32})/([01])', path)
            if internal:
                resolved = _source_tokens.get(internal[1])
                if self.client_address[0] not in ('127.0.0.1', '::1') or not resolved:
                    return _send_json(self, 404, {'detail': 'Source not found.'})
                return self._relay_source(resolved, int(internal[2]))
            if path == '/api/search':
                query = (qs.get('q', [''])[0] or '').strip()[:180]
                if not query:
                    return _send_json(self, 400, {'detail': 'Type something to search.'})
                limit = max(1, min(20, int(qs.get('limit', ['20'])[0] or 20)))
                items = _search(query, limit)
                return _send_json(self, 200, {'items': items[:limit]})
            if path == '/api/resolve':
                video_id = (qs.get('id', [''])[0] or '').strip()
                force = qs.get('refresh', ['0'])[0] == '1'
                result = _resolve(video_id, force,
                                  progressive=_requires_progressive_source(self.headers.get('User-Agent', '')))
                return _send_json(self, 200, {
                    'id': video_id,
                    'quality': result['quality'],
                    'mode': result['mode'],
                    'mediaPath': f'/yft/media/{video_id}',
                    'expiresIn': max(0, int(result['expires'] - time.time()))
                })
            if path.startswith('/media/'):
                video_id = path.split('/', 2)[-1]
                force = qs.get('refresh', ['0'])[0] == '1'
                return self._serve_media(video_id, force,
                                         progressive=_requires_progressive_source(self.headers.get('User-Agent', '')))
            if path == '/api/download/status':
                job_id = (qs.get('id', [''])[0] or '').strip()
                if not JOB_RE.fullmatch(job_id):
                    return _send_json(self, 400, {'detail': 'Invalid download ID.'})
                with _jobs_guard:
                    job = _jobs.get(job_id)
                    if not job:
                        return _send_json(self, 404, {'detail': 'This temporary download is no longer available.'})
                    return _send_json(self, 200, {
                        'state': job['state'], 'progress': job.get('progress', ''),
                        'error': job.get('error', ''), 'quality': job.get('quality', '')
                    })
            if path == '/api/download/file':
                return self._serve_download(qs.get('id', [''])[0])
            return _send_json(self, 404, {'detail': 'YFT API route not found.'})
        except ConnectionError:
            return
        except urllib.error.HTTPError as exc:
            return _send_json(self, 502, {'detail': f'YouTube API returned HTTP {exc.code}.'})
        except (ValueError, json.JSONDecodeError) as exc:
            return _send_json(self, 400, {'detail': str(exc)[:180]})
        except Exception as exc:
            return _send_json(self, 502, {'detail': str(exc)[:180]})

    def do_POST(self):
        path, _qs = self._parse()
        try:
            if path == '/api/device/downloads':
                data = _read_json_request(self, 256_000)
                device = str(data.get('device', ''))
                if not re.fullmatch(r'[a-f0-9-]{32,36}', device):
                    return _send_json(self, 400, {'detail': 'Invalid device ID.'})
                records = {}
                for item in data.get('items', [])[:1000]:
                    if isinstance(item, dict) and VIDEO_RE.fullmatch(str(item.get('id', ''))) and int(item.get('size', 0)) > 0:
                        records[item['id']] = {'size': int(item['size']), 'verifiedAt': time.time()}
                _device_downloads[device] = {'items': records, 'updatedAt': time.time()}
                return _send_json(self, 200, {'ok': True, 'verifiedCount': len(records)})
            if path == '/api/download':
                data = _read_json_request(self)
                video_id = str(data.get('id', '')).strip()
                if not VIDEO_RE.fullmatch(video_id):
                    return _send_json(self, 400, {'detail': 'Invalid video ID.'})
                now = time.time()
                ip = self._client_ip()
                recent = [stamp for stamp in _download_history.get(ip, []) if stamp > now - 3600]
                if len(recent) >= MAX_DOWNLOADS_PER_IP_HOUR:
                    return _send_json(self, 429, {'detail': 'YFT test download limit reached. Try again later.'})
                recent.append(now)
                _download_history[ip] = recent
                with _jobs_guard:
                    active = sum(1 for job in _jobs.values() if job.get('state') in ('queued', 'downloading'))
                    if active >= MAX_ACTIVE_DOWNLOADS:
                        return _send_json(self, 429, {'detail': 'YFT is handling two downloads right now. Try again shortly.'})
                    job_id = uuid.uuid4().hex
                    _jobs[job_id] = {
                        'id': video_id, 'title': str(data.get('title', 'Video'))[:240],
                        'artist': str(data.get('artist', ''))[:180], 'state': 'queued',
                        'progress': '', 'created': now, 'expires': now + DOWNLOAD_TTL,
                    }
                threading.Thread(target=_download_worker, args=(job_id,), daemon=True).start()
                return _send_json(self, 202, {'jobId': job_id, 'state': 'queued'})
            if path == '/api/download/ack':
                data = _read_json_request(self)
                job_id = str(data.get('jobId', '')).strip()
                if not JOB_RE.fullmatch(job_id):
                    return _send_json(self, 400, {'detail': 'Invalid download ID.'})
                with _jobs_guard:
                    job = _jobs.get(job_id)
                    if not job or job.get('state') != 'ready':
                        return _send_json(self, 409, {'detail': 'The IndexedDB write was not acknowledged yet.'})
                    path_to_remove = job.get('file')
                    _jobs.pop(job_id, None)
                if path_to_remove:
                    try:
                        os.remove(path_to_remove)
                    except OSError:
                        pass
                deleted = not path_to_remove or not os.path.exists(path_to_remove)
                return _send_json(self, 200, {'ok': True, 'tempDeleted': deleted})
            return _send_json(self, 404, {'detail': 'YFT API route not found.'})
        except (ValueError, json.JSONDecodeError) as exc:
            return _send_json(self, 400, {'detail': str(exc)[:180]})
        except Exception as exc:
            return _send_json(self, 500, {'detail': str(exc)[:180]})

    def _serve_media(self, video_id: str, force: bool = False, progressive: bool = False):
        if not VIDEO_RE.fullmatch(video_id):
            return _send_json(self, 400, {'detail': 'Invalid video ID.'})
        for attempt in range(2):
            try:
                resolved = _resolve(video_id, force or attempt > 0, downgrade=attempt > 0,
                                    progressive=progressive)
                if len(resolved['urls']) > 1:
                    if self._serve_muxed(resolved):
                        return
                    continue
                return self._relay_source(resolved, 0)
            except (RuntimeError, requests.RequestException):
                if attempt == 1:
                    return _send_json(self, 502, {'detail': 'The selected source could not be streamed; fresh resolution also failed.'})
        return _send_json(self, 502, {'detail': 'The live muxer could not open a playable source.'})

    def _relay_source(self, resolved: dict, index: int):
        upstream = _open_source(resolved, index, self.headers.get('Range'))
        self.send_response(upstream.status_code)
        self.send_header('Content-Type', upstream.headers.get('Content-Type', 'video/mp4'))
        for header in ('Content-Length', 'Content-Range', 'Accept-Ranges', 'ETag', 'Last-Modified'):
            value = upstream.headers.get(header)
            if value:
                self.send_header(header, value)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-YFT-Quality', resolved['quality'])
        self.end_headers()
        try:
            for chunk in upstream.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                self.wfile.write(chunk)
        except (ConnectionError, TimeoutError, requests.RequestException):
            pass
        finally:
            upstream.close()

    def _serve_muxed(self, resolved: dict):
        if not os.path.isfile(FFMPEG):
            raise RuntimeError('FFmpeg is unavailable in the YFT runtime.')
        cmd = [FFMPEG, '-hide_banner', '-loglevel', 'error', '-nostdin']
        # Every upstream read follows the resolver's configured proxy. FFmpeg
        # reads only loopback URLs; a signed provider URL never leaves YFT.
        for index in range(len(resolved['urls'])):
            cmd += ['-http_proxy', '', '-rw_timeout', '20000000', '-i', f"http://127.0.0.1:{PORT}/internal/media/{resolved['token']}/{index}"]
        cmd += [
            '-map', '0:v:0', '-map', '1:a:0?', '-c:v', 'copy', '-c:a', 'copy',
            '-movflags', '+frag_keyframe+empty_moov+default_base_moof', '-f', 'mp4', 'pipe:1'
        ]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except Exception:
            return False
        first = proc.stdout.read1(32 * 1024) if proc.stdout else b''
        if not first:
            proc.wait(timeout=5)
            return False
        self.close_connection = True
        self.send_response(200)
        self.send_header('Content-Type', 'video/mp4')
        self.send_header('Accept-Ranges', 'none')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-YFT-Quality', resolved['quality'])
        self.send_header('X-YFT-Stream-Mode', 'live-mux')
        self.end_headers()
        try:
            self.wfile.write(first)
            while True:
                chunk = proc.stdout.read1(64 * 1024) if proc.stdout else b''
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (ConnectionError, TimeoutError):
            pass
        finally:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        return True

    def _serve_download(self, job_id: str):
        if not JOB_RE.fullmatch(job_id):
            return _send_json(self, 400, {'detail': 'Invalid download ID.'})
        with _jobs_guard:
            job = _jobs.get(job_id)
            if not job or job.get('state') != 'ready' or not job.get('file'):
                return _send_json(self, 404, {'detail': 'The download is not ready.'})
            file_path = Path(job['file'])
            mime = job.get('mime') or 'application/octet-stream'
            size = int(job.get('size') or 0)
        if not file_path.is_file() or size <= 0:
            return _send_json(self, 410, {'detail': 'The temporary file expired.'})
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(size))
        self.send_header('Content-Disposition', f'attachment; filename="yft-{job_id}.{file_path.suffix.lstrip(".")}"')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        try:
            with file_path.open('rb') as source:
                shutil.copyfileobj(source, self.wfile, length=256 * 1024)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass


def main():
    threading.Thread(target=_cleanup_loop, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    server.serve_forever(poll_interval=0.5)


if __name__ == '__main__':
    main()

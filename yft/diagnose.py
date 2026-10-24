import json
import re
import subprocess
import sys
from pathlib import Path
sys.path.insert(0, '/opt/yft')
import server

video_id = 'xFYQQPAOz7Y'
cases = [('tv', False, 'tv'), ('tv-cookie', True, 'tv'), ('tv-simply', False, 'tv_simply')]
cookie_names = set()
for line in Path(server.COOKIES).read_text(errors='ignore').splitlines():
    fields = line.split('\t')
    if len(fields) >= 7 and not line.startswith('#'):
        cookie_names.add(fields[5])
    elif line.startswith('#HttpOnly_') and len(fields) >= 7:
        cookie_names.add(fields[5])
print(json.dumps({'cookie_file': {'readable': True, 'count': len(cookie_names), 'has_session': '__Secure-1PSID' in cookie_names, 'has_login_info': 'LOGIN_INFO' in cookie_names}}, separators=(',', ':')))
wanted = {'SIDCC', '__Secure-1PSIDCC', '__Secure-1PSIDTS', '__Secure-YEC', '__Secure-YENID', 'YSC'}
expiry_map = {}
for line in Path(server.COOKIES).read_text(errors='ignore').splitlines():
    fields = line.split('\t')
    if len(fields) >= 7 and fields[5] in wanted:
        expiry_map[fields[5]] = fields[4]
print(json.dumps({'cookie_expiries_only': expiry_map}, separators=(',', ':')))
for label, cookie, client in cases:
    cookie_copy = server._copy_cookies() if cookie else None
    cmd = server._yt_dlp_base(cookie_copy)
    # Compare direct egress without changing YFT or YouFree configuration.
    if client:
        cmd += ['--extractor-args', f'youtube:player_client={client}']
    cmd += ['--format', server.FORMAT_SELECTOR, '--dump-single-json', '--skip-download', '--', f'https://www.youtube.com/watch?v={video_id}']
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=55, check=False)
    finally:
        if cookie_copy:
            Path(cookie_copy).unlink(missing_ok=True)
    summary = {'case': label, 'returncode': proc.returncode, 'stdout_bytes': len(proc.stdout or '')}
    if proc.returncode == 0:
        try:
            info = json.loads(proc.stdout)
            selected = info.get('requested_formats') or [info]
            summary['selected'] = [{'height': f.get('height'), 'ext': f.get('ext'), 'vcodec': f.get('vcodec'), 'acodec': f.get('acodec')} for f in selected]
        except Exception:
            summary['json'] = 'invalid'
    err = ((proc.stderr or '') + '\n' + (proc.stdout or '')).lower()
    summary['signals'] = {
        'confirm_not_a_bot': any(x in err for x in ('confirm you\'re not a bot', 'confirm you are not a bot')),
        'login_or_bot_check': any(x in err for x in ('sign in', 'bot', 'confirm you')),
        'http_403': '403' in err,
        'javascript_challenge': any(x in err for x in ('javascript', 'challenge', 'ejs')),
        'unavailable': 'unavailable' in err,
        'format_selector': 'format' in err,
    }
    lines = [line.strip() for line in ((proc.stderr or '') + '\n' + (proc.stdout or '')).splitlines() if 'error' in line.lower() or 'warning' in line.lower()]
    if lines:
        line = re.sub(r'https?://\S+', '[URL]', lines[-1])
        line = re.sub(r'(?i)(cookie|authorization|token)\s*[:=]\s*\S+', r'\1=[redacted]', line)
        line = re.sub(r'[A-Za-z0-9_\-]{80,}', '[redacted]', line)
        summary['sanitized_error'] = line[:220]
    print(json.dumps(summary, separators=(',', ':')))

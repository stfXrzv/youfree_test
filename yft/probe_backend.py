"""Print resolver diagnostics without disclosing cookies or signed source URLs."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

parser = argparse.ArgumentParser()
parser.add_argument('--root', default='/opt/yft/ytapi')
parser.add_argument('--id', default='xFYQQPAOz7Y')
parser.add_argument('--case', choices=['anonymous', 'session', 'mweb', 'direct'], default='anonymous')
parser.add_argument('--proxy', default='' if os.name == 'nt' else 'socks5h://127.0.0.1:40000')
args = parser.parse_args()
root = Path(args.root)
cookie = None
command = [sys.executable, '-m', 'yt_dlp', '--ignore-config', '--no-playlist', '--no-warnings',
           '--remote-components', 'ejs:github']
runtime = shutil.which('deno') or shutil.which('node')
if runtime:
    command += ['--js-runtimes', ('deno:' if 'deno' in Path(runtime).stem else 'node:') + runtime]
if os.name != 'nt':
    command += ['--extractor-args', 'youtubepot-bgutilhttp:base_url=http://127.0.0.1:4417']
if args.case != 'direct':
    command += ['--proxy', args.proxy]
else:
    command += ['--proxy', '']
if args.case in ('session', 'mweb'):
    fd, name = tempfile.mkstemp(prefix='probe-cookie-', suffix='.txt', dir=root / 'tmp')
    os.close(fd)
    cookie = Path(name)
    shutil.copyfile(root / 'cookies.txt', cookie)
    cookie.chmod(0o600)
    command += ['--cookies', str(cookie)]
if args.case == 'mweb':
    command += ['--extractor-args', 'youtube:player_client=mweb']
command += ['--skip-download', '--dump-single-json', '-f', 'bv[height<=1440]+ba/b',
            '--', 'https://www.youtube.com/watch?v=' + args.id]
started = time.monotonic()
try:
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    summary = {'case': args.case, 'seconds': round(time.monotonic() - started, 2), 'code': result.returncode}
    if result.returncode == 0:
        info = json.loads(result.stdout)
        summary.update({'id': info.get('id'), 'title': info.get('title'),
                        'formats': [{'id': f.get('format_id'), 'height': f.get('height'),
                                     'video': f.get('vcodec'), 'audio': f.get('acodec'),
                                     'protocol': f.get('protocol')} for f in info.get('requested_formats', [info])]})
    else:
        error = '\n'.join(line for line in result.stderr.splitlines() if 'ERROR:' in line or 'WARNING:' in line)[-600:]
        summary['error'] = re.sub(r'https?://\S+', '[source]', error)
    print(json.dumps(summary), flush=True)
except subprocess.TimeoutExpired:
    print(json.dumps({'case': args.case, 'error': 'resolver timeout', 'seconds': 60}), flush=True)
finally:
    if cookie:
        cookie.unlink(missing_ok=True)

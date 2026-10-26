#!/usr/bin/python3
"""Restricted SSH receiver. Installed root-owned; never executes repository code."""
import base64
import fcntl
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

ROOT = Path('/srv/tanem-sites')
CONFIG = Path('/etc/tanem-publish')
NGINX = Path('/etc/nginx/conf.d')
AUTH = Path('/var/lib/tanem-publish/.ssh/authorized_keys')
PROGRAM = '/usr/local/lib/tanem-publish/receiver.py'
SLUG = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z')
SHA = re.compile(r'[0-9a-f]{40}\Z')
EXTENSIONS = {'.html', '.htm', '.css', '.js', '.mjs', '.json', '.webmanifest',
              '.png', '.jpg', '.jpeg', '.webp', '.avif', '.gif', '.svg', '.ico',
              '.woff', '.woff2', '.ttf', '.otf', '.eot', '.mp4', '.webm', '.mp3',
              '.ogg', '.wav', '.pdf', '.txt', '.xml', '.wasm'}


def atomic(path, data, mode=0o600):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix='.next-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data if isinstance(data, bytes) else data.encode())
            out.flush()
            os.fsync(out.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def write_json(path, data):
    atomic(path, json.dumps(data, sort_keys=True) + '\n')


def read_json(path):
    return json.loads(Path(path).read_text())


def validate_site(value):
    slug = value['slug']
    if not isinstance(slug, str) or not SLUG.fullmatch(slug):
        raise ValueError('Invalid site identifier')
    repo = value['repository']
    if not re.fullmatch(r'TanemTech/[A-Za-z0-9_.-]+', repo):
        raise ValueError('Only explicitly registered TanemTech repositories are accepted')
    hostname = value['hostname']
    if hostname != slug + '.tanem.ru':
        raise ValueError('Hostname must equal the approved slug.tanem.ru')
    return {'slug': slug, 'repository': repo, 'hostname': hostname}


def public_key(value):
    parts = value.strip().split()
    if len(parts) < 2 or parts[0] != 'ssh-ed25519':
        raise ValueError('Expected an Ed25519 public key')
    raw = base64.b64decode(parts[1], validate=True)
    if len(raw) != 51 or raw[:19] != b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20':
        raise ValueError('Invalid public key encoding')
    return 'ssh-ed25519 ' + parts[1]


def auth_line(key, scope):
    return f'restrict,command="/usr/bin/sudo -n {PROGRAM} {scope}" {public_key(key)}\n'


def rebuild_auth():
    text = auth_line((CONFIG / 'admin.pub').read_text(), 'admin')
    for path in sorted((CONFIG / 'sites').glob('*.json')):
        site = read_json(path)
        validate_site(site)
        for key in site['keys']:
            text += auth_line(key, site['slug'])
    atomic(AUTH, text, 0o644)


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=30)


def nginx_dump():
    result = run('/usr/sbin/nginx', '-T')
    return result.stdout


def conflict(hostname, dump):
    dump = re.sub(r'#[^\n]*', '', dump)
    for names in re.findall(r'\bserver_name\s+([^;]+);', dump):
        for name in names.split():
            name = name.strip('"\'')
            if name == hostname or name == '.' + hostname:
                return True
            if name.startswith('*.') and hostname.endswith(name[1:]):
                return True
            if name.startswith('.') and (hostname == name[1:] or hostname.endswith(name)):
                return True
            if name.endswith('.*') and hostname.startswith(name[:-1]):
                return True
            # Regex virtual hosts require a human review; do not guess overlap.
            if name.startswith('~'):
                raise ValueError('Existing regex server_name requires inspection before adding a site')
    return False


def register(value):
    site = validate_site(value)
    path = CONFIG / 'sites' / (site['slug'] + '.json')
    key = public_key(value['public_key'])
    if path.exists():
        old = read_json(path)
        if validate_site(old) != site:
            raise ValueError('Existing site binding cannot be reassigned')
        keys = old['keys']
    else:
        if conflict(site['hostname'], nginx_dump()):
            raise ValueError('Hostname already belongs to an existing Nginx site; no takeover performed')
        if (ROOT / site['slug']).exists() or (NGINX / f'tanem-auto-{site["slug"]}.conf').exists():
            raise ValueError('Unmanaged path already exists; no overwrite performed')
        keys = []
    # Keep existing keys until the new secret has been saved and successfully used.
    if key not in keys:
        keys.append(key)
    write_json(path, {**site, 'keys': keys, 'connected': False})
    rebuild_auth()
    return {'status': 'REGISTERED', **site}


def enrollment(request):
    site = validate_site(request)
    path = CONFIG / 'sites' / (site['slug'] + '.json')
    if not path.exists():
        return {'status': 'NOT_REGISTERED'}
    old = read_json(path)
    if validate_site(old) != site:
        raise ValueError('Existing site binding cannot be reassigned')
    if request['action'] == 'confirm':
        key = public_key(request['public_key'])
        if key not in old['keys']:
            raise ValueError('Confirmation requires a previously enrolled key')
        # Called only after saving the repository secret and triggering CI.
        write_json(path, {**site, 'keys': [key], 'connected': True})
        rebuild_auth()
        return {'status': 'CONNECTED'}
    return {'status': 'CONNECTED' if old.get('connected') else 'PENDING', **site}


def safe_name(name):
    if '\\' in name or '\x00' in name or any(ord(c) < 32 for c in name):
        raise ValueError('Invalid archive path')
    parts = name.split('/')
    while parts and parts[0] == '.':
        parts.pop(0)
    if not parts or any(p in ('', '.', '..') for p in parts):
        raise ValueError('Unsafe archive path')
    path = PurePosixPath(*parts)
    if path.is_absolute() or any(p.startswith('.') for p in parts):
        if str(path) != '.nojekyll':
            raise ValueError('Hidden or absolute archive path')
    if path.suffix.lower() not in EXTENSIONS and str(path) != '.nojekyll':
        raise ValueError('Unexpected file type in static artifact: ' + str(path))
    return str(path)


def unpack(stream, target):
    names = set()
    total = 0
    with tarfile.open(fileobj=stream, mode='r|gz') as archive:
        for member in archive:
            if member.isdir():
                # Directories are created only from validated file paths below.
                continue
            if not member.isfile() or member.issparse():
                raise ValueError('Links, special files and sparse files are forbidden')
            name = safe_name(member.name)
            if name in names or member.size < 0:
                raise ValueError('Duplicate or invalid archive member')
            names.add(name)
            # Capacity check, not a fixed site-size or photo-count limit.
            if member.size >= shutil.disk_usage(target).free:
                raise ValueError('Insufficient free disk space for a staged release')
            dest = target / name
            dest.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            with archive.extractfile(member) as source, dest.open('xb') as out:
                shutil.copyfileobj(source, out, 1024 * 1024)
            if dest.stat().st_size != member.size:
                raise ValueError('Truncated archive')
            dest.chmod(0o644)
            total += member.size
    if 'index.html' not in names or b'<html' not in (target / 'index.html').read_bytes()[:65536].lower():
        raise ValueError('Valid index.html is required')
    return len(names), total


def nginx_config(site):
    root = ROOT / site['slug'] / 'current'
    return f'''# Managed by TANEM publisher. Existing sites are never adopted implicitly.
server {{
    listen 80;
    server_name {site['hostname']};
    root {root};
    index index.html;
    location = /__tanem_release {{
        alias {root}/.tanem-release;
        default_type text/plain;
        add_header Cache-Control "no-store" always;
    }}
    location ~ /\\. {{ deny all; }}
    location / {{ try_files $uri $uri/ =404; }}
}}
'''


def link(path, value):
    tmp = path.with_name(path.name + '.next')
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(value)
    os.replace(tmp, path)


def probe(site, sha):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(30):
        try:
            req = urllib.request.Request('http://127.0.0.1/__tanem_release',
                                         headers={'Host': site['hostname']})
            with opener.open(req, timeout=2) as response:
                if response.read(128).decode().strip() == sha:
                    return
        except (OSError, ValueError):
            pass
        time.sleep(0.2)
    raise RuntimeError('HTTP release verification failed')


def recover(base):
    journal = base / 'pending.json'
    if not journal.exists():
        return
    old = read_json(journal)
    current = base / 'current'
    if old['current'] is None:
        current.unlink(missing_ok=True)
    else:
        link(current, old['current'])
    conf = NGINX / f'tanem-auto-{base.name}.conf'
    if old['new_config']:
        conf.unlink(missing_ok=True)
        run('/usr/sbin/nginx', '-t')
        run('/usr/sbin/nginx', '-s', 'reload')
    if old['state'] is None:
        (base / 'state.json').unlink(missing_ok=True)
    else:
        write_json(base / 'state.json', old['state'])
    journal.unlink()


def deploy(site, request, stream):
    sha = request.get('sha', '')
    number = request.get('run_id')
    if not SHA.fullmatch(sha) or type(number) is not int or number < 1:
        raise ValueError('Invalid release identity')
    if request.get('repository') != site['repository']:
        raise ValueError('Repository does not match registered site')
    base = ROOT / site['slug']
    base.mkdir(mode=0o755, exist_ok=True)
    recover(base)
    state_file = base / 'state.json'
    old_state = read_json(state_file) if state_file.exists() else None
    if old_state and number < old_state['run_id']:
        return {'status': 'STALE', 'sha': old_state['sha']}
    if old_state and number == old_state['run_id']:
        if sha != old_state['sha']:
            raise ValueError('Run identity reused for another commit')
        probe(site, sha)
        return {'status': 'UNCHANGED', 'sha': sha}
    releases = base / 'releases'
    releases.mkdir(mode=0o755, exist_ok=True)
    # Remove abandoned staging directories only inside this publisher-owned tree.
    for path in releases.glob('.stage-*'):
        shutil.rmtree(path)
    with tempfile.TemporaryDirectory(prefix='.stage-', dir=releases) as temp:
        staging = Path(temp)
        staging.chmod(0o755)
        count, size = unpack(stream, staging)
        (staging / '.tanem-release').write_text(sha + '\n')
        (staging / '.tanem-release').chmod(0o644)
        current = base / 'current'
        old_current = os.readlink(current) if current.is_symlink() else None
        if current.exists() and old_current is None:
            raise ValueError('Refusing to replace unmanaged site directory')
        conf = NGINX / f'tanem-auto-{site["slug"]}.conf'
        expected_config = nginx_config(site)
        new_config = not conf.exists()
        if not new_config and conf.read_text() != expected_config:
            raise ValueError('Managed Nginx config was changed; inspection required')
        if new_config and conflict(site['hostname'], nginx_dump()):
            raise ValueError('Hostname became occupied; existing site left unchanged')
        final = releases / f'{number}-{sha}'
        if final.exists():
            if old_current == str(final):
                raise ValueError('Release state mismatch; inspection required')
            shutil.rmtree(final)
        # Save recovery instructions BEFORE any serving path/config is changed.
        write_json(base / 'pending.json', {'current': old_current,
                   'state': old_state, 'new_config': new_config})
        try:
            os.replace(staging, final)
            link(current, str(final))
            if new_config:
                atomic(conf, expected_config, 0o644)
            run('/usr/sbin/nginx', '-t')
            if new_config:
                run('/usr/sbin/nginx', '-s', 'reload')
            probe(site, sha)
            state = {'sha': sha, 'run_id': number, 'files': count, 'bytes': size,
                     'previous': old_current, 'repository': site['repository']}
            write_json(state_file, state)
            (base / 'pending.json').unlink()
        except BaseException:
            recover(base)
            raise
        # Retain current + previous; backups do not accumulate indefinitely.
        for path in releases.iterdir():
            if str(path) not in (str(final), old_current):
                try:
                    shutil.rmtree(path)
                except OSError as exc:
                    print('WARNING: old release cleanup failed: ' + str(exc), file=sys.stderr)
    return {'status': 'DEPLOYED', **state, 'hostname': site['hostname']}


def handle(scope, request, stream):
    if request.get('action') == 'ping':
        return {'status': 'READY', 'scope': scope, 'version': 1}
    if scope == 'admin':
        if request.get('action') in ('status', 'confirm'):
            return enrollment(request)
        if request.get('action') != 'register':
            raise ValueError('Admin key only registers site bindings and scoped keys')
        return register(request)
    if not SLUG.fullmatch(scope):
        raise ValueError('Invalid scope')
    site = read_json(CONFIG / 'sites' / (scope + '.json'))
    validate_site(site)
    if request.get('action') != 'deploy':
        raise ValueError('Site key can only publish its own site')
    return deploy(site, request, stream)


def main():
    os.umask(0o022)
    if os.geteuid() != 0 or len(sys.argv) != 2:
        raise ValueError('Receiver requires its installed restricted SSH command')
    line = sys.stdin.buffer.readline(16385)
    if len(line) > 16384 or not line.endswith(b'\n'):
        raise ValueError('Invalid request header')
    request = json.loads(line)
    with (CONFIG / 'publish.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = handle(sys.argv[1], request, sys.stdin.buffer)
    print(json.dumps(result))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('ERROR: ' + str(exc), file=sys.stderr)
        sys.exit(1)

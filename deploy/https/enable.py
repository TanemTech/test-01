#!/usr/bin/python3
"""One-time guarded HTTPS activation; modifies only TANEM-owned HTTP vhosts."""
import hashlib
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

PROGRAM = Path('/usr/local/lib/tanem-publish/receiver.py')
MARKER = Path('/etc/tanem-publish/installed.json')
REGISTRY = Path('/etc/tanem-publish/sites')
NGINX = Path('/etc/nginx/conf.d')
ETC = Path('/etc/tanem-https')
LIB = Path('/usr/local/lib/tanem-https')
WEBROOT = Path('/var/lib/tanem-https')
SYSTEMD = Path('/etc/systemd/system')
RELOAD = r'''#!/bin/sh
set -eu
/usr/sbin/nginx -t
/usr/sbin/nginx -s reload
'''
# Reuse the system's existing certbot.timer instead of adding another service/timer.
DROPIN_DIR = SYSTEMD / 'certbot.service.d'
DROPIN_PATH = DROPIN_DIR / '10-tanem-https.conf'
DEPLOY_HOOK = Path('/etc/letsencrypt/renewal-hooks/deploy/tanem-https-reload.sh')
DROPIN = '''[Service]
# Never change the success/failure status of the existing certificate renewals.
ExecStartPost=-/usr/bin/python3 /usr/local/lib/tanem-https/agent.py
'''



def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=35)


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


def digest(value):
    return hashlib.sha256(value).hexdigest()


def install(new_receiver, agent_file):
    if os.geteuid() != 0:
        raise RuntimeError('Requires Selectel root console')
    for file in (PROGRAM, MARKER, new_receiver, agent_file):
        if not file.is_file():
            raise RuntimeError('Missing installation prerequisite: ' + str(file))
    for cmd in ('/usr/bin/certbot', '/usr/sbin/nginx', '/usr/bin/systemctl'):
        if not Path(cmd).exists():
            raise RuntimeError('Missing existing tool: ' + cmd)
    run('/usr/sbin/nginx', '-t')
    current = PROGRAM.read_bytes()
    new_bytes = new_receiver.read_bytes()
    old_marker = MARKER.read_bytes()
    marker = json.loads(old_marker)
    if marker.get('receiver_sha256') != digest(current):
        raise RuntimeError('Publisher version marker mismatch; no changes')
    if (ETC / 'installed.json').exists():
        if current == new_bytes:
            print('HTTPS_ALREADY_INSTALLED')
            return
        raise RuntimeError('Unknown HTTPS installation; do not overwrite')
    if current == new_bytes:
        raise RuntimeError('Receiver was modified outside guarded HTTPS installation')
    if ETC.exists() or LIB.exists() or WEBROOT.exists():
        raise RuntimeError('Existing HTTPS paths need manual inspection; no changes')
    if DROPIN_PATH.exists() or DEPLOY_HOOK.exists():
        raise RuntimeError('Existing Certbot integration needs inspection; no changes')
    certbot_service = run('/usr/bin/systemctl', 'cat', 'certbot.service').stdout
    if 'ExecStart=/usr/bin/certbot -q renew --no-random-sleep-on-renew' not in certbot_service:
        raise RuntimeError('Unexpected existing Certbot service; no changes')
    if run('/usr/bin/systemctl', 'is-enabled', 'certbot.timer').stdout.strip() != 'enabled':
        raise RuntimeError('Certbot automatic renewal timer must already be enabled')

    previous = module(PROGRAM, 'tanem_receiver_old')
    updated = module(new_receiver, 'tanem_receiver_new')
    plans = []
    for site_file in sorted(REGISTRY.glob('*.json')):
        site = json.loads(site_file.read_text())
        previous.validate_site(site)
        updated.validate_site(site)
        if site_file.stem != site['slug']:
            raise RuntimeError('Registry mismatch: ' + site_file.name)
        http = NGINX / ('tanem-auto-' + site['slug'] + '.conf')
        if http.exists():
            if http.read_text() != previous.nginx_config(site):
                raise RuntimeError('HTTP configuration changed; no upgrade: ' + http.name)
            plans.append((http, http.read_bytes(), updated.nginx_config(site)))
    # Existing non-TANEM vhosts and their SSL certificates are never edited.
    done = []
    try:
        ETC.mkdir(mode=0o700)
        done.append(ETC)
        (ETC / 'redirects').mkdir(mode=0o755)
        (ETC / 'attempts').mkdir(mode=0o700)
        backup = ETC / 'backup'
        backup.mkdir(mode=0o700)
        LIB.mkdir(mode=0o755, parents=True)
        done.append(LIB)
        WEBROOT.joinpath('.well-known', 'acme-challenge').mkdir(mode=0o755, parents=True)
        done.append(WEBROOT)
        (backup / 'receiver.py').write_bytes(current)
        (backup / 'installed.json').write_bytes(old_marker)
        for file, before, after in plans:
            (backup / file.name).write_bytes(before)
        new_receiver_tmp = PROGRAM.with_name('receiver.py.tanem-next')
        new_receiver_tmp.write_bytes(new_bytes)
        new_receiver_tmp.chmod(0o755)
        os.replace(new_receiver_tmp, PROGRAM)
        for file, _, after in plans:
            updated.atomic(file, after, 0o644)
        marker['receiver_sha256'] = digest(new_bytes)
        updated.atomic(MARKER, json.dumps(marker, sort_keys=True) + '\n')
        (LIB / 'agent.py').write_bytes(agent_file.read_bytes())
        (LIB / 'agent.py').chmod(0o755)
        (LIB / 'reload.sh').write_text(RELOAD)
        (LIB / 'reload.sh').chmod(0o755)
        DROPIN_DIR.mkdir(parents=True, exist_ok=True)
        DROPIN_PATH.write_text(DROPIN)
        done.append(DROPIN_PATH)
        DEPLOY_HOOK.parent.mkdir(parents=True, exist_ok=True)
        DEPLOY_HOOK.write_text(RELOAD)
        DEPLOY_HOOK.chmod(0o755)
        done.append(DEPLOY_HOOK)
        run('/usr/sbin/nginx', '-t')
        run('/usr/sbin/nginx', '-s', 'reload')
        run('/usr/bin/systemctl', 'daemon-reload')
        (ETC / 'installed.json').write_text(json.dumps({'receiver_sha256': digest(new_bytes)}))
    except BaseException:
        # Roll back all TANEM-managed HTTP files and the publisher; existing sites untouched.
        PROGRAM.write_bytes(current)
        PROGRAM.chmod(0o755)
        MARKER.write_bytes(old_marker)
        for file, before, _ in plans:
            file.write_bytes(before)
        subprocess.run(['/usr/sbin/nginx', '-t'], check=False, capture_output=True)
        subprocess.run(['/usr/sbin/nginx', '-s', 'reload'], check=False, capture_output=True)
        for path in reversed(done):
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        subprocess.run(['/usr/bin/systemctl', 'daemon-reload'], check=False, capture_output=True)
        raise
    print('TANEM_HTTPS_INSTALLED: existing certbot.timer reused; managed vhosts:',
          len(plans))


if __name__ == '__main__':
    try:
        with Path('/etc/tanem-publish/publish.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            install(Path(sys.argv[1]), Path(sys.argv[2]))
    except Exception as exc:
        print('HTTPS_SETUP_ABORTED:', str(exc), file=sys.stderr)
        sys.exit(1)

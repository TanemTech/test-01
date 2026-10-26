#!/usr/bin/env python3
"""One-time bootstrap; no changes to existing sites, SSH settings, firewall or DNS."""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import tempfile

import receiver as r


def execute(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=30, **kwargs)


def install(host, port, quiet=False):
    if os.geteuid() != 0:
        raise ValueError('Run once in the Selectel root web console')
    ipaddress.IPv4Address(host)
    if not 1 <= port <= 65535:
        raise ValueError('Invalid SSH port')
    for command in ('nginx', 'sshd', 'sudo', 'visudo', 'ssh', 'ssh-keygen', 'useradd', 'openssl'):
        if not shutil.which(command):
            raise ValueError('Required server command missing: ' + command)
    dump = execute('/usr/sbin/nginx', '-T').stdout
    if '/etc/nginx/conf.d/*.conf' not in dump:
        raise ValueError('Nginx must already include /etc/nginx/conf.d/*.conf; no config changed')
    execute('/usr/sbin/sshd', '-t')
    marker = r.CONFIG / 'installed.json'
    receiver_bytes = Path(r.__file__).read_bytes()
    digest = hashlib.sha256(receiver_bytes).hexdigest()
    if marker.exists():
        state = r.read_json(marker)
        if state['receiver_sha256'] != digest:
            raise ValueError('Another publisher version is installed; explicit upgrade required')
        if state['host'] != host or state['port'] != port:
            raise ValueError('Existing installation has a different server address')
        bundle = r.read_json(r.CONFIG / 'credentials.json')
    else:
        try:
            pwd.getpwnam('tanem-publish')
        except KeyError:
            pass
        else:
            raise ValueError('User tanem-publish already exists without our installation marker')
        for path in (r.CONFIG, r.ROOT, Path(r.PROGRAM).parent, r.AUTH.parent.parent,
                     Path('/etc/sudoers.d/tanem-publish')):
            if path.exists():
                raise ValueError('Existing unmanaged path; nothing overwritten: ' + str(path))
        # Every persistent path below belongs exclusively to this publisher.
        r.CONFIG.mkdir(mode=0o700)
        (r.CONFIG / 'sites').mkdir(mode=0o700)
        r.ROOT.mkdir(mode=0o755)
        Path(r.PROGRAM).parent.mkdir(mode=0o755, parents=True)
        r.atomic(r.PROGRAM, receiver_bytes, 0o755)
        execute('useradd', '--system', '--home-dir', str(r.AUTH.parent.parent),
                '--no-create-home', '--shell', '/bin/sh', 'tanem-publish')
        # An unknown random password keeps account-level SSH policies compatible;
        # only the forced Ed25519 keys are used by the automation.
        random_password = os.urandom(48).hex()
        hashed = execute('openssl', 'passwd', '-6', '-stdin', input=random_password).stdout.strip()
        execute('usermod', '--password', hashed, 'tanem-publish')
        r.AUTH.parent.mkdir(mode=0o755, parents=True)
        r.AUTH.parent.parent.chmod(0o755)
        key_path = r.CONFIG / 'admin'
        execute('ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key_path))
        r.rebuild_auth()
        sudoers = f'tanem-publish ALL=(root) NOPASSWD: {r.PROGRAM} *\n'
        with tempfile.NamedTemporaryFile(mode='w') as check:
            check.write(sudoers)
            check.flush()
            execute('visudo', '-cf', check.name)
        r.atomic('/etc/sudoers.d/tanem-publish', sudoers, 0o440)
        key_fields = Path('/etc/ssh/ssh_host_ed25519_key.pub').read_text().split()[:2]
        alias = host if port == 22 else f'[{host}]:{port}'
        bundle = {'host': host, 'port': port, 'user': 'tanem-publish',
                  'known_hosts': alias + ' ' + ' '.join(key_fields) + '\n',
                  'private_key': key_path.read_text()}
        r.write_json(r.CONFIG / 'credentials.json', bundle)
        # Record completed installation before local handshake: network-policy
        # failure must be retryable without duplicating users/keys.
        r.write_json(marker, {'receiver_sha256': digest, 'host': host, 'port': port})
    # Read-only local SSH smoke test; never restarts/reconfigures sshd.
    with tempfile.TemporaryDirectory() as temp:
        known = Path(temp) / 'known_hosts'
        known.write_text(bundle['known_hosts'])
        key = r.CONFIG / 'admin'
        alias = host if port == 22 else f'[{host}]:{port}'
        result = execute('ssh', '-F', '/dev/null', '-o', 'BatchMode=yes',
                         '-o', 'IdentitiesOnly=yes', '-o', 'ConnectTimeout=10',
                         '-o', 'StrictHostKeyChecking=yes', '-o', f'HostKeyAlias={alias}',
                         '-o', f'UserKnownHostsFile={known}', '-i', str(key), '-p', str(port),
                         'tanem-publish@127.0.0.1', input='{"action":"ping"}\n')
        if json.loads(result.stdout).get('scope') != 'admin':
            raise ValueError('Unexpected SSH self-test response')
    print('SETUP_OK: local SSH publisher test passed. No websites or DNS changed.')
    if not quiet:
        print('Copy the following single line ONLY into GitHub secret TANEM_SERVER. Do not send it in chat.')
        print(json.dumps(bundle, separators=(',', ':')))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='135.106.229.21')
    parser.add_argument('--port', type=int, default=22)
    parser.add_argument('--quiet', action='store_true')
    args = parser.parse_args()
    try:
        install(args.host, args.port, args.quiet)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print('SETUP_ERROR: ' + str(exc), file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError):
            print(exc.stderr.strip(), file=sys.stderr)
        sys.exit(1)

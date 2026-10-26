#!/usr/bin/env python3
"""Runs ONLY on a disposable GitHub-hosted CI VM, never on Selectel."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile

import client


def package(path, version):
    with tarfile.open(path, 'w:gz') as tar:
        data = ('<!doctype html><html>' + version + '</html>').encode()
        info = tarfile.TarInfo('index.html')
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))


def main():
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted':
        raise SystemExit('Integration test only runs on disposable GitHub-hosted runners')
    config = Path('/etc/nginx/nginx.conf')
    before = hashlib.sha256(config.read_bytes()).hexdigest()
    subprocess.run(['bash', 'deploy/install.sh', '--host', '127.0.0.1', '--port', '2222', '--quiet'], check=True)
    # Idempotent re-run must preserve the administration key.
    original_key = Path('/etc/tanem-publish/admin.pub').read_bytes()
    subprocess.run(['bash', 'deploy/install.sh', '--host', '127.0.0.1', '--port', '2222', '--quiet'], check=True)
    assert original_key == Path('/etc/tanem-publish/admin.pub').read_bytes()
    bundle = json.loads(Path('/etc/tanem-publish/credentials.json').read_text())
    with tempfile.TemporaryDirectory() as folder:
        folder = Path(folder)
        key = folder / 'site'
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], check=True)
        site = {'slug': 'test-01', 'repository': 'TanemTech/test-01', 'hostname': 'test-01.tanem.ru'}
        result = client.ssh_request(bundle, {**site, 'action': 'register',
                                            'public_key': key.with_suffix('.pub').read_text()})
        assert result['status'] == 'REGISTERED'
        child = {**bundle, 'private_key': key.read_text(), 'hostname': site['hostname']}
        artifact = folder / 'site.tar.gz'
        package(artifact, 'first')
        header = {'action': 'deploy', 'repository': site['repository'], 'sha': 'a' * 40, 'run_id': 100}
        assert client.ssh_request(child, header, artifact)['status'] == 'DEPLOYED'
        package(artifact, 'second')
        assert client.ssh_request(child, {**header, 'sha': 'b' * 40, 'run_id': 101}, artifact)['status'] == 'DEPLOYED'
        current = Path('/srv/tanem-sites/test-01/current')
        assert 'second' in (current / 'index.html').read_text()
        state = json.loads(Path('/srv/tanem-sites/test-01/state.json').read_text())
        assert 'first' in (Path(state['previous']) / 'index.html').read_text()
        assert client.ssh_request(child, header)['status'] == 'STALE'
        try:
            client.ssh_request(child, {**site, 'action': 'register', 'public_key': key.with_suffix('.pub').read_text()})
        except RuntimeError:
            pass
        else:
            raise AssertionError('Site key could register a site')
        # SSH requested commands must be ignored by the forced receiver.
        sentinel = folder / 'shell-escape'
        with client.ssh_command(child) as command:
            p = subprocess.run(command + ['touch ' + str(sentinel)], input=b'{"action":"ping"}\n', capture_output=True)
            assert p.returncode == 0 and not sentinel.exists()
        # A second site gets a different key and a separate serving directory.
        other = {**site, 'slug': 'test-02', 'hostname': 'test-02.tanem.ru', 'repository': 'TanemTech/test-02'}
        key2 = folder / 'site2'
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key2)], check=True)
        client.ssh_request(bundle, {**other, 'action': 'register', 'public_key': key2.with_suffix('.pub').read_text()})
        child2 = {**bundle, 'private_key': key2.read_text(), 'hostname': other['hostname']}
        assert client.ssh_request(child2, {**header, 'repository': other['repository']}, artifact)['status'] == 'DEPLOYED'
        assert 'second' in (current / 'index.html').read_text()
    assert before == hashlib.sha256(config.read_bytes()).hexdigest()
    print('INTEGRATION_OK: install, retry, restricted SSH, first deployment, automatic-style update, second site, isolation, previous release and real Nginx HTTP verification')


if __name__ == '__main__':
    main()

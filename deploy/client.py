#!/usr/bin/env python3
"""Trusted runner code. Artifact content is treated as data, never executed."""
import argparse
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from receiver import public_key, safe_name, validate_site

API = 'https://api.github.com'


def load_credentials(name):
    raw = os.environ.get(name, '')
    try:
        bundle = json.loads(raw)
    except json.JSONDecodeError:
        # The installer encodes real key/known_hosts line endings as literal
        # JSON \\n escapes. A web-console copy can additionally insert physical
        # CR/LF every terminal column; remove only those wrapping characters.
        try:
            bundle = json.loads(raw.replace('\r', '').replace('\n', ''))
        except json.JSONDecodeError:
            raise ValueError(name + ': invalid JSON; copy the complete credential object') from None
    fields = ('host', 'user', 'private_key', 'known_hosts')
    if not isinstance(bundle, dict) or any(not isinstance(bundle.get(f), str) or not bundle[f] for f in fields):
        raise ValueError(name + ': credential fields are missing or invalid')
    if type(bundle.get('port')) is not int or not 1 <= bundle['port'] <= 65535:
        raise ValueError(name + ': invalid SSH port')
    return bundle


def api(path, method='GET', body=None, allow_missing=False):
    req = urllib.request.Request(API + path, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
                 'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json',
                 'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'TANEM-publisher'})
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            data = response.read()
            return json.loads(data) if data else None
    except urllib.error.HTTPError as exc:
        if allow_missing and exc.code == 404:
            return None
        # Response bodies may contain submitted data; never dump secrets to logs.
        raise RuntimeError(f'GitHub API {method} {path}: HTTP {exc.code}') from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def download_artifact(repo, artifact_id, dest):
    url = f'{API}/repos/{repo}/actions/artifacts/{artifact_id}/zip'
    request = urllib.request.Request(url, headers={
        'Authorization': 'Bearer ' + os.environ['GH_TOKEN'], 'User-Agent': 'TANEM-publisher'})
    try:
        response = urllib.request.build_opener(NoRedirect).open(request, timeout=45)
    except urllib.error.HTTPError as exc:
        if exc.code not in (301, 302, 303, 307, 308):
            raise RuntimeError(f'Artifact download failed: HTTP {exc.code}') from None
        target = exc.headers['Location']
        if urllib.parse.urlparse(target).scheme != 'https':
            raise ValueError('Artifact redirect must use HTTPS')
        # Signed blob URL: deliberately do not forward GitHub Authorization.
        response = urllib.request.urlopen(target, timeout=60)
    with response, open(dest, 'wb') as out:
        shutil.copyfileobj(response, out, 1024 * 1024)


@contextlib.contextmanager
def ssh_command(bundle):
    if not re.fullmatch(r'[0-9.]+', bundle['host']) or bundle['user'] != 'tanem-publish':
        raise ValueError('Invalid server credential bundle')
    with tempfile.TemporaryDirectory() as temp:
        key = Path(temp) / 'key'
        key.write_text(bundle['private_key'])
        key.chmod(0o600)
        known = Path(temp) / 'known_hosts'
        known.write_text(bundle['known_hosts'])
        yield ['ssh', '-F', '/dev/null', '-T', '-o', 'BatchMode=yes',
               '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes',
               '-o', 'ConnectTimeout=15', '-o', 'ServerAliveInterval=15',
               '-o', 'ServerAliveCountMax=3', '-o', f'UserKnownHostsFile={known}',
               '-i', str(key), '-p', str(int(bundle['port'])),
               bundle['user'] + '@' + bundle['host']]


def ssh_request(bundle, header, archive=None):
    with ssh_command(bundle) as command, tempfile.TemporaryFile() as payload:
        payload.write(json.dumps(header).encode() + b'\n')
        if archive is not None:
            with open(archive, 'rb') as source:
                shutil.copyfileobj(source, payload, 1024 * 1024)
        payload.seek(0)
        result = subprocess.run(command, stdin=payload, capture_output=True, timeout=900)
        if result.returncode:
            raise RuntimeError('SSH publisher failed: ' + result.stderr.decode(errors='replace').strip())
        return json.loads(result.stdout)


def check_run(repo, run_id, workflow_file):
    run = api(f'/repos/{repo}/actions/runs/{int(run_id)}')
    head = api(f'/repos/{repo}/branches/main')['commit']['sha']
    if (run.get('conclusion') != 'success' or run.get('status') != 'completed'
            or run.get('event') not in ('push', 'workflow_dispatch')
            or run.get('head_branch') != 'main' or run.get('head_sha') != head
            or run.get('head_repository', {}).get('full_name') != repo
            or run.get('path') != '.github/workflows/' + workflow_file):
        raise ValueError('Only a successful approved workflow for CURRENT main may be published')
    return run


def repack(archive_path, output):
    """Normalize either upload-artifact ZIP or the existing Pages artifact.tar."""
    names = set()
    has_html = False
    with zipfile.ZipFile(archive_path) as source, tarfile.open(output, 'w:gz') as dest:
        files = [m for m in source.infolist() if not m.is_dir()]
        if len(files) == 1 and files[0].filename == 'artifact.tar':
            with source.open(files[0]) as raw, tarfile.open(fileobj=raw, mode='r|*') as inner:
                for member in inner:
                    if member.isdir():
                        continue
                    if not member.isfile() or member.issparse():
                        raise ValueError('Artifact contains links or special files')
                    name = safe_name(member.name)
                    if name in names:
                        raise ValueError('Duplicate artifact path')
                    names.add(name)
                    info = tarfile.TarInfo(name)
                    info.size = member.size
                    info.mode = 0o644
                    with inner.extractfile(member) as data:
                        dest.addfile(info, data)
                    has_html |= name == 'index.html'
        else:
            for member in files:
                if (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError('Artifact contains symlink')
                name = safe_name(member.filename)
                if name in names:
                    raise ValueError('Duplicate artifact path')
                names.add(name)
                info = tarfile.TarInfo(name)
                info.size = member.file_size
                info.mode = 0o644
                with source.open(member) as data:
                    dest.addfile(info, data)
                has_html |= name == 'index.html'
    if not has_html:
        raise ValueError('Approved artifact does not contain index.html')


def publish(bundle, repo, run_id, workflow_file, artifact_name):
    run = check_run(repo, run_id, workflow_file)
    artifacts = api(f'/repos/{repo}/actions/runs/{int(run_id)}/artifacts?per_page=100')['artifacts']
    matches = [a for a in artifacts if a['name'] == artifact_name and not a['expired']]
    if len(matches) != 1:
        raise ValueError('Exactly one unexpired approved site artifact is required')
    with tempfile.TemporaryDirectory() as temp:
        archive = Path(temp) / 'artifact.zip'
        output = Path(temp) / 'site.tar.gz'
        download_artifact(repo, matches[0]['id'], archive)
        repack(archive, output)
        check_run(repo, run_id, workflow_file)
        result = ssh_request(bundle, {'action': 'deploy', 'repository': repo,
                             'sha': run['head_sha'], 'run_id': int(run_id)}, output)
    if result['status'] not in ('DEPLOYED', 'UNCHANGED'):
        raise ValueError('Publication was superseded: ' + result['status'])
    print(json.dumps(result))
    # External check from the GitHub runner, without needing DNS/REG.RU.
    hostname = bundle['hostname']
    req = urllib.request.Request('http://' + bundle['host'] + '/__tanem_release',
                                 headers={'Host': hostname})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=20) as response:
        if response.read(128).decode().strip() != run['head_sha']:
            raise ValueError('External HTTP check returned a different release')
    print('EXTERNAL_HTTP_OK: ' + hostname + ' at ' + run['head_sha'])


def validate_registry(root):
    sites, slugs, repos = [], set(), set()
    for path in sorted(Path(root).glob('*.json')):
        site = json.loads(path.read_text())
        validate_site(site)
        if path.stem != site['slug'] or site['slug'] in slugs or site['repository'] in repos:
            raise ValueError('Duplicate or mismatched registry entry')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+\.ya?ml', site['workflow_file']):
            raise ValueError('Invalid approved workflow file')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', site['artifact']):
            raise ValueError('Invalid artifact name')
        slugs.add(site['slug'])
        repos.add(site['repository'])
        sites.append(site)
    return sites


def onboard(bundle, root):
    sites = validate_registry(root)
    result = ssh_request(bundle, {'action': 'ping'})
    if result.get('scope') != 'admin':
        raise ValueError('TANEM_SERVER must contain the administration key')
    for site in sites:
        repo = site['repository']
        # Preflight repository/token/workflow existence before any server enrollment.
        meta = api(f'/repos/{repo}')
        if meta['default_branch'] != 'main':
            raise ValueError('Expected main branch: ' + repo)
        api(f'/repos/{repo}/actions/secrets/public-key')
        api(f'/repos/{repo}/contents/.github/workflows/selectel-site.yml?ref=main')
        api(f'/repos/{repo}/actions/workflows/{site["workflow_file"]}')
        status = ssh_request(bundle, {**site, 'action': 'status'})
        saved_secret = api(f'/repos/{repo}/actions/secrets/TANEM_SITE_SSH', allow_missing=True)
        if status['status'] == 'CONNECTED' and saved_secret is not None:
            print('ALREADY_CONNECTED: ' + repo)
            continue
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'site_key'
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(path)], check=True)
            pub = public_key(path.with_suffix('.pub').read_text())
            response = ssh_request(bundle, {**site, 'action': 'register', 'public_key': pub})
            if response['status'] != 'REGISTERED':
                raise ValueError('Registration failed')
            child = {**bundle, 'private_key': path.read_text(), 'hostname': site['hostname']}
            if ssh_request(child, {'action': 'ping'}).get('scope') != site['slug']:
                raise ValueError('Scoped SSH key self-test failed')
            subprocess.run(['gh', 'secret', 'set', 'TANEM_SITE_SSH', '--repo', repo],
                           input=json.dumps(child), text=True, capture_output=True, check=True)
        # Explicit dispatch also works if the triggering code change was made by
        # a GitHub App and recursive push workflows were suppressed.
        api(f'/repos/{repo}/actions/workflows/{site["workflow_file"]}/dispatches',
            'POST', {'ref': 'main'})
        ssh_request(bundle, {**site, 'action': 'confirm', 'public_key': pub})
        print('CONNECTED: ' + repo + '; validation and publication requested')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['onboard', 'publish', 'registry'])
    parser.add_argument('--registry', default='deploy/sites')
    parser.add_argument('--repo')
    parser.add_argument('--run-id', type=int)
    parser.add_argument('--workflow')
    parser.add_argument('--artifact')
    args = parser.parse_args()
    try:
        if args.action == 'registry':
            print(json.dumps(validate_registry(args.registry)))
        elif args.action == 'onboard':
            onboard(load_credentials('TANEM_SERVER'), args.registry)
        else:
            publish(load_credentials('TANEM_SITE_SSH'), args.repo,
                    args.run_id, args.workflow, args.artifact)
    except Exception as exc:
        print('ERROR: ' + str(exc), file=sys.stderr)
        sys.exit(1)

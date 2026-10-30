#!/usr/bin/env python3
"""Enable Pages centrally; never copy an administrator token into client repositories."""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

from client import api


def request_file(path):
    entry = json.loads(Path(path).read_text())
    repo = entry.get('repository', '')
    if not re.fullmatch(r'TanemTech/[A-Za-z0-9][A-Za-z0-9_.-]*', repo):
        raise ValueError('Expected a TanemTech client repository')
    name = repo.split('/')[1]
    if name.lower().startswith('copy-') or name.lower() == 'tanemtech.github.io':
        raise ValueError('Source templates and the account website cannot be enrolled')
    if Path(path).name != name + '.json':
        raise ValueError('Pages request filename must match the repository name')
    return repo


def changed_requests(before, after):
    if not re.fullmatch(r'[0-9a-f]{40}', before or '') or not re.fullmatch(r'[0-9a-f]{40}', after or ''):
        raise ValueError('Expected full commit SHAs for the Pages request change')
    if before == '0' * 40:
        return sorted(Path('deploy/pages').glob('*.json'))
    result = subprocess.run(['git', 'diff', '--name-only', '--diff-filter=AM', before, after,
                             '--', 'deploy/pages/'], check=True, capture_output=True, text=True)
    return [Path(p) for p in result.stdout.splitlines()
            if re.fullmatch(r'deploy/pages/[A-Za-z0-9][A-Za-z0-9_.-]*\.json', p)]


def contents(repo, path, allow_missing=False):
    data = api(f'/repos/{repo}/contents/{path}?ref=main', allow_missing=allow_missing)
    return None if data is None else base64.b64decode(data['content']).decode('utf-8')


def apply_reviewed_visibility(repo, reviewed_refs):
    """Only an explicit owner-approved request may expose a reviewed repository.

    Pin every Git ref, including PR refs, so a new branch or changed history
    invalidates the review. Ordinary Pages enrollment never changes visibility.
    """
    if reviewed_refs is None:
        return
    if repo == 'TanemTech/test-01':
        raise ValueError('The controller cannot change its own visibility')
    if (not isinstance(reviewed_refs, dict) or 'refs/heads/main' not in reviewed_refs
            or any(not isinstance(ref, str) or not ref.startswith('refs/')
                   or not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{40}', sha)
                   for ref, sha in reviewed_refs.items())):
        raise ValueError('Public visibility requires the reviewed Git refs and full SHAs')
    meta = api(f'/repos/{repo}')
    if meta.get('archived') or meta.get('default_branch') != 'main':
        raise ValueError('Public visibility requires an active client repository with main')
    if meta.get('private') is False:
        return  # The one-time transition is already complete.
    refs = api(f'/repos/{repo}/git/refs?per_page=100')
    if not isinstance(refs, list) or len(refs) >= 100:
        raise ValueError('Cannot verify the complete reviewed Git ref set')
    actual = {item['ref']: item['object']['sha'] for item in refs}
    if actual != reviewed_refs:
        raise ValueError('Repository refs changed after review; visibility left unchanged')
    api(f'/repos/{repo}', method='PATCH', body={'private': False})
    if api(f'/repos/{repo}').get('private') is not False:
        raise RuntimeError('GitHub did not confirm public repository visibility')
    print('PUBLIC_VISIBILITY_CONFIRMED: ' + repo, flush=True)


def configure(repo):
    meta = api(f'/repos/{repo}')
    if meta.get('archived') or meta.get('default_branch') != 'main':
        raise ValueError('Pages enrollment requires an active client repository with main')
    source = contents(repo, '.github/workflows/pages.yml')
    if 'actions/deploy-pages@' not in source or 'workflow_dispatch:' not in source:
        raise ValueError('Client needs its template Pages workflow with workflow_dispatch')
    # Legacy client configurations may publish to Selectel after a Pages run.
    # Refuse those rather than cause an unapproved production deployment.
    match = re.search(r'^name:\s*(.+)\s*$', source, re.MULTILINE)
    if match is None:
        raise ValueError('Pages workflow needs an explicit name')
    pages_name = match.group(1).strip().strip('\"\'')
    workflows = api(f'/repos/{repo}/contents/.github/workflows?ref=main')
    for item in workflows:
        if item['name'] == 'pages.yml' or not item['name'].endswith(('.yml', '.yaml')):
            continue
        text = contents(repo, '.github/workflows/' + item['name'])
        if 'workflow_run:' in text and pages_name in text:
            raise ValueError('Another workflow follows Pages runs; separate its production release first: ' + item['name'])
    pages = api(f'/repos/{repo}/pages', allow_missing=True)
    if pages is None:
        try:
            api(f'/repos/{repo}/pages', method='POST', body={'build_type': 'workflow'})
        except RuntimeError as exc:
            if meta.get('private'):
                raise RuntimeError(
                    'GitHub could not enable Pages for this private repository. '
                    f'API response: {exc}. '
                    'Private-source Pages requires GitHub Pro for a personal account, '
                    'or Team/Enterprise, and a token with Pages: write and Administration: write. '
                    'The repository visibility was not changed.'
                ) from None
            raise
        pages = api(f'/repos/{repo}/pages')
    if pages.get('cname') or pages.get('build_type') != 'workflow':
        raise ValueError('Existing Pages uses a custom domain or another build source; left unchanged')
    expected = 'https://tanemtech.github.io/' + repo.split('/')[1] + '/'
    if pages.get('html_url', '').rstrip('/') != expected.rstrip('/'):
        raise ValueError('Unexpected Pages address; no build requested')
    return expected


def runs(repo):
    return api(f'/repos/{repo}/actions/workflows/pages.yml/runs?event=workflow_dispatch&branch=main&per_page=100')['workflow_runs']


def wait_run(repo, sha, previous, deadline):
    while time.monotonic() < deadline:
        matches = [r for r in runs(repo) if r['id'] > previous and r['head_sha'] == sha
                   and r.get('event') == 'workflow_dispatch' and r.get('head_branch') == 'main']
        if matches:
            run = max(matches, key=lambda r: r['id'])
            if run['status'] == 'completed':
                if run['conclusion'] != 'success':
                    raise RuntimeError('Pages build did not succeed: ' + run['html_url'])
                return run
        time.sleep(10)
    raise TimeoutError('Pages build is not ready yet; check the client Actions run')


def wait_http(url, deadline):
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=20) as response:
                if response.status == 200 and response.geturl().rstrip('/') == url.rstrip('/'):
                    return
        except (urllib.error.URLError, TimeoutError):
            pass
        time.sleep(10)
    raise TimeoutError('Pages build completed but its HTTPS link is not ready yet')


def connect(repo):
    url = configure(repo)
    sha = api(f'/repos/{repo}/branches/main')['commit']['sha']
    previous = max((r['id'] for r in runs(repo)), default=0)
    api(f'/repos/{repo}/actions/workflows/pages.yml/dispatches', method='POST', body={'ref': 'main'})
    print('PAGES_BUILD_REQUESTED: ' + repo, flush=True)
    deadline = time.monotonic() + 900
    run = wait_run(repo, sha, previous, deadline)
    # A successful workflow that skipped deployment (e.g. unfilled template data)
    # is not a published preview. Check actual deployment jobs as well.
    jobs = api(f'/repos/{repo}/actions/runs/{run["id"]}/jobs?per_page=100')['jobs']
    if not any(j.get('conclusion') == 'success' and any(
            s.get('conclusion') == 'success' and 'deploy' in s.get('name', '').lower()
            and 'pages' in s.get('name', '').lower() for s in j.get('steps', [])) for j in jobs):
        raise ValueError('Pages deployment was skipped or absent; complete the client site input files')
    wait_http(url, time.monotonic() + 120)
    print('PAGES_READY: ' + url, flush=True)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as out:
            out.write(f'Готовая ссылка: [{url}]({url})\n\nСборка: {run["html_url"]}\n\n')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--before')
    parser.add_argument('--after')
    args = parser.parse_args()
    paths = changed_requests(args.before, args.after) if args.before else sorted(Path('deploy/pages').glob('*.json'))
    if not paths:
        print('NO_NEW_PAGES_REQUESTS')
        return
    if not os.environ.get('GH_TOKEN'):
        raise ValueError('Add TANEM_PAGES_TOKEN once in TanemTech/test-01 Actions secrets')
    for path in paths:
        repo = request_file(path)
        entry = json.loads(path.read_text())
        apply_reviewed_visibility(repo, entry.get('public_visibility_reviewed_refs'))
        connect(repo)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('PAGES_NOT_READY: ' + str(exc), file=sys.stderr)
        print('For HTTP 403/404 verify token repository access and Pages/Administration/Actions permissions.', file=sys.stderr)
        sys.exit(1)

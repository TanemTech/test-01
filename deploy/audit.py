#!/usr/bin/env python3
"""Read-only check of the installed publisher and GitHub release prerequisites."""
import json
import sys

from client import api, load_credentials, ssh_request, validate_registry


def main():
    bundle = load_credentials('TANEM_SERVER')
    if ssh_request(bundle, {'action': 'ping'}).get('scope') != 'admin':
        raise ValueError('TANEM_SERVER does not authenticate as publisher admin')
    print('SELECTEL_SSH_OK', flush=True)
    failures = []
    for site in validate_registry('deploy/sites'):
        repo = site['repository']
        state = ssh_request(bundle, {**site, 'action': 'status'})
        print('SELECTEL_STATUS: ' + repo + ': ' + state['status'], flush=True)
        env_path = f'/repos/{repo}/environments/tanem-production'
        checks = {
            'environment': env_path,
            'environment_ssh_secret': env_path + '/secrets/TANEM_SITE_SSH',
            'release_permission': env_path + '/variables/TANEM_RELEASE_PERMISSION',
            'legacy_repository_ssh_secret': f'/repos/{repo}/actions/secrets/TANEM_SITE_SSH',
        }
        for name, path in checks.items():
            try:
                data = api(path, allow_missing=True)
                if name == 'environment':
                    reviewers = [r.get('reviewer', {}).get('login')
                                 for rule in (data or {}).get('protection_rules', [])
                                 if rule.get('type') == 'required_reviewers'
                                 for r in rule.get('reviewers', [])]
                    ok = 'TanemTech' in reviewers
                elif name == 'release_permission':
                    ok = (data or {}).get('value') == 'enabled'
                elif name == 'legacy_repository_ssh_secret':
                    ok = data is None
                else:
                    ok = data is not None
                print(f'{name}: {"OK" if ok else "NEEDS_SETUP"}', flush=True)
                if not ok:
                    failures.append(repo + ': ' + name)
            except RuntimeError as exc:
                print(f'{name}: UNVERIFIED ({exc})', flush=True)
                failures.append(repo + ': ' + name + ' access')
    if failures:
        raise ValueError('Publication prerequisites incomplete: ' + ', '.join(failures))
    print('SELECTEL_PREREQUISITES_OK (no publication performed)')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('AUDIT_INCOMPLETE: ' + str(exc), file=sys.stderr)
        sys.exit(1)

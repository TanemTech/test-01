#!/usr/bin/env python3
"""Finish the existing test-01 approval setup and key migration; never publish."""
import json
from pathlib import Path
import sys
import tempfile

from client import api, load_credentials, onboard, validate_registry

REPO = 'TanemTech/test-01'
ENV = '/repos/' + REPO + '/environments/tanem-production'


def owner_reviewers(env):
    return [reviewer.get('reviewer', {}).get('login')
            for rule in env.get('protection_rules', [])
            if rule.get('type') == 'required_reviewers'
            for reviewer in rule.get('reviewers', [])]


def repair(bundle, registry='deploy/sites'):
    sites = [site for site in validate_registry(registry) if site['repository'] == REPO]
    if len(sites) != 1 or sites[0]['slug'] != 'test-01':
        raise ValueError('Expected the existing test-01 registration; no other site may be changed')
    meta = api('/repos/' + REPO)
    if meta.get('owner', {}).get('login') != 'TanemTech' or meta.get('private'):
        raise ValueError('Expected the existing public TanemTech/test-01 repository')
    env = api(ENV, allow_missing=True)
    variable = api(ENV + '/variables/TANEM_RELEASE_PERMISSION', allow_missing=True) if env else None
    if variable is not None and variable.get('value') != 'enabled':
        raise ValueError('Publication was explicitly disabled; leave that decision unchanged')
    if env and 'TanemTech' not in owner_reviewers(env):
        # An empty auto-created environment can be completed. Existing custom
        # protection rules are never replaced by this one-time repair.
        if env.get('protection_rules') or env.get('deployment_branch_policy'):
            raise ValueError('Existing environment protection differs; no changes made')
    if not env or 'TanemTech' not in owner_reviewers(env):
        api(ENV, method='PUT', body={
            'reviewers': [{'type': 'User', 'id': meta['owner']['id']}],
            'prevent_self_review': False,
        })
    verified = api(ENV)
    if 'TanemTech' not in owner_reviewers(verified):
        raise ValueError('Required approval was not saved; no SSH key will be migrated')
    if variable is None:
        api(ENV + '/variables', method='POST', body={
            'name': 'TANEM_RELEASE_PERMISSION', 'value': 'enabled',
        })
    # Only this already-registered test site, even if more sites were added later.
    with tempfile.TemporaryDirectory() as root:
        (Path(root) / 'test-01.json').write_text(json.dumps(sites[0]))
        onboard(bundle, root)
    print('TEST_CONNECTION_REPAIRED_NOT_PUBLISHED')


if __name__ == '__main__':
    try:
        repair(load_credentials('TANEM_SERVER'))
    except Exception as exc:
        print('REPAIR_INCOMPLETE: ' + str(exc), file=sys.stderr)
        sys.exit(1)

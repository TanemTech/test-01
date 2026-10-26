#!/usr/bin/env python3
"""Generate a client workflow; used by ChatGPT/the technical specialist."""
import argparse
import json
from pathlib import Path
import re

from receiver import validate_site


def render(site, revision):
    validate_site(site)
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('Pin publisher code to an inspected full commit SHA')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+\.ya?ml', site['workflow_file']):
        raise ValueError('Invalid workflow filename')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', site['artifact']):
        raise ValueError('Invalid artifact name')
    name = site['workflow_name']
    if not isinstance(name, str) or not name.strip():
        raise ValueError('Exact existing validation workflow name is required')
    template = (Path(__file__).parent / 'templates/site.yml.in').read_text()
    return template.replace('WORKFLOW_NAME', json.dumps(name, ensure_ascii=False)).replace(
        'PUBLISHER_REVISION', revision).replace('WORKFLOW_FILE', site['workflow_file']).replace(
        'ARTIFACT_NAME', site['artifact'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('site_json')
    parser.add_argument('--revision', required=True)
    args = parser.parse_args()
    print(render(json.loads(Path(args.site_json).read_text()), args.revision), end='')

#!/usr/bin/python3
"""Disposable GitHub-hosted Nginx HTTPS integration test; never run on Selectel."""
import importlib.util
import os
from pathlib import Path
import ssl
import subprocess
import sys
import urllib.error
import urllib.request


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main():
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted':
        raise SystemExit('Only disposable GitHub-hosted runner may run this test')
    site = {'slug': 'test-01', 'repository': 'TanemTech/test-01', 'hostname': 'test-01.tanem.ru'}
    receiver = load('deploy/https/receiver.py', 'https_receiver')
    agent = load('deploy/https/agent.py', 'https_agent')
    root = Path('/var/lib/tanem-https')
    (root / '.well-known/acme-challenge').mkdir(parents=True, exist_ok=True)
    (root / '.well-known/acme-challenge/probe').write_text('challenge-is-served')
    redirects = Path('/etc/tanem-https/redirects')
    redirects.mkdir(parents=True, exist_ok=True)
    http = Path('/etc/nginx/conf.d/tanem-auto-test-01.conf')
    assert http.exists(), 'publisher CI integration must run first'
    http.write_text(receiver.nginx_config(site))
    subprocess.run(['/usr/sbin/nginx', '-t'], check=True)
    subprocess.run(['/usr/sbin/nginx', '-s', 'reload'], check=True)
    req = urllib.request.Request('http://127.0.0.1/.well-known/acme-challenge/probe',
                                 headers={'Host': 'test-01.tanem.ru'})
    assert urllib.request.urlopen(req).read() == b'challenge-is-served'
    cert = Path('/etc/letsencrypt/live/test-01.tanem.ru')
    cert.mkdir(parents=True)
    subprocess.run(['openssl', 'req', '-x509', '-nodes', '-newkey', 'rsa:2048',
                    '-keyout', str(cert / 'privkey.pem'), '-out', str(cert / 'fullchain.pem'),
                    '-days', '1', '-subj', '/CN=test-01.tanem.ru'], check=True,
                   capture_output=True)
    (Path('/etc/nginx/conf.d') / 'tanem-https-test-01.conf').write_text(agent.tls_config(site))
    (redirects / 'test-01.conf').write_text('return 308 https://test-01.tanem.ru$request_uri;\n')
    subprocess.run(['/usr/sbin/nginx', '-t'], check=True)
    subprocess.run(['/usr/sbin/nginx', '-s', 'reload'], check=True)
    try:
        opener = urllib.request.build_opener(type('NoRedirect', (urllib.request.HTTPRedirectHandler,), {
            'redirect_request': lambda self, req, fp, code, msg, headers, url: None,
        })())
        opener.open(urllib.request.Request('http://127.0.0.1/',
                    headers={'Host': 'test-01.tanem.ru'}))
    except urllib.error.HTTPError as response:
        assert response.code == 308
        assert response.headers['Location'] == 'https://test-01.tanem.ru/'
    else:
        raise AssertionError('HTTP did not redirect to HTTPS')
    context = ssl._create_unverified_context()  # Self-signed only inside disposable CI.
    req = urllib.request.Request('https://127.0.0.1/', headers={'Host': 'test-01.tanem.ru'})
    # SNI uses IP, but this disposable runner has only one 443 test server.
    with urllib.request.urlopen(req, context=context) as response:
        assert b'second' in response.read()
    req = urllib.request.Request('http://127.0.0.1/__tanem_release',
                                 headers={'Host': 'test-01.tanem.ru'})
    assert urllib.request.urlopen(req).read().strip() == b'b' * 40
    print('HTTPS_INTEGRATION_OK: HTTP-01, redirect, TLS site and release endpoint')


if __name__ == '__main__':
    main()

#!/usr/bin/python3
"""TANEM HTTPS reconciler. Only enrolled publisher-owned sites; no DNS changes."""
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time

sys.path.insert(0, '/usr/local/lib/tanem-publish')
import receiver as publisher

ROOT = Path('/srv/tanem-sites')
REGISTRY = Path('/etc/tanem-publish/sites')
TLS = Path('/etc/tanem-https')
NGINX = Path('/etc/nginx/conf.d')
WEBROOT = Path('/var/lib/tanem-https')
CERTS = Path('/etc/letsencrypt/live')
COOLDOWN_SECONDS = 24 * 3600


def execute(*cmd):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=180).stdout


def public_dns_ok(hostname, server_ip):
    try:
        answers = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
        a = {a[4][0] for a in answers if a[0] == socket.AF_INET}
        aaaa = {a[4][0] for a in answers if a[0] == socket.AF_INET6}
        return a == {server_ip} and not aaaa
    except socket.gaierror:
        return False


def tls_config(site):
    slug, hostname = site['slug'], site['hostname']
    root = ROOT / slug / 'current'
    cert = CERTS / hostname
    return f'''# TANEM HTTPS: managed separately from HTTP publisher.
server {{
    listen 443 ssl;
    server_name {hostname};
    root {root};
    index index.html;
    ssl_certificate {cert}/fullchain.pem;
    ssl_certificate_key {cert}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    location = /__tanem_release {{ return 404; }}
    location ~ /\\. {{ deny all; }}
    location / {{ try_files $uri $uri/ =404; }}
}}
'''


def valid_certificate(hostname):
    cert = CERTS / hostname / 'cert.pem'
    if not cert.is_file():
        return False
    for options in (('-checkend', '86400'), ('-checkhost', hostname)):
        result = subprocess.run(['/usr/bin/openssl', 'x509', '-in', str(cert), '-noout', *options],
                                capture_output=True, text=True, timeout=15)
        if result.returncode:
            return False
        if options[0] == '-checkhost' and 'does match certificate' not in result.stdout:
            return False
    return True


def renewal_account():
    accounts = set()
    for conf in Path('/etc/letsencrypt/renewal').glob('*.conf'):
        text = conf.read_text()
        found = re.search(r'^account\s*=\s*([a-zA-Z0-9_-]+)\s*$', text, re.M)
        if found:
            accounts.add(found.group(1))
    if len(accounts) != 1:
        raise RuntimeError('Need exactly one existing Certbot account; no new account created')
    return next(iter(accounts))


def issue_certificate(hostname, account):
    attempt = TLS / 'attempts' / (hostname + '.json')
    now = int(time.time())
    if attempt.exists():
        previous = json.loads(attempt.read_text()).get('time', 0)
        if now - previous < COOLDOWN_SECONDS:
            print('HTTPS_RETRY_LATER:', hostname)
            return False
    attempt.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    attempt.write_text(json.dumps({'time': now}))
    cmd = ['/usr/bin/certbot', 'certonly', '--webroot', '-w', str(WEBROOT),
           '--cert-name', hostname, '-d', hostname, '--account', account,
           '--non-interactive', '--agree-tos', '--no-eff-email',
           '--deploy-hook', '/usr/local/lib/tanem-https/reload.sh']
    try:
        execute(*cmd)
        return True
    except subprocess.CalledProcessError as exc:
        print('HTTPS_ISSUANCE_FAILED:', hostname, (exc.stderr or '')[-300:], file=sys.stderr)
        return False


def activate(site):
    slug = site['slug']
    ssl = NGINX / ('tanem-https-' + slug + '.conf')
    redirect = TLS / 'redirects' / (slug + '.conf')
    expected_tls = tls_config(site)
    expected_redirect = f'return 308 https://{site["hostname"]}$request_uri;\n'
    if ssl.exists() and ssl.read_text() != expected_tls:
        raise RuntimeError('Existing HTTPS configuration differs: ' + str(ssl))
    if redirect.exists() and redirect.read_text() != expected_redirect:
        raise RuntimeError('Existing redirect configuration differs: ' + str(redirect))
    if ssl.exists() and redirect.exists():
        return
    old_ssl = ssl.read_bytes() if ssl.exists() else None
    old_redirect = redirect.read_bytes() if redirect.exists() else None
    try:
        if not ssl.exists():
            publisher.atomic(ssl, expected_tls, 0o644)
        if not redirect.exists():
            publisher.atomic(redirect, expected_redirect, 0o644)
        execute('/usr/sbin/nginx', '-t')
        execute('/usr/sbin/nginx', '-s', 'reload')
    except BaseException:
        if old_ssl is None:
            ssl.unlink(missing_ok=True)
        else:
            publisher.atomic(ssl, old_ssl, 0o644)
        if old_redirect is None:
            redirect.unlink(missing_ok=True)
        else:
            publisher.atomic(redirect, old_redirect, 0o644)
        execute('/usr/sbin/nginx', '-t')
        execute('/usr/sbin/nginx', '-s', 'reload')
        raise
    print('HTTPS_ENABLED:', site['hostname'])


def reconcile():
    if os.geteuid() != 0:
        raise RuntimeError('HTTPS agent requires root')
    installed = publisher.read_json(Path('/etc/tanem-publish/installed.json'))
    server_ip = str(ipaddress.IPv4Address(installed['host']))
    if not Path('/usr/bin/certbot').exists():
        raise RuntimeError('Existing certbot not found; do not auto-install')
    if not WEBROOT.is_dir():
        raise RuntimeError('HTTPS webroot is absent; run guarded installer first')
    for site_path in sorted(REGISTRY.glob('*.json')):
        try:
            site = json.loads(site_path.read_text())
            publisher.validate_site(site)
            slug, hostname = site['slug'], site['hostname']
            if site_path.stem != slug or not site.get('connected'):
                continue
            current = ROOT / slug / 'current'
            http = NGINX / ('tanem-auto-' + slug + '.conf')
            if not current.is_symlink() or not (current / 'index.html').is_file():
                continue
            if not http.exists() or http.read_text() != publisher.nginx_config(site):
                print('HTTPS_SKIP_CHANGED_HTTP:', hostname)
                continue
            if not valid_certificate(hostname):
                if not public_dns_ok(hostname, server_ip):
                    print('HTTPS_WAIT_DNS:', hostname)
                    continue
                if not issue_certificate(hostname, renewal_account()):
                    continue
                if not valid_certificate(hostname):
                    print('HTTPS_CERT_NOT_VALID:', hostname)
                    continue
            activate(site)
        except Exception as exc:
            print('HTTPS_SITE_ERROR:', site_path.name, str(exc), file=sys.stderr)


if __name__ == '__main__':
    with (TLS / 'agent.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        reconcile()

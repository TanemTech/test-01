#!/usr/bin/env python3
"""Remove only the managed test-01 site; run locally as root on Selectel."""
import fcntl
import importlib.util
import os
from pathlib import Path
import shutil

SITE = {'slug': 'test-01', 'repository': 'TanemTech/test-01',
        'hostname': 'test-01.tanem.ru'}


def remove(r):
    base = r.ROOT / SITE['slug']
    registry = r.CONFIG / 'sites/test-01.json'
    retired = r.CONFIG / 'retiring-test-01.json'
    conf = r.NGINX / 'tanem-auto-test-01.conf'
    backup = r.CONFIG / 'retiring-test-01.nginx'

    # Every destructive target must be an ordinary publisher-owned path.
    for path in (r.ROOT, r.CONFIG, r.CONFIG / 'sites', r.NGINX,
                 base, registry, retired, conf, backup):
        if path.is_symlink() or os.path.ismount(path):
            raise ValueError('Unexpected link or mount: ' + str(path))
    if registry.exists() and retired.exists():
        raise ValueError('Conflicting removal state; inspect before proceeding')
    record = registry if registry.exists() else retired
    if record.exists():
        if r.validate_site(r.read_json(record)) != SITE:
            raise ValueError('Site binding differs from the approved test site')
    elif base.exists() or conf.exists() or backup.exists():
        raise ValueError('Unregistered files: refusing to remove unmanaged data')
    else:
        r.rebuild_auth()
        r.run('/usr/sbin/nginx', '-t')
        print('ALREADY_REMOVED: test-01')
        return

    if (base / 'pending.json').exists():
        raise ValueError('Unfinished publication; inspect before removal')
    if (base / 'state.json').exists():
        if r.read_json(base / 'state.json').get('repository') != SITE['repository']:
            raise ValueError('Release belongs to another repository')
    if conf.exists() and backup.exists():
        raise ValueError('Conflicting Nginx removal state')
    for path in (conf, backup):
        if path.exists() and path.read_text() != r.nginx_config(SITE):
            raise ValueError('Nginx config was changed; refusing to remove it')

    # Keep recoverable metadata until all steps complete. A retry continues safely.
    if conf.exists():
        os.replace(conf, backup)
    try:
        r.run('/usr/sbin/nginx', '-t')
        r.run('/usr/sbin/nginx', '-s', 'reload')
    except BaseException:
        if backup.exists():
            os.replace(backup, conf)
            r.run('/usr/sbin/nginx', '-t')
            r.run('/usr/sbin/nginx', '-s', 'reload')
        raise

    if registry.exists():
        os.replace(registry, retired)
    r.rebuild_auth()  # Revoke only this site's key; keep admin and other site keys.
    if base.exists():
        shutil.rmtree(base)
    backup.unlink(missing_ok=True)
    retired.unlink(missing_ok=True)
    r.run('/usr/sbin/nginx', '-t')
    if r.conflict(SITE['hostname'], r.nginx_dump()):
        raise RuntimeError('Hostname still exists in Nginx; inspection required')
    assert not any(p.exists() for p in (base, registry, retired, conf, backup))
    print('REMOVED: test-01; files, releases, Nginx config and site SSH key removed')


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run this script as root in the Selectel server console')
    source = Path('/usr/local/lib/tanem-publish/receiver.py')
    if source.is_symlink() or source.stat().st_uid != 0 or source.stat().st_mode & 0o022:
        raise SystemExit('Installed receiver must be root-owned and not writable by others')
    spec = importlib.util.spec_from_file_location('installed_receiver', source)
    receiver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(receiver)
    with (receiver.CONFIG / 'publish.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        remove(receiver)


if __name__ == '__main__':
    main()

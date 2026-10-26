import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import receiver as r
import client as c


def archive(files=None):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz') as tar:
        for name, data in (files or {'index.html': b'<html>v1</html>', 'assets/photo.webp': b'image'}).items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
    stream.seek(0)
    return stream


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'sites'
        self.conf = self.base / 'nginx'
        self.config = self.base / 'config'
        for path in (self.root, self.conf, self.config / 'sites'):
            path.mkdir(parents=True)
        self.patches = [patch.object(r, 'ROOT', self.root), patch.object(r, 'NGINX', self.conf),
                        patch.object(r, 'CONFIG', self.config), patch.object(r, 'run'),
                        patch.object(r, 'nginx_dump', return_value=''), patch.object(r, 'probe')]
        self.mocks = [p.start() for p in self.patches]
        self.site = {'slug': 'test-01', 'repository': 'TanemTech/test-01', 'hostname': 'test-01.tanem.ru'}
        self.request = {'repository': self.site['repository'], 'sha': 'a' * 40, 'run_id': 100}

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def deploy(self, request=None, files=None):
        return r.deploy(self.site, request or self.request, archive(files))

    def test_new_site_then_update_and_bounded_backup(self):
        self.assertEqual(self.deploy()['status'], 'DEPLOYED')
        current = self.root / 'test-01/current'
        first = os.readlink(current)
        second = {**self.request, 'sha': 'b' * 40, 'run_id': 101}
        self.deploy(second, {'index.html': b'<html>v2</html>'})
        self.assertEqual((current / 'index.html').read_bytes(), b'<html>v2</html>')
        self.assertFalse((current / 'assets/photo.webp').exists())
        self.assertTrue(Path(first).exists())
        self.deploy({**self.request, 'sha': 'c' * 40, 'run_id': 102})
        self.assertEqual(len(list((self.root / 'test-01/releases').iterdir())), 2)
        self.assertFalse(Path(first).exists())

    def test_failed_health_restores_previous_site_and_state(self):
        self.deploy()
        current = self.root / 'test-01/current'
        previous = os.readlink(current)
        self.mocks[-1].side_effect = RuntimeError('health failed')
        with self.assertRaisesRegex(RuntimeError, 'health failed'):
            self.deploy({**self.request, 'sha': 'b' * 40, 'run_id': 101})
        self.assertEqual(os.readlink(current), previous)
        self.assertEqual(r.read_json(self.root / 'test-01/state.json')['sha'], 'a' * 40)
        self.assertFalse((self.root / 'test-01/pending.json').exists())

    def test_failed_first_site_removes_only_own_config(self):
        existing = self.conf / 'client.conf'
        existing.write_text('existing customer')
        self.mocks[-1].side_effect = RuntimeError('health failed')
        with self.assertRaises(RuntimeError):
            self.deploy()
        self.assertEqual(existing.read_text(), 'existing customer')
        self.assertFalse((self.conf / 'tanem-auto-test-01.conf').exists())
        self.assertFalse((self.root / 'test-01/current').exists())

    def test_nginx_failure_restores_old_version(self):
        self.deploy()
        current = self.root / 'test-01/current'
        previous = os.readlink(current)
        self.mocks[3].side_effect = RuntimeError('nginx -t failed')
        with self.assertRaises(RuntimeError):
            self.deploy({**self.request, 'sha': 'b' * 40, 'run_id': 101})
        self.assertEqual(os.readlink(current), previous)

    def test_pending_transaction_recovers_after_interrupted_process(self):
        self.deploy()
        base = self.root / 'test-01'
        previous = os.readlink(base / 'current')
        state = r.read_json(base / 'state.json')
        r.write_json(base / 'pending.json', {'current': previous, 'state': state, 'new_config': False})
        r.link(base / 'current', str(base / 'nonexistent'))
        r.recover(base)
        self.assertEqual(os.readlink(base / 'current'), previous)
        self.assertEqual(r.read_json(base / 'state.json'), state)

    def test_stale_run_cannot_replace_newer_site(self):
        self.deploy()
        result = self.deploy({**self.request, 'sha': 'b' * 40, 'run_id': 99})
        self.assertEqual(result['status'], 'STALE')
        self.assertEqual((self.root / 'test-01/current/.tanem-release').read_text().strip(), 'a' * 40)

    def test_same_run_different_commit_rejected(self):
        self.deploy()
        with self.assertRaises(ValueError):
            self.deploy({**self.request, 'sha': 'b' * 40})

    def test_other_repository_rejected(self):
        with self.assertRaises(ValueError):
            self.deploy({**self.request, 'repository': 'TanemTech/other'})
        self.assertFalse((self.root / 'test-01').exists())

    def test_existing_hostname_never_overwritten(self):
        self.mocks[4].return_value = 'server_name test-01.tanem.ru;'
        with self.assertRaises(ValueError):
            self.deploy()
        self.assertEqual(list(self.conf.iterdir()), [])

    def test_changed_managed_nginx_config_not_overwritten(self):
        self.deploy()
        conf = self.conf / 'tanem-auto-test-01.conf'
        conf.write_text('operator changes')
        with self.assertRaises(ValueError):
            self.deploy({**self.request, 'run_id': 101})
        self.assertEqual(conf.read_text(), 'operator changes')

    def test_missing_html_rejected_without_switch(self):
        self.deploy()
        with self.assertRaises(ValueError):
            self.deploy({**self.request, 'run_id': 101}, {'image.webp': b'photo'})
        self.assertEqual(r.read_json(self.root / 'test-01/state.json')['run_id'], 100)

    def test_traversal_and_secret_and_executable_rejected(self):
        for name in ('../escape.html', '/etc/nginx/a.html', 'assets/../../x.html',
                     '.env', '.github/secrets.json', 'server.py', 'deploy.sh', 'a\\b.html'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                r.unpack(archive({name: b'x'}), self.base)
        self.assertFalse((self.base.parent / 'escape.html').exists())

    def test_tar_symlink_and_hardlink_rejected(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE):
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode='w:gz') as tar:
                item = tarfile.TarInfo('index.html')
                item.type = kind
                item.linkname = '/etc/passwd'
                tar.addfile(item)
            stream.seek(0)
            with self.assertRaises(ValueError):
                r.unpack(stream, self.base)

    def test_insufficient_disk_does_not_switch(self):
        self.deploy()
        with patch.object(r.shutil, 'disk_usage', return_value=type('Disk', (), {'free': 1})()):
            with self.assertRaises(ValueError):
                self.deploy({**self.request, 'run_id': 101})
        self.assertEqual(r.read_json(self.root / 'test-01/state.json')['run_id'], 100)

    def test_site_identifier_cannot_inject_config(self):
        for slug in ('../etc', 'test;}', '-bad', 'bad\nserver', 'a.b'):
            with self.assertRaises(ValueError):
                r.validate_site({**self.site, 'slug': slug})

    def test_wildcard_hostname_conflict(self):
        self.assertTrue(r.conflict('test.tanem.ru', 'server_name *.tanem.ru;'))
        self.assertFalse(r.conflict('test.tanem.ru', 'server_name marina-grig.tanem.ru;'))

    def test_site_key_cannot_register_or_change_scope(self):
        r.write_json(self.config / 'sites/test-01.json', self.site)
        with self.assertRaises(ValueError):
            r.handle('test-01', {'action': 'register', **self.site}, io.BytesIO())

    def test_bad_public_key_cannot_inject_authorized_keys(self):
        with self.assertRaises(ValueError):
            r.public_key('ssh-ed25519 AAAA\ncommand="id"')


class ClientTests(unittest.TestCase):
    def test_pages_artifact_normalization(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            inner = io.BytesIO()
            with tarfile.open(fileobj=inner, mode='w') as tar:
                for name in ('./index.html', './assets/photo.webp', './.nojekyll'):
                    item = tarfile.TarInfo(name)
                    content = b'<html>valid</html>' if name.endswith('html') else b''
                    item.size = len(content)
                    tar.addfile(item, io.BytesIO(content))
            with zipfile.ZipFile(folder / 'input.zip', 'w') as z:
                z.writestr('artifact.tar', inner.getvalue())
            c.repack(folder / 'input.zip', folder / 'output.tar.gz')
            target = folder / 'site'
            target.mkdir()
            with open(folder / 'output.tar.gz', 'rb') as stream:
                self.assertEqual(r.unpack(stream, target)[0], 3)

    def test_regular_zip_artifact(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            with zipfile.ZipFile(folder / 'input.zip', 'w') as z:
                z.writestr('index.html', '<html>valid</html>')
                z.writestr('assets/photo.webp', 'image')
            c.repack(folder / 'input.zip', folder / 'output.tar.gz')
            with tarfile.open(folder / 'output.tar.gz') as tar:
                self.assertEqual(tar.getnames(), ['index.html', 'assets/photo.webp'])

    def test_failed_foreign_stale_and_pr_runs_rejected(self):
        run = {'conclusion': 'success', 'status': 'completed', 'event': 'push',
               'head_branch': 'main', 'head_sha': 'a' * 40,
               'head_repository': {'full_name': 'TanemTech/test-01'},
               'path': '.github/workflows/validate.yml'}
        for changed in ({'conclusion': 'failure'}, {'event': 'pull_request'},
                        {'head_sha': 'b' * 40}, {'head_branch': 'feature'},
                        {'path': '.github/workflows/untrusted.yml'},
                        {'head_repository': {'full_name': 'attacker/fork'}}):
            with patch.object(c, 'api', side_effect=[{**run, **changed}, {'commit': {'sha': 'a' * 40}}]):
                with self.assertRaises(ValueError):
                    c.check_run('TanemTech/test-01', 10, 'validate.yml')


if __name__ == '__main__':
    unittest.main()

import base64
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pages


class PagesTests(unittest.TestCase):
    def test_rejects_other_owners_templates_and_mismatched_filename(self):
        with tempfile.TemporaryDirectory() as root:
            for repo in ['Other/test-01', 'TanemTech/COPY-Tanem-Salons-Template',
                         'TanemTech/tanemtech.github.io', 'TanemTech/../test-01', 'TanemTech/test-02']:
                path = Path(root) / 'test-01.json'
                path.write_text(json.dumps({'repository': repo}))
                with self.subTest(repo=repo), self.assertRaises(ValueError):
                    pages.request_file(path)
            path.write_text('{"repository":"TanemTech/test-01"}')
            self.assertEqual(pages.request_file(path), 'TanemTech/test-01')

    def fake_api(self, page=None, other_workflow=None):
        calls = []
        enabled = dict(page) if page else None
        source = 'name: Client Pages\non:\n  workflow_dispatch:\njobs:\n  deploy:\n    uses: actions/deploy-pages@v4\n'
        def fake(path, method='GET', body=None, **kwargs):
            nonlocal enabled
            calls.append((path, method, body))
            if path.endswith('/pages'):
                if method == 'POST':
                    enabled = {'build_type': 'workflow', 'html_url': 'https://tanemtech.github.io/test-01/', 'cname': None}
                return enabled
            if path.endswith('/contents/.github/workflows?ref=main'):
                return [{'name': 'selectel-site.yml'}] if other_workflow else []
            if '/contents/' in path:
                text = other_workflow if 'selectel-site.yml' in path else source
                return {'content': base64.b64encode(text.encode()).decode()}
            return {'default_branch': 'main', 'private': False, 'archived': False}
        return fake, calls

    def test_enables_actions_pages_once_without_touching_secrets_or_selectel(self):
        fake, calls = self.fake_api()
        with patch.object(pages, 'api', side_effect=fake):
            self.assertEqual(pages.configure('TanemTech/test-01'), 'https://tanemtech.github.io/test-01/')
            pages.configure('TanemTech/test-01')
        writes = [(p, m, b) for p, m, b in calls if m != 'GET']
        self.assertEqual(writes, [('/repos/TanemTech/test-01/pages', 'POST', {'build_type': 'workflow'})])

    def test_preserves_existing_custom_domain_and_legacy_build(self):
        for page in [dict(cname='example.com', build_type='workflow'), dict(cname=None, build_type='legacy')]:
            fake, calls = self.fake_api(page)
            with self.subTest(page=page), patch.object(pages, 'api', side_effect=fake), self.assertRaises(ValueError):
                pages.configure('TanemTech/test-01')
            self.assertTrue(all(method == 'GET' for _, method, _ in calls))

    def test_does_not_trigger_production_that_follows_pages(self):
        fake, calls = self.fake_api(other_workflow='on:\n  workflow_run:\n    workflows: [Client Pages]\n')
        with patch.object(pages, 'api', side_effect=fake), self.assertRaisesRegex(ValueError, 'production release'):
            pages.configure('TanemTech/test-01')
        self.assertTrue(all(method == 'GET' for _, method, _ in calls))

    def test_refuses_private_repo_before_enabling_anything(self):
        with patch.object(pages, 'api', return_value={'private': True, 'default_branch': 'main'}) as api:
            with self.assertRaises(ValueError):
                pages.configure('TanemTech/test-01')
            self.assertEqual(api.call_count, 1)

    def test_waits_for_new_run_with_matching_revision(self):
        good = dict(id=12, head_sha='a' * 40, event='workflow_dispatch', head_branch='main', status='completed', conclusion='success')
        wrong = dict(good, id=13, head_sha='b' * 40)
        old = dict(good, id=9)
        with patch.object(pages, 'runs', return_value=[old, wrong, good]):
            self.assertEqual(pages.wait_run('TanemTech/test-01', 'a' * 40, 10, pages.time.monotonic() + 5), good)

    def test_failed_build_is_not_reported_ready(self):
        run = dict(id=12, head_sha='a' * 40, event='workflow_dispatch', head_branch='main', status='completed', conclusion='failure', html_url='https://github.com/run')
        with patch.object(pages, 'runs', return_value=[run]), self.assertRaises(RuntimeError):
            pages.wait_run('TanemTech/test-01', 'a' * 40, 10, pages.time.monotonic() + 5)

    def test_skipped_deployment_is_not_ready(self):
        with patch.object(pages, 'configure', return_value='https://tanemtech.github.io/test-01/'), \
             patch.object(pages, 'runs', return_value=[]), \
             patch.object(pages, 'wait_run', return_value={'id': 12}), \
             patch.object(pages, 'api', side_effect=[{'commit': {'sha': 'a' * 40}}, None, {'jobs': [{'conclusion': 'skipped', 'steps': []}]}]), \
             patch.object(pages, 'wait_http') as http:
            with self.assertRaisesRegex(ValueError, 'skipped'):
                pages.connect('TanemTech/test-01')
            http.assert_not_called()

    def test_changes_only_process_added_or_modified_requests(self):
        output = 'deploy/pages/test-01.json\ndeploy/pages/nested/ignored.json\n'
        with patch.object(pages.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout=output)) as run:
            self.assertEqual(pages.changed_requests('a' * 40, 'b' * 40), [Path('deploy/pages/test-01.json')])
            self.assertIn('--diff-filter=AM', run.call_args.args[0])

    def test_missing_selectel_key_fails_instead_of_green_skip(self):
        root = Path(__file__).resolve().parents[2]
        for relative in ['.github/workflows/selectel-site.yml', 'deploy/templates/site.yml.in']:
            source = (root / relative).read_text()
            script = source.split('      - name: Check connection\n', 1)[1].split('        run: |\n', 1)[1].split('      - uses:', 1)[0]
            script = '\n'.join(line[10:] for line in script.splitlines())
            result = subprocess.run(['bash', '-e', '-c', script], env={'TANEM_SITE_SSH': ''}, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()

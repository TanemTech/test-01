import base64
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
    def test_visibility_is_unchanged_without_explicit_review(self):
        with patch.object(pages, 'api') as api:
            pages.apply_reviewed_visibility('TanemTech/test-02', None)
        api.assert_not_called()

    def test_controller_visibility_cannot_be_changed(self):
        with patch.object(pages, 'api') as api, self.assertRaises(ValueError):
            pages.apply_reviewed_visibility('TanemTech/test-01', {'refs/heads/main': 'a' * 40})
        api.assert_not_called()

    def test_public_visibility_only_for_exact_reviewed_refs(self):
        reviewed = {'refs/heads/main': 'a' * 40, 'refs/heads/build': 'b' * 40}
        refs = [{'ref': ref, 'object': {'sha': sha}} for ref, sha in reviewed.items()]
        with patch.object(pages, 'api', side_effect=[
            {'private': True, 'default_branch': 'main'}, refs, {}, {'private': False},
        ]) as api, patch('builtins.print'):
            pages.apply_reviewed_visibility('TanemTech/test-02', reviewed)
        self.assertEqual([c for c in api.call_args_list if c.kwargs.get('method') == 'PATCH'], [
            unittest.mock.call('/repos/TanemTech/test-02', method='PATCH', body={'private': False}),
        ])

    def test_changed_or_added_refs_block_public_visibility(self):
        reviewed = {'refs/heads/main': 'a' * 40}
        for refs in [
            [{'ref': 'refs/heads/main', 'object': {'sha': 'b' * 40}}],
            [{'ref': 'refs/heads/main', 'object': {'sha': 'a' * 40}},
             {'ref': 'refs/heads/new', 'object': {'sha': 'b' * 40}}],
            [],
        ]:
            with self.subTest(refs=refs), patch.object(pages, 'api', side_effect=[
                {'private': True, 'default_branch': 'main'}, refs,
            ]) as api, self.assertRaises(ValueError):
                pages.apply_reviewed_visibility('TanemTech/test-02', reviewed)
            self.assertTrue(all(c.kwargs.get('method', 'GET') == 'GET' for c in api.call_args_list))

    def test_invalid_visibility_review_fails_without_api_access(self):
        for reviewed in [False, {}, [], {'refs/heads/main': 'short'}, {'refs/heads/main': 1}]:
            with self.subTest(reviewed=reviewed), patch.object(pages, 'api') as api, self.assertRaises(ValueError):
                pages.apply_reviewed_visibility('TanemTech/test-02', reviewed)
            api.assert_not_called()

    def test_public_visibility_request_is_idempotent(self):
        with patch.object(pages, 'api', return_value={'private': False, 'default_branch': 'main'}) as api:
            pages.apply_reviewed_visibility('TanemTech/test-02', {'refs/heads/main': 'a' * 40})
        api.assert_called_once_with('/repos/TanemTech/test-02')

    def test_visibility_requires_confirmation_from_github(self):
        with patch.object(pages, 'api', side_effect=[
            {'private': True, 'default_branch': 'main'},
            [{'ref': 'refs/heads/main', 'object': {'sha': 'a' * 40}}], {}, {'private': True},
        ]), self.assertRaises(RuntimeError):
            pages.apply_reviewed_visibility('TanemTech/test-02', {'refs/heads/main': 'a' * 40})

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

    def fake_api(self, page=None, other_workflow=None, private=False, archived=False, default_branch='main', page_create_error=None):
        calls = []
        enabled = dict(page) if page else None
        source = 'name: Client Pages\non:\n  workflow_dispatch:\njobs:\n  deploy:\n    uses: actions/deploy-pages@v4\n'
        def fake(path, method='GET', body=None, **kwargs):
            nonlocal enabled
            calls.append((path, method, body))
            if path.endswith('/pages'):
                if method == 'POST':
                    if page_create_error:
                        raise RuntimeError(page_create_error)
                    enabled = {'build_type': 'workflow', 'html_url': 'https://tanemtech.github.io/test-01/', 'cname': None}
                return enabled
            if path.endswith('/contents/.github/workflows?ref=main'):
                return [{'name': 'selectel-site.yml'}] if other_workflow else []
            if '/contents/' in path:
                text = other_workflow if 'selectel-site.yml' in path else source
                return {'content': base64.b64encode(text.encode()).decode()}
            return {'default_branch': default_branch, 'private': private, 'archived': archived}
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

    def test_allows_private_repository_and_keeps_visibility_private(self):
        fake, calls = self.fake_api(private=True)
        with patch.object(pages, 'api', side_effect=fake):
            self.assertEqual(pages.configure('TanemTech/test-01'), 'https://tanemtech.github.io/test-01/')
        writes = [(p, m, b) for p, m, b in calls if m != 'GET']
        self.assertEqual(writes, [('/repos/TanemTech/test-01/pages', 'POST', {'build_type': 'workflow'})])
        repo_metadata = [entry for entry in calls if entry[0] == '/repos/TanemTech/test-01']
        self.assertEqual(len(repo_metadata), 1)

    def test_private_repository_reports_plan_or_token_requirement(self):
        fake, calls = self.fake_api(private=True, page_create_error='GitHub API POST /repos/TanemTech/test-01/pages: HTTP 422')
        with patch.object(pages, 'api', side_effect=fake):
            with self.assertRaisesRegex(RuntimeError, 'Private-source Pages requires GitHub Pro'):
                pages.configure('TanemTech/test-01')

    def test_refuses_archived_or_non_main_repositories(self):
        for options in [{'archived': True}, {'default_branch': 'master'}]:
            fake, calls = self.fake_api(**options)
            with self.subTest(options=options), patch.object(pages, 'api', side_effect=fake), self.assertRaises(ValueError):
                pages.configure('TanemTech/test-01')
            self.assertEqual(len(calls), 1)

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
             patch.object(pages, 'wait_http') as http, patch('builtins.print'):
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

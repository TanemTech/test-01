"""The publisher must never deploy merely because someone pushed or connected a repo."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]


class ManualReleasePolicy(unittest.TestCase):
    def text(self, relative):
        return (ROOT / relative).read_text()

    def test_build_is_manual_only(self):
        s = self.text('.github/workflows/validate.yml')
        header = s.split('\npermissions:', 1)[0]
        self.assertIn('  workflow_dispatch:', header)
        self.assertNotIn('  push:', header)
        self.assertNotIn('  pull_request:', header)

    def test_real_publication_requires_manual_dispatch_and_approval(self):
        s = self.text('.github/workflows/selectel-site.yml')
        self.assertIn("github.event.workflow_run.event == 'workflow_dispatch'", s)
        self.assertIn('environment: tanem-production', s)
        self.assertIn('TANEM_RELEASE_PERMISSION', s)
        self.assertNotIn("github.event.workflow_run.event == 'push'", s)

    def test_new_site_template_cannot_autopublish(self):
        s = self.text('deploy/templates/site.yml.in')
        self.assertIn("github.event.workflow_run.event == 'workflow_dispatch'", s)
        self.assertIn('environment: tanem-production', s)
        self.assertIn('TANEM_RELEASE_PERMISSION', s)

    def test_connect_is_manual_and_does_not_publish(self):
        s = self.text('.github/workflows/selectel-connect.yml')
        header = s.split('\npermissions:', 1)[0]
        self.assertIn('  workflow_dispatch:', header)
        self.assertNotIn('  push:', header)
        client = self.text('deploy/client.py')
        onboard = client.split('def onboard(', 1)[1].split("\n\nif __name__", 1)[0]
        self.assertNotIn('/dispatches', onboard)
        self.assertIn('CONNECTED_NOT_PUBLISHED', onboard)

    def test_registered_test_site_points_to_manual_workflow(self):
        s = self.text('deploy/sites/test-01.json')
        self.assertIn('"workflow_name": "Publish TANEM site (manual request)"', s)


if __name__ == '__main__':
    unittest.main()

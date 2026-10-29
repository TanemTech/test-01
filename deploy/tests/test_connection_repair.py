import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import repair_test_connection as repair


class ConnectionRepairTests(unittest.TestCase):
    site = {'slug': 'test-01', 'repository': 'TanemTech/test-01', 'hostname': 'test-01.tanem.ru'}
    meta = {'private': False, 'owner': {'login': 'TanemTech', 'id': 333898059}}
    protected = {'protection_rules': [{'type': 'required_reviewers', 'reviewers': [{'reviewer': {'login': 'TanemTech'}}]}]}

    def test_denied_environment_write_does_not_touch_server_or_keys(self):
        with patch.object(repair, 'validate_registry', return_value=[self.site]), \
             patch.object(repair, 'api', side_effect=[self.meta, None, RuntimeError('HTTP 403')]), \
             patch.object(repair, 'onboard') as onboard:
            with self.assertRaises(RuntimeError):
                repair.repair({})
            onboard.assert_not_called()

    def test_disabled_publication_is_preserved(self):
        with patch.object(repair, 'validate_registry', return_value=[self.site]), \
             patch.object(repair, 'api', side_effect=[self.meta, self.protected, {'value': 'disabled'}]) as api, \
             patch.object(repair, 'onboard') as onboard:
            with self.assertRaisesRegex(ValueError, 'explicitly disabled'):
                repair.repair({})
            self.assertTrue(all('method' not in c.kwargs for c in api.call_args_list))
            onboard.assert_not_called()

    def test_preserves_other_custom_protection(self):
        existing = {'protection_rules': [{'type': 'wait_timer', 'wait_timer': 10}]}
        with patch.object(repair, 'validate_registry', return_value=[self.site]), \
             patch.object(repair, 'api', side_effect=[self.meta, existing, None]) as api, \
             patch.object(repair, 'onboard') as onboard:
            with self.assertRaisesRegex(ValueError, 'protection differs'):
                repair.repair({})
            self.assertTrue(all('method' not in c.kwargs for c in api.call_args_list))
            onboard.assert_not_called()

    def test_only_existing_test_site_is_migrated(self):
        other = dict(self.site, repository='TanemTech/other', slug='other')
        def check(bundle, root):
            files = list(Path(root).glob('*.json'))
            self.assertEqual(len(files), 1)
            self.assertEqual(json.loads(files[0].read_text()), self.site)
        with patch.object(repair, 'validate_registry', return_value=[self.site, other]), \
             patch.object(repair, 'api', side_effect=[self.meta, None, {}, self.protected, None]) as api, \
             patch.object(repair, 'onboard', side_effect=check), patch('builtins.print'):
            repair.repair({})
            writes = [c for c in api.call_args_list if 'method' in c.kwargs]
            self.assertEqual(len(writes), 2)
            self.assertEqual(writes[0].kwargs['body']['reviewers'], [{'type': 'User', 'id': 333898059}])
            self.assertNotIn('dispatches', str(api.call_args_list))


if __name__ == '__main__':
    unittest.main()

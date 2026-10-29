"""Upgrade old FTP drafts without weakening persisted transaction guards."""
from contextlib import nullcontext
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from server_setting.model import FIELDS
from server_setting.root_actions import Host, RootActionError, RootManager, RECIPES


LEGACY = {'anonymous_enable': 'NO', 'local_enable': 'YES', 'write_enable': 'NO',
          'pasv_min_port': '51000', 'pasv_max_port': '51100'}


class FTPHost(Host):
    def run(self, argv, timeout=30):
        if argv[0] == 'dpkg-query':
            output = 'install ok installed\n'
        elif argv[:2] == ['systemctl', 'show']:
            output = 'LoadState=loaded\nActiveState=inactive\nUnitFileState=disabled\n'
        elif argv[0] == 'systemctl':
            output = ''
        else:
            raise AssertionError(argv)
        return subprocess.CompletedProcess(argv, 0, output, '')


class FTPRecipeCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'etc').mkdir()
        (self.root / 'etc/os-release').write_text('ID=ubuntu\nVERSION_ID="26.04"\n')
        self.original = b'# Existing site configuration\r\nlisten=NO\r\nlisten_ipv6=YES\r\nxferlog_std_format=NO\r\n'
        self.config_path = self.root / 'etc/vsftpd.conf'
        self.config_path.write_bytes(self.original)
        self.host = FTPHost(self.root)
        self.manager = RootManager(self.host)
        self.recipe_path = self.host.path(RECIPES + '/vsftpd.json')
        self.saved = {'schema': 1, 'module': 'vsftpd', 'values': dict(LEGACY),
                      'service_active': None, 'service_enabled': None}
        self.recipe_path.parent.mkdir(parents=True, mode=0o700)
        self.recipe_path.parent.chmod(0o700)
        self.recipe_path.write_text(json.dumps(self.saved, indent=3) + '\n', encoding='utf-8')
        self.recipe_path.chmod(0o600)
        self.addCleanup(patch.stopall)
        patch.object(self.manager, '_gate').start()
        patch.object(self.manager, '_lock', return_value=nullcontext()).start()
        patch('server_setting.root_actions.os.geteuid', return_value=0, create=True).start()
        self.assertTrue(set(LEGACY) < set(FIELDS['vsftpd']), 'Expanded FTP fields are required')

    def test_exact_legacy_values_expand_defaults_preserve_custom_values_and_input(self):
        supplied = copy.deepcopy(LEGACY)
        normalized = self.manager._values('vsftpd', supplied)
        self.assertEqual(normalized, {**FIELDS['vsftpd'], **LEGACY})
        self.assertEqual(supplied, LEGACY)
        self.assertEqual(self.manager._values('vsftpd', normalized), normalized)

    def test_load_saved_returns_complete_draft_and_never_rewrites_disk(self):
        if os.name == 'posix':
            self.assertEqual(stat.S_IMODE(self.recipe_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.recipe_path.parent.stat().st_mode), 0o700)
            self.assertEqual(self.recipe_path.stat().st_uid, 0)
        before = self.recipe_path.read_bytes()
        loaded = self.manager.load_saved('vsftpd')
        self.assertEqual(loaded['values'], {**FIELDS['vsftpd'], **LEGACY})
        self.assertEqual(self.recipe_path.read_bytes(), before)
        self.assertEqual(self.config_path.read_bytes(), self.original)
        self.assertFalse((self.root / 'var/lib/3wa-server-setup').exists())

    def test_plan_lists_new_defaults_and_existing_values_before_any_write(self):
        recipe_before = self.recipe_path.read_bytes()
        plan = self.manager.plan('vsftpd', dict(LEGACY))
        self.assertEqual(plan['values'], {**FIELDS['vsftpd'], **LEGACY})
        for key, value in plan['values'].items():
            self.assertIn(key + '=' + value, plan['files'][0]['content'])
        self.assertEqual(self.recipe_path.read_bytes(), recipe_before)
        self.assertEqual(self.config_path.read_bytes(), self.original)

    def test_other_partial_unknown_and_invalid_values_remain_rejected(self):
        partial = dict(LEGACY)
        partial.pop('write_enable')
        for values in (partial, {**LEGACY, 'listen': 'YES'}, {**LEGACY, 'unknown': 'YES'},
                       {**FIELDS['vsftpd'], 'unknown': 'YES'}, {**LEGACY, 'local_enable': 'maybe'},
                       {**LEGACY, 'pasv_min_port': '52000', 'pasv_max_port': '51000'}):
            with self.subTest(values=values), self.assertRaises(RootActionError):
                self.manager._values('vsftpd', values)
        with self.assertRaises(RootActionError):
            self.manager._values('apache', dict(LEGACY))
        self.assertEqual(json.loads(self.recipe_path.read_text()), self.saved)

    def test_apply_saves_normalized_values_and_restore_keeps_original_recipe_and_bytes(self):
        result = self.manager.apply(self.manager.plan('vsftpd', dict(LEGACY)))
        self.assertEqual(result['status'], 'APPLIED')
        self.assertEqual(json.loads(self.recipe_path.read_text())['values'], {**FIELDS['vsftpd'], **LEGACY})
        restored = self.manager.restore_last('vsftpd')
        self.assertEqual(restored['status'], 'RESTORED')
        self.assertEqual(self.config_path.read_bytes(), self.original)
        self.assertEqual(json.loads(self.recipe_path.read_text()), self.saved)

    def test_failed_apply_restores_original_config_and_legacy_recipe(self):
        with patch('server_setting.root_recipes.verify_plan', side_effect=ValueError('fixed verification failure')):
            with self.assertRaises(RootActionError) as raised:
                self.manager.apply(self.manager.plan('vsftpd', dict(LEGACY)))
        self.assertEqual(raised.exception.code, 'ROLLED_BACK')
        self.assertEqual(self.config_path.read_bytes(), self.original)
        self.assertEqual(json.loads(self.recipe_path.read_text()), self.saved)

    def test_old_transaction_fingerprint_is_not_silently_reinterpreted_after_upgrade(self):
        with patch.dict(FIELDS, {'vsftpd': dict(LEGACY)}):
            result = self.manager.apply(self.manager.plan('vsftpd', dict(LEGACY)))
            self.assertEqual(result['status'], 'APPLIED')
        config_before = self.config_path.read_bytes()
        recipe_before = self.recipe_path.read_bytes()
        with self.assertRaises(RootActionError) as raised:
            self.manager.restore_plan('vsftpd')
        self.assertEqual(raised.exception.code, 'EXTERNAL_EDIT')
        self.assertEqual(self.config_path.read_bytes(), config_before)
        self.assertEqual(self.recipe_path.read_bytes(), recipe_before)

    def test_recipe_hash_guard_still_blocks_external_edits_after_new_apply(self):
        self.manager.apply(self.manager.plan('vsftpd', dict(LEGACY)))
        saved = json.loads(self.recipe_path.read_text())
        saved['values']['write_enable'] = 'YES'
        self.recipe_path.write_text(json.dumps(saved), encoding='utf-8')
        digest = hashlib.sha256(self.recipe_path.read_bytes()).hexdigest()
        with self.assertRaises(RootActionError) as raised:
            self.manager.restore_plan('vsftpd')
        self.assertEqual(raised.exception.code, 'EXTERNAL_EDIT')
        self.assertEqual(hashlib.sha256(self.recipe_path.read_bytes()).hexdigest(), digest)


if __name__ == '__main__':
    unittest.main()

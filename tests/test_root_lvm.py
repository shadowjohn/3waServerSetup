"""Root LV growth plans, stale targets and partial filesystem failures."""
import copy
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from server_setting.root_actions import RootActionError, RootManager

if importlib.util.find_spec('server_setting.root_lvm'):
    from server_setting.root_lvm import LVMHost, LVMManager
else:
    LVMHost = LVMManager = None


class RootLVMTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(LVMManager, 'Root LVM manager is not implemented')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root/'etc').mkdir()
        (self.root/'etc/os-release').write_text('ID=ubuntu\nVERSION_ID="26.04"\n')
        self.extent = 4*1024*1024
        self.size, self.free, self.fs_size = 256*self.extent, 256, 256*self.extent
        self.attr, self.segtype, self.fstype = '-wi-ao----', 'linear', 'ext4'
        self.calls, self.fail_fs, self.fail_command, self.bad_identity = [], False, False, False
        outer = self

        class FakeHost(LVMHost):
            def block_identity(self, path):
                if path == '/dev/test-vg/root-lv':
                    return (253, 9) if outer.bad_identity else (253, 0)
                return (8, 3)

            def plain_pv(self, path):
                return True

            def run(self, argv, timeout=30):
                outer.calls.append(list(argv))
                name = argv[0]
                if name == 'findmnt':
                    data = {'filesystems': [{'target': '/', 'source': '/dev/mapper/test--vg-root--lv',
                        'fstype': outer.fstype, 'options': 'rw,relatime', 'maj:min': '253:0'}]}
                elif name == 'vgs':
                    data = {'report': [{'vg': [{'vg_name':'test-vg', 'vg_uuid':'vg-uuid',
                        'vg_size':str(512*outer.extent), 'vg_free':str(outer.free*outer.extent),
                        'vg_extent_size':str(outer.extent), 'vg_free_count':str(outer.free)}]}]}
                elif name == 'lvs':
                    data = {'report': [{'seg': [{'lv_path':'/dev/test-vg/root-lv', 'lv_name':'root-lv',
                        'vg_name':'test-vg', 'lv_size':str(outer.size), 'lv_attr':outer.attr,
                        'segtype':outer.segtype, 'lv_role':'public', 'origin':'', 'pool_lv':'',
                        'lv_uuid':'lv-uuid', 'lv_kernel_major':'253', 'lv_kernel_minor':'0'}]}]}
                elif name == 'pvs':
                    data = {'report': [{'pv': [{'pv_name':'/dev/sda3', 'vg_name':'test-vg'}]}]}
                elif name == 'dumpe2fs':
                    return subprocess.CompletedProcess(argv, 0, f'Block count: {outer.fs_size//4096}\nBlock size: 4096\n', '')
                elif name == 'lvextend':
                    if outer.fail_command:
                        return subprocess.CompletedProcess(argv, 5, '', 'native failure')
                    if '--test' not in argv:
                        target = int(argv[argv.index('--extents')+1])*outer.extent
                        outer.free -= (target-outer.size)//outer.extent
                        outer.size = target
                        if not outer.fail_fs:
                            outer.fs_size = target
                    return subprocess.CompletedProcess(argv, int(outer.fail_fs), '', '')
                elif name == 'resize2fs':
                    if outer.fail_fs:
                        return subprocess.CompletedProcess(argv, 1, '', '')
                    outer.fs_size = outer.size
                    return subprocess.CompletedProcess(argv, 0, '', '')
                else:
                    raise AssertionError(argv)
                return subprocess.CompletedProcess(argv, 0, json.dumps(data), '')

        self.host = FakeHost(self.root)
        self.manager = LVMManager(self.host)
        self.addCleanup(patch.stopall)
        patch('server_setting.root_lvm.os.geteuid', return_value=0, create=True).start()
        @contextmanager
        def locked(manager):
            yield
        patch.object(RootManager, '_lock', locked).start()

    def test_status_and_plan_are_read_only_with_real_detected_target(self):
        state = self.manager.status()
        self.assertTrue(state['supported'])
        self.assertEqual(state['lv_path'], '/dev/test-vg/root-lv')
        plan = self.manager.plan('all')
        self.assertEqual(plan['target_bytes'], 512*self.extent)
        self.assertEqual(plan['grow_bytes'], 256*self.extent)
        self.assertEqual(plan['command'][-1], '/dev/test-vg/root-lv')
        self.assertEqual(plan['command'][plan['command'].index('--extents')+1], '512')
        self.assertNotIn('+100%FREE', plan['command'])
        self.assertFalse((self.root/'var').exists())
        self.assertFalse(any(call[0] in {'lvextend','resize2fs'} for call in self.calls))

    def test_apply_grows_both_lv_and_filesystem_and_is_journaled(self):
        plan = self.manager.plan('add', 1)
        result = self.manager.apply(plan)
        self.assertEqual(result['status'], 'LVM_APPLIED')
        self.assertEqual(self.size, 512*self.extent)
        self.assertEqual(self.fs_size, self.size)
        self.assertEqual(self.free, 0)
        self.assertEqual(sum(call[0]=='lvextend' and '--test' not in call for call in self.calls), 1)
        result = self.manager.apply(self.manager.plan('all'))
        self.assertEqual(result['status'], 'NO_CHANGE')

    def test_stale_preview_and_tampered_plan_never_execute_growth(self):
        plan = self.manager.plan('all')
        changed = copy.deepcopy(plan)
        changed['target_bytes'] += self.extent
        with self.assertRaises(RootActionError) as failure:
            self.manager.apply(changed)
        self.assertEqual(failure.exception.code, 'TAMPERED_PLAN')
        self.free -= 1
        with self.assertRaises(RootActionError) as failure:
            self.manager.apply(plan)
        self.assertEqual(failure.exception.code, 'STALE_PLAN')
        self.assertFalse(any(call[0]=='lvextend' for call in self.calls))

    def test_unsupported_layout_and_invalid_amount_are_rejected(self):
        for attribute, value in [('segtype','thin'), ('attr','twi-a-tz--'), ('fstype','btrfs'), ('bad_identity',True)]:
            previous = getattr(self, attribute)
            setattr(self, attribute, value)
            with self.subTest(attribute=attribute), self.assertRaises(RootActionError):
                self.manager.plan('all')
            setattr(self, attribute, previous)
        for amount in [0,-1,True,1.5,'1; command']:
            with self.subTest(amount=amount), self.assertRaises((RootActionError,ValueError)):
                self.manager.plan('add', amount)
        with self.assertRaises(RootActionError):
            self.manager.plan('add',2)

    def test_partial_growth_recovery_only_resizes_filesystem_and_never_adds_again(self):
        plan = self.manager.plan('all')
        self.fail_fs = True
        with self.assertRaises(RootActionError) as failure:
            self.manager.apply(plan)
        self.assertEqual(failure.exception.code, 'LVM_FS_RESIZE_REQUIRED')
        self.assertEqual(self.size, plan['target_bytes'])
        self.assertLess(self.fs_size, self.size)
        journal = list((self.root/'var/lib/3wa-server-setup/transactions').glob('*/journal.json'))[0]
        identifier = json.loads(journal.read_text())['transaction_id']
        self.fail_fs = False
        result = self.manager.recover(identifier)
        self.assertEqual(result['status'], 'LVM_APPLIED')
        self.assertEqual(self.fs_size, self.size)
        self.assertEqual(sum(call[0]=='lvextend' and '--test' not in call for call in self.calls),1)
        self.assertEqual(sum(call[0]=='resize2fs' for call in self.calls),1)

    def test_failed_command_does_not_claim_rollback_or_success(self):
        self.fail_command = True
        with self.assertRaises(RootActionError) as failure:
            self.manager.apply(self.manager.plan('all'))
        self.assertEqual(failure.exception.code,'LVM_FAILED')
        self.assertEqual(self.size,self.fs_size)

    def test_test_mode_is_explicitly_not_a_filesystem_resize(self):
        result = self.manager.check(self.manager.plan('all'))
        self.assertEqual(result['status'],'LVM_TEST_PASSED')
        command = next(call for call in self.calls if call[0]=='lvextend')
        self.assertIn('--test',command)
        self.assertNotIn('--resizefs',command)
        self.assertEqual(self.size,256*self.extent)

    def test_recover_before_command_crash_closes_failed_journal_without_growing(self):
        plan = self.manager.plan('all')
        root = self.manager.manager
        root._preflight_state()
        identifier = root._id()
        root._journal({'schema':1,'kind':'root-lv-growth','module':'lvm','status':'LVM_GROWING',
                       'transaction_id':identifier,'plan':plan})
        with self.assertRaises(RootActionError) as failure:
            self.manager.recover(identifier)
        self.assertEqual(failure.exception.code,'LVM_FAILED')
        self.assertEqual(root._journals()[-1]['status'],'LVM_FAILED')
        self.assertFalse(any(call[0] in {'lvextend','resize2fs'} for call in self.calls))


if __name__ == '__main__':
    unittest.main()

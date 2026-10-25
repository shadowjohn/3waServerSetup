"""Swap transactions use real files; only kernel commands are substituted."""
import copy
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

try:
    from server_setting.root_swap import SwapManager, SwapHost
except ImportError:
    SwapManager = SwapHost = None
from server_setting.root_actions import RootActionError


class SwapTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(SwapManager, 'Swap backend is not implemented')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for directory in ('etc', 'proc', 'run/systemd/generator'):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        (self.root / 'etc/os-release').write_text('ID=ubuntu\nVERSION_ID="26.04"\n')
        self.fstab = b'# preserve arbitrary bytes \xff\r\n/swap.img none swap sw 0 0\r\n'
        (self.root / 'etc/fstab').write_bytes(self.fstab)
        (self.root / 'proc/swaps').write_text('Filename\tType\tSize\tUsed\tPriority\n')
        (self.root / 'proc/meminfo').write_text('MemAvailable: 3000000 kB\n')
        self.calls = []
        self.fail_on = None
        self.fail_off = False
        self.free_bytes = 10 * 1024 ** 3
        self.unwritten = set()
        outer = self

        class FakeHost(SwapHost):
            def free_bytes(self):
                return outer.free_bytes

            def allocated(self, absolute, known_good=False):
                # Windows does not expose SEEK_HOLE/st_blocks. File writes,
                # renames, checksums, backup metadata and journals stay real.
                info = self.path(absolute).stat()
                return info.st_size > 0 and (info.st_ino not in outer.unwritten or known_good)

            def run(self, argv, timeout=30):
                outer.calls.append(list(argv))
                name = argv[0]
                if name == 'findmnt':
                    return subprocess.CompletedProcess(argv, 0, 'ext4\n', '')
                if name == 'systemctl':
                    return subprocess.CompletedProcess(argv, 0, 'LoadState=not-found\nFragmentPath=\nSourcePath=\nDropInPaths=\nActiveState=inactive\nWhat=\nTransient=no\n', '')
                if name == 'blkid':
                    target = self.path(argv[-1])
                    with target.open('rb') as stream:
                        magic = stream.read(80)
                    valid = magic.startswith(b'TEST_SWAP:')
                    value = magic.split(b'\n', 1)[0][10:].decode('ascii') if valid else ''
                    return subprocess.CompletedProcess(argv, 0 if valid else 2,
                                                       'DEVNAME=' + argv[-1] + '\nTYPE=swap\nUUID=' + value + '\n' if valid else '', '')
                if name == 'mkswap':
                    target = self.path(argv[-1])
                    with target.open('r+b') as stream:
                        stream.write(('TEST_SWAP:' + str(target.stat().st_ino) + '\n').encode())
                    return subprocess.CompletedProcess(argv, 0, '', '')
                if name in {'swapon', 'swapoff'}:
                    target = argv[-1]
                    if name == 'swapon' and outer.fail_on:
                        outer.fail_on -= 1
                        return subprocess.CompletedProcess(argv, 1, 'sensitive native diagnostic', '')
                    if name == 'swapoff' and outer.fail_off:
                        return subprocess.CompletedProcess(argv, 1, '', '')
                    rows = (outer.root / 'proc/swaps').read_text().splitlines()
                    rows = [row for row in rows if not row.startswith(target + '\t')]
                    if name == 'swapon':
                        size = self.path(target).stat().st_size // 1024 - 4
                        rows.append(f'{target}\tfile\t{size}\t0\t-2')
                    (outer.root / 'proc/swaps').write_text('\n'.join(rows) + '\n')
                    return subprocess.CompletedProcess(argv, 0, '', '')
                raise AssertionError(argv)

        self.host = FakeHost(self.root)
        self.manager = SwapManager(self.host)
        self.addCleanup(patch.stopall)
        patch('server_setting.root_actions.os.geteuid', return_value=0, create=True).start()

        @contextmanager
        def unlocked():
            yield
        patch.object(self.manager._root, '_lock', unlocked).start()

    def config(self, **values):
        result = {'schema': 1, 'path': '/swapfile', 'size_mib': 16, 'active': True, 'persistent': True}
        result.update(values)
        return result

    def existing(self, path='/swap.img', size=16, active=True):
        target = self.host.path(path)
        with target.open('wb') as stream:
            for _ in range(size):
                stream.write(bytes(1024 * 1024))
        target.chmod(0o600)
        self.host.run(['mkswap', '--', path])
        if active:
            self.host.run(['swapon', '--', path])
        return target

    def test_save_is_private_and_has_no_kernel_side_effect(self):
        self.assertIsNone(self.manager.load_saved())
        result = self.manager.save(self.config(size_mib=16384))
        self.assertEqual(result['status'], 'SAVED')
        self.assertEqual(self.manager.load_saved(), self.config(size_mib=16384))
        self.assertFalse(self.host.path('/swapfile').exists())
        self.assertEqual(self.host.path('/etc/fstab').read_bytes(), self.fstab)
        self.assertFalse(self.calls)

    def test_config_rejects_unknown_paths_bool_sizes_and_extra_fields(self):
        for values in ({'path': '/etc/passwd'}, {'size_mib': True}, {'size_mib': 15},
                       {'size_mib': 1048577}, {'active': 'yes'}, {'swappiness': 60}):
            with self.subTest(values=values), self.assertRaises((RootActionError, ValueError)):
                self.manager.plan(self.config(**values))
        self.assertFalse(self.host.path('/var').exists())

    def test_recommendation_does_not_change_current_and_insufficient_space_blocks(self):
        self.existing()
        observed = self.manager.status('/swap.img')
        self.assertEqual(observed['size_mib'], 16)
        self.assertEqual(observed['recommended_size_mib'], 16384)
        self.free_bytes = 9200 * 1024 ** 2
        with self.assertRaises(RootActionError) as raised:
            self.manager.plan(self.config(size_mib=16384))
        self.assertEqual(raised.exception.code, 'SWAP_DISK_SPACE')
        self.assertFalse(self.host.path('/var').exists())

    def test_create_preserves_other_swap_and_fstab_exactly_and_resize_cleans_old_data(self):
        other = self.existing()
        other_inode = other.stat().st_ino
        plan = self.manager.plan(self.config())
        self.assertFalse(self.host.path('/var').exists())
        result = self.manager.apply(plan)
        self.assertEqual(result['status'], 'SWAP_APPLIED')
        self.assertEqual(self.host.path('/etc/fstab').read_bytes(), self.fstab + b'/swapfile none swap sw,nofail 0 0\r\n')
        self.assertEqual(self.manager.status('/swapfile')['size_mib'], 16)
        self.assertTrue(self.manager.status('/swapfile')['active'])
        self.assertEqual(other.stat().st_ino, other_inode)
        resized = self.manager.apply(self.manager.plan(self.config(size_mib=32)))
        self.assertEqual(resized['status'], 'SWAP_APPLIED')
        self.assertEqual(self.host.path('/swapfile').stat().st_size, 32 * 1024 ** 2)
        self.assertFalse(list(self.host.path('/.3wa-swap').rglob('*.swap')))
        self.assertEqual(other.stat().st_ino, other_inode)
        self.assertFalse(any(call == ['swapoff', '--', '/swap.img'] for call in self.calls))
        self.assertEqual(self.manager.apply(self.manager.plan(self.config(size_mib=32)))['status'], 'NO_CHANGE')

    def test_activation_failure_restores_original_inode_active_state_and_fstab(self):
        original = self.existing()
        inode = original.stat().st_ino
        self.unwritten.add(inode)
        self.fail_on = 1
        result = self.manager.apply(self.manager.plan(self.config(path='/swap.img', size_mib=32)))
        self.assertEqual(result['status'], 'SWAP_ROLLED_BACK')
        self.assertEqual(original.stat().st_ino, inode)
        self.assertTrue(self.manager.status('/swap.img')['active'])
        self.assertEqual(self.host.path('/etc/fstab').read_bytes(), self.fstab)
        journal = self.host.path('/var/lib/3wa-server-setup/transactions/' + result['transaction_id'] + '/journal.json').read_text()
        self.assertNotIn('sensitive native diagnostic', journal)

    def test_low_memory_blocks_before_swapoff_and_preserves_original(self):
        original = self.existing()
        inode = original.stat().st_ino
        (self.root / 'proc/meminfo').write_text('MemAvailable: 128000 kB\n')
        with self.assertRaises(RootActionError) as raised:
            self.manager.plan(self.config(path='/swap.img', size_mib=32))
        self.assertEqual(raised.exception.code, 'SWAP_MEMORY')
        self.assertEqual(original.stat().st_ino, inode)
        self.assertFalse(any(call[0] == 'swapoff' for call in self.calls))

    def test_unknown_file_alias_custom_unit_and_rc_local_are_rejected(self):
        target = self.host.path('/swapfile')
        target.write_bytes(b'not swap, must never overwrite')
        with self.assertRaises(RootActionError):
            self.manager.plan(self.config())
        self.assertEqual(target.read_bytes(), b'not swap, must never overwrite')
        target.unlink()
        self.existing('/swapfile')
        fields = self.host.run(['blkid', '/swapfile']).stdout.split('UUID=')[1].strip()
        self.host.path('/etc/fstab').write_bytes(('UUID=' + fields + ' none swap sw 0 0\n').encode())
        with self.assertRaises(RootActionError):
            self.manager.plan(self.config())
        self.host.path('/etc/fstab').write_bytes(self.fstab)
        self.host.path('/etc/rc.local').write_text('#!/bin/sh\nswapon -a\n')
        with self.assertRaises(RootActionError):
            self.manager.plan(self.config())

    def test_stale_or_rehashed_tampered_plan_refuses_before_swap_mutation(self):
        plan = self.manager.plan(self.config())
        plan['config']['size_mib'] = 32
        with self.assertRaises(RootActionError):
            self.manager.apply(plan)
        plan = self.manager.plan(self.config())
        self.host.path('/etc/fstab').write_bytes(self.fstab + b'# edit\n')
        with self.assertRaises(RootActionError) as raised:
            self.manager.apply(plan)
        self.assertEqual(raised.exception.code, 'STALE_PLAN')
        self.assertFalse(self.host.path('/swapfile').exists())

    def test_pending_recovery_does_not_unlink_active_candidate_if_swapoff_fails(self):
        original = self.existing()
        original_inode = original.stat().st_ino
        checked = self.manager._verify
        def fail_verify(config):
            raise RootActionError('simulated verification failure')
        with patch.object(self.manager, '_verify', side_effect=fail_verify):
            self.fail_off = False
            actual_run = self.host.run
            def fail_rollback(argv, timeout=30):
                if argv[0] == 'swapon':
                    result = actual_run(argv, timeout)
                    self.fail_off = True
                    return result
                return actual_run(argv, timeout)
            with patch.object(self.host, 'run', side_effect=fail_rollback):
                result = self.manager.apply(self.manager.plan(self.config(path='/swap.img', size_mib=32)))
        self.assertEqual(result['status'], 'RECOVERY_REQUIRED')
        self.assertEqual(original.stat().st_size, 32 * 1024 ** 2)
        self.assertNotEqual(original.stat().st_ino, original_inode)
        self.assertTrue(self.manager.status('/swap.img')['active'])
        with self.assertRaises(RootActionError):
            self.manager.save(self.config())
        self.fail_off = False
        recovered = self.manager.recover(result['transaction_id'])
        self.assertEqual(recovered['status'], 'SWAP_ROLLED_BACK')
        self.assertEqual(original.stat().st_ino, original_inode)
        self.assertTrue(self.manager.status('/swap.img')['active'])

    def test_first_creation_recover_removes_only_selected_inactive_file(self):
        other = self.existing()
        result = self.manager.apply(self.manager.plan(self.config(active=False, persistent=False)))
        self.assertTrue(self.host.path('/swapfile').exists())
        self.assertFalse(self.manager.status('/swapfile')['active'])
        recovered = self.manager.recover(result['transaction_id'])
        self.assertEqual(recovered['status'], 'SWAP_ROLLED_BACK')
        self.assertFalse(self.host.path('/swapfile').exists())
        self.assertTrue(other.exists())
        self.assertEqual(self.host.path('/etc/fstab').read_bytes(), self.fstab)

    def test_keep_reported_size_preserves_fractional_mib_and_kernel_synthetic_unit(self):
        target = self.existing('/swapfile', active=False)
        with target.open('ab') as stream:
            stream.write(bytes(4096))
        self.host.run(['swapon', '--', '/swapfile'])
        inode = target.stat().st_ino
        actual = self.host.run
        def synthetic(argv, timeout=30):
            if argv[:2] == ['systemctl', 'show']:
                active = '/swapfile' in self.manager._swaps()
                return subprocess.CompletedProcess(argv, 0, 'LoadState=loaded\nFragmentPath=\nSourcePath=\nDropInPaths=\nActiveState=' + ('active' if active else 'inactive') + '\nWhat=/swapfile\nTransient=no\n', '')
            return actual(argv, timeout)
        with patch.object(self.host, 'run', side_effect=synthetic):
            plan = self.manager.plan(self.config(persistent=False))
            self.assertFalse(plan['replace_file'])
            self.assertEqual(plan['fstab_action'], 'unchanged')
            result = self.manager.apply(plan)
            self.assertEqual(result['status'], 'SWAP_APPLIED')
            off = self.manager.apply(self.manager.plan(self.config(active=False, persistent=False)))
            self.assertEqual(off['status'], 'SWAP_APPLIED')
        self.assertEqual(target.stat().st_ino, inode)
        self.assertEqual(target.stat().st_size, 16 * 1024 ** 2 + 4096)

    def test_zero_fill_deadline_cleans_partial_file_without_touching_other_swap(self):
        original = self.existing()
        inode = original.stat().st_ino
        with patch('server_setting.root_swap.time.monotonic', side_effect=[0, 901]):
            result = self.manager.apply(self.manager.plan(self.config()))
        self.assertEqual(result['status'], 'SWAP_ROLLED_BACK')
        self.assertFalse(self.host.path('/swapfile').exists())
        self.assertEqual(original.stat().st_ino, inode)
        self.assertFalse(list(self.host.path('/.3wa-swap').rglob('*.swap')))
        self.assertFalse(self.host.path('/opt/3waServerSetting/config/swap/owned.json').exists())

    def test_applied_recovery_external_fstab_edit_is_guarded_before_swapoff(self):
        result = self.manager.apply(self.manager.plan(self.config()))
        inode = self.host.path('/swapfile').stat().st_ino
        self.host.path('/etc/fstab').write_bytes(self.host.path('/etc/fstab').read_bytes() + b'# external\n')
        self.calls.clear()
        recovered = self.manager.recover(result['transaction_id'])
        self.assertEqual(recovered['status'], 'RECOVERY_REQUIRED')
        self.assertEqual(self.host.path('/swapfile').stat().st_ino, inode)
        self.assertTrue(self.manager.status('/swapfile')['active'])
        self.assertFalse(any(call[0] == 'swapoff' for call in self.calls))

    def test_recovery_only_exempts_its_own_pending_transaction(self):
        result = self.manager.apply(self.manager.plan(self.config()))
        identifier = result['transaction_id']
        other = {'transaction_id': self.manager._root._id(), 'kind': 'firewall',
                 'module': 'firewall', 'status': 'FW_AWAITING_CONFIRMATION'}
        self.manager._root._journal(other)
        self.calls.clear()
        with self.assertRaises(RootActionError) as raised:
            self.manager.recover(identifier)
        self.assertEqual(raised.exception.code, 'RECOVERY_REQUIRED')
        self.assertFalse(any(call[0] in {'swapon', 'swapoff', 'mkswap'} for call in self.calls))
        self.assertTrue(self.host.path('/swapfile').exists())
        other['status'] = 'FW_RESTORED_PREVIOUS'
        self.manager._root._journal(other)
        recovered = self.manager.recover(identifier)
        self.assertEqual(recovered['status'], 'SWAP_ROLLED_BACK')

    def test_blkid_export_device_name_must_match_the_requested_file(self):
        self.existing()
        actual = self.host.run
        def wrong_device(argv, timeout=30):
            result = actual(argv, timeout)
            if argv[0] == 'blkid':
                result.stdout = result.stdout.replace('DEVNAME=' + argv[-1], 'DEVNAME=/unexpected-file')
            return result
        with patch.object(self.host, 'run', side_effect=wrong_device):
            with self.assertRaises(RootActionError):
                self.manager.plan(self.config(path='/swap.img'))

    def test_kernel_accepted_ext4_unwritten_extents_remain_valid_after_disable_and_cleanup(self):
        self.existing()
        self.unwritten.add(self.host.path('/swap.img').stat().st_ino)
        self.assertTrue(self.manager.status('/swap.img')['supported'])
        result = self.manager.apply(self.manager.plan(self.config(path='/swap.img', active=False)))
        self.assertEqual(result['status'], 'SWAP_APPLIED')
        self.assertTrue(self.manager.status('/swap.img')['supported'])
        self.assertFalse(self.manager.status('/swap.img')['active'])
        resized = self.manager.apply(self.manager.plan(self.config(path='/swap.img', size_mib=32, active=False)))
        self.assertEqual(resized['status'], 'SWAP_APPLIED')
        self.assertFalse(list(self.host.path('/.3wa-swap').rglob('*.swap')))
        # An inactive file with no recorded kernel proof keeps strict extent
        # validation, even when its signature and plain fstab line look valid.
        self.existing('/swapfile', active=False)
        self.unwritten.add(self.host.path('/swapfile').stat().st_ino)
        with self.assertRaises(RootActionError):
            self.manager.plan(self.config())

    def test_save_and_apply_keep_shared_config_directory_public(self):
        directory = self.host.path('/opt/3waServerSetting/config')
        directory.mkdir(parents=True)
        directory.chmod(0o755)
        example = directory / 'firewall.example.json'
        example.write_bytes(b'{"example":"read-only TUI"}\n')
        self.manager.save(self.config())
        self.assertTrue((directory / 'swap/draft.json').is_file())
        if os.name == 'posix':
            self.assertEqual(directory.stat().st_mode & 0o777, 0o755)
            self.assertEqual((directory / 'swap').stat().st_mode & 0o777, 0o700)
            self.assertEqual((directory / 'swap/draft.json').stat().st_mode & 0o777, 0o600)
        result = self.manager.apply(self.manager.plan(self.config()))
        self.assertEqual(result['status'], 'SWAP_APPLIED')
        self.assertEqual(example.read_bytes(), b'{"example":"read-only TUI"}\n')
        if os.name == 'posix':
            self.assertEqual(directory.stat().st_mode & 0o777, 0o755)


if __name__ == '__main__':
    unittest.main()

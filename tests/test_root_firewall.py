"""Guarded firewall tests: real private files, fake kernel/systemd/SSH boundaries."""
import copy
from contextlib import contextmanager
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from server_setting.root_actions import Host, RootActionError
try:
    from server_setting.root_firewall import FirewallManager
except ImportError:
    FirewallManager = None


class FirewallTests(unittest.TestCase):
    def test_only_attested_banip_companion_hooks_can_coexist(self):
        self.graph = [{'table':{'family':'ip','name':'filter','handle':4}}] + [
            {'chain':{'family':'ip','table':'filter','name':name,'hook':hook,'type':'filter','policy':'accept'}}
            for name,hook in [('INPUT','input'),('FORWARD','forward'),('OUTPUT','output')]]
        with patch('server_setting.root_banip.verify_companion',return_value=True):
            self.assertEqual(self.manager.plan(self.config)['config'],self.config)
            self.graph.append({'chain':{'family':'inet','table':'unrelated','name':'input','hook':'input','policy':'drop'}})
            with self.assertRaises(RootActionError):
                self.manager.plan(self.config)
        self.graph.pop()
        with patch('server_setting.root_banip.verify_companion',return_value=False):
            with self.assertRaises(RootActionError):
                self.manager.plan(self.config)

    def setUp(self):
        self.assertIsNotNone(FirewallManager, 'Guarded firewall backend is missing')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'etc/ufw').mkdir(parents=True)
        (self.root / 'etc/os-release').write_text('ID=ubuntu\nVERSION_ID="26.04"\n')
        (self.root / 'etc/ufw/ufw.conf').write_text('ENABLED=no\n')
        for name in ('ipv4/ip_forward', 'ipv6/conf/all/forwarding'):
            path = self.root / 'proc/sys/net' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('0\n')
        self.events = []
        self.graph = []
        self.script = None
        self.boot_enabled = False
        self.fail_arm = False
        self.handle = 10
        self.legacy = False
        self.proof = {'caller_pid': os.getpid(), 'caller_start_ticks': 1, 'transport_pid': 111,
                      'transport_start_ticks': 1, 'socket_inode': '100', 'peer_ip': '192.168.137.1',
                      'peer_port': 60001, 'server_ip': '192.168.137.26', 'server_port': 22,
                      'boot_id': 'test-boot'}
        outer = self
        class FakeHost(Host):
            def run(self, argv, timeout=30):
                outer.events.append(list(argv))
                output, code = '', 0
                if argv[:2] == ['nft', '-j']:
                    output = json.dumps({'nftables': outer.graph})
                elif argv[:2] == ['nft', '-s']:
                    output = outer.script or ''
                elif argv[:2] == ['nft', '-f']:
                    source = Path(argv[2]).read_text()
                    if 'table inet threewa_services {' in source:
                        source = '\n'.join(line for line in source.splitlines() if line.strip() and not line.startswith('delete table')) + '\n'
                        outer.script = source
                        outer.handle += 1
                        foreign = [item for item in outer.graph if next(iter(item.values())).get('name') != 'threewa_services'
                                   and next(iter(item.values())).get('table') != 'threewa_services']
                        outer.graph = foreign + [{'table': {'family': 'inet', 'name': 'threewa_services',
                            'comment': re.search(r'comment "([^"]+)"', source)[1], 'handle': outer.handle}},
                            {'rule': {'family': 'inet', 'table': 'threewa_services', 'chain': 'input',
                                      'expr': [{'script_digest': hashlib.sha256(source.encode()).hexdigest()}]}}]
                    else:
                        outer.graph = [item for item in outer.graph if next(iter(item.values())).get('name') != 'threewa_services'
                                       and next(iter(item.values())).get('table') != 'threewa_services']
                        outer.script = None
                elif argv[:2] == ['systemctl', 'show']:
                    output = 'LoadState=not-found\nActiveState=inactive\nUnitFileState=disabled\n'
                elif argv[0] in {'iptables', 'ip6tables'}:
                    output = 'iptables v1.8.11 (' + ('legacy' if outer.legacy else 'nf_tables') + ')\n'
                elif argv[:2] == ['systemctl', 'is-enabled']:
                    output, code = ('enabled\n', 0) if outer.boot_enabled else ('disabled\n', 1)
                elif argv[:2] == ['systemctl', 'enable']:
                    outer.boot_enabled = True
                elif argv[:2] == ['systemctl', 'disable']:
                    outer.boot_enabled = False
                elif argv[:2] == ['systemctl', 'start']:
                    unit = argv[2]
                    identifier = unit.split('@', 1)[1].rsplit('.', 1)[0]
                    runtime = outer.manager.runtime
                    if unit.startswith('threewa-fw-guard@'):
                        if outer.fail_arm:
                            return subprocess.CompletedProcess(argv, 1, '', '')
                        runtime.arm(identifier)
                    elif unit.startswith('threewa-fw-apply@'):
                        runtime.apply(identifier)
                    elif unit.startswith('threewa-fw-confirm@'):
                        runtime.confirm(identifier)
                    elif unit.startswith('threewa-fw-rollback@'):
                        runtime.rollback(identifier, manual=True)
                return subprocess.CompletedProcess(argv, code, output, '')
        self.host = FakeHost(self.root)
        self.manager = FirewallManager(self.host)
        self.addCleanup(patch.stopall)
        patch('server_setting.root_actions.os.geteuid', return_value=0, create=True).start()
        @contextmanager
        def no_lock(*args, **kwargs):
            yield
        patch.object(self.manager.core, '_lock', no_lock).start()
        patch.object(self.manager.runtime, 'lock', no_lock).start()
        patch.object(self.manager.runtime, 'proof', side_effect=lambda pid=None: copy.deepcopy(outer.proof)).start()
        patch.object(self.manager.runtime, 'boot_id', return_value='test-boot').start()
        self.config = {'schema': 2, 'services': [{'id': 'http', 'enabled': True, 'protocol': 'tcp',
                        'ports': [80, 443], 'sources': ['192.0.2.0/24']}]}

    def fresh_connection(self, identifier):
        journal = self.manager.runtime.journal(identifier)
        self.proof.update(socket_inode=str(int(self.proof['socket_inode']) + 100), peer_port=self.proof['peer_port'] + 1,
                          transport_pid=self.proof['transport_pid'] + 1,
                          transport_start_ticks=journal['applied_ticks'] + 1)

    def test_startup_status_and_plan_do_not_create_state_or_change_rules(self):
        self.assertTrue(self.manager.status()['supported'])
        plan = self.manager.plan(self.config)
        self.assertEqual(plan['rescue'], {'port': 22, 'sources': ['192.168.137.1/32']})
        self.assertFalse((self.root / 'var').exists())
        self.assertFalse(any(call[:2] == ['nft', '-f'] for call in self.events))

    def test_saved_draft_is_private_and_does_not_activate_rules(self):
        self.assertEqual(self.manager.save(self.config)['status'], 'SAVED')
        self.assertEqual(self.manager.load_saved(), self.config)
        self.assertFalse(self.graph)

    def test_guard_must_arm_before_any_kernel_change(self):
        self.fail_arm = True
        with self.assertRaises(RootActionError):
            self.manager.apply(self.manager.plan(self.config))
        self.assertFalse(any(call[:2] == ['nft', '-f'] for call in self.events))
        self.assertFalse(self.graph)

    def test_apply_is_pending_and_existing_connection_cannot_confirm(self):
        result = self.manager.apply(self.manager.plan(self.config))
        self.assertEqual(result['status'], 'AWAITING_NEW_SSH')
        identifier = result['transaction_id']
        first_load = next(i for i, call in enumerate(self.events) if call[:2] == ['nft', '-f'])
        arm = next(i for i, call in enumerate(self.events) if call[:2] == ['systemctl', 'start'] and 'threewa-fw-guard@' in call[2])
        self.assertLess(arm, first_load)
        with self.assertRaises(RootActionError):
            self.manager.confirm(identifier)
        with self.assertRaises(RootActionError):
            self.manager.core._check_pending()
        self.assertFalse(self.manager.status()['persistent'])

    def test_fresh_ssh_confirms_and_reset_restores_initial_without_touching_foreign_table(self):
        foreign = {'table': {'family': 'ip', 'name': 'foreign_nat', 'handle': 2}}
        self.graph = [foreign]
        result = self.manager.apply(self.manager.plan(self.config))
        identifier = result['transaction_id']
        self.fresh_connection(identifier)
        self.assertEqual(self.manager.confirm(identifier)['status'], 'CONFIRMED')
        self.assertTrue(self.boot_enabled)
        self.assertTrue(self.manager.status()['persistent'])
        self.assertEqual(self.manager.rollback(identifier)['status'], 'RESTORED_INITIAL')
        self.assertEqual(self.graph, [foreign])
        self.assertFalse(self.boot_enabled)
        self.assertFalse(self.manager.status()['persistent'])

    def test_timeout_rolls_back_without_global_lock_or_live_ui(self):
        result = self.manager.apply(self.manager.plan(self.config))
        identifier = result['transaction_id']
        with patch.object(self.manager.core, '_lock', side_effect=AssertionError('watchdog must not use global lock')):
            self.manager.runtime.expire(identifier)
        self.assertFalse(self.graph)
        self.assertEqual(self.manager.runtime.journal(identifier)['status'], 'FW_ROLLED_BACK')

    def test_reboot_never_loads_unconfirmed_candidate(self):
        result = self.manager.apply(self.manager.plan(self.config))
        self.graph = []
        self.script = None
        self.manager.runtime.boot()
        self.assertFalse(self.graph)
        self.assertEqual(self.manager.runtime.journal(result['transaction_id'])['status'], 'FW_BOOT_ROLLED_BACK')

    def test_modified_preview_and_foreign_edit_are_rejected(self):
        plan = self.manager.plan(self.config)
        changed = copy.deepcopy(plan)
        changed['script'] += '\nflush ruleset\n'
        with self.assertRaises(RootActionError):
            self.manager.apply(changed)
        self.graph.append({'table': {'family': 'ip', 'name': 'changed_elsewhere', 'handle': 3}})
        with self.assertRaises(RootActionError):
            self.manager.apply(plan)
        self.assertFalse(any(call[:2] == ['nft', '-f'] for call in self.events))

    def test_name_collision_is_not_adopted(self):
        self.graph = [{'table': {'family': 'inet', 'name': 'threewa_services', 'comment': 'foreign', 'handle': 7}}]
        with self.assertRaises(RootActionError):
            self.manager.plan(self.config)

    def test_expired_confirmation_cannot_commit(self):
        result = self.manager.apply(self.manager.plan(self.config))
        identifier = result['transaction_id']
        self.fresh_connection(identifier)
        journal = self.manager.runtime.journal(identifier)
        journal['deadline_boottime'] = -1
        self.manager.runtime.write_journal(journal)
        with self.assertRaises(RootActionError):
            self.manager.confirm(identifier)
        self.manager.runtime.expire(identifier)
        self.assertFalse(self.graph)

    def test_confirmed_policy_survives_boot_reload_and_can_restore_initial(self):
        result = self.manager.apply(self.manager.plan(self.config))
        identifier = result['transaction_id']
        self.fresh_connection(identifier)
        self.manager.confirm(identifier)
        old_handle = self.manager.runtime.snapshot()['handle']
        self.graph, self.script = [], None
        self.manager.runtime.boot()
        self.assertNotEqual(self.manager.runtime.snapshot()['handle'], old_handle)
        self.assertTrue(self.manager.status()['supported'])
        self.manager.plan(self.config)
        self.assertEqual(self.manager.rollback(identifier)['status'], 'RESTORED_INITIAL')
        self.assertFalse(self.graph)

    def test_foreign_hook_forwarding_and_legacy_backend_are_rejected(self):
        self.graph = [{'chain': {'family': 'ip', 'table': 'foreign', 'name': 'input', 'hook': 'input'}}]
        with self.assertRaises(RootActionError):
            self.manager.plan(self.config)
        self.graph = []
        path = self.root / 'proc/sys/net/ipv4/ip_forward'
        path.write_text('1\n')
        with self.assertRaises(RootActionError):
            self.manager.plan(self.config)
        path.write_text('0\n')
        self.legacy = True
        with self.assertRaises(RootActionError):
            self.manager.plan(self.config)

    def test_confirmation_command_uses_installed_launcher_and_explicit_confirm(self):
        result = self.manager.apply(self.manager.plan(self.config))
        self.assertEqual(result['confirmation_command'], '/opt/3waServerSetting/run.sh --firewall-confirm ' + result['transaction_id'] + ' --confirm')

    def test_two_confirmed_policies_can_restore_previous_then_initial(self):
        first = self.manager.apply(self.manager.plan(self.config))['transaction_id']
        self.fresh_connection(first)
        self.manager.confirm(first)
        first_policy = self.manager.runtime.snapshot()['sha256']
        changed = copy.deepcopy(self.config)
        changed['services'][0]['sources'] = ['public']
        second = self.manager.apply(self.manager.plan(changed))['transaction_id']
        self.fresh_connection(second)
        self.manager.confirm(second)
        self.assertEqual(self.manager.rollback(second)['status'], 'RESTORED_PREVIOUS')
        self.assertEqual(self.manager.runtime.snapshot()['sha256'], first_policy)
        self.assertEqual(self.manager.rollback(first)['status'], 'RESTORED_INITIAL')
        self.assertFalse(self.graph)
        self.assertFalse(self.boot_enabled)

    def test_kernel_commit_before_after_journal_is_still_recoverable(self):
        original = self.manager.runtime.write_journal
        def crash_after_kernel(journal):
            if journal['status'] == 'FW_AWAITING_NEW_SSH':
                raise KeyboardInterrupt('simulate applier death after kernel commit')
            original(journal)
        with patch.object(self.manager.runtime, 'write_journal', side_effect=crash_after_kernel):
            with self.assertRaises(KeyboardInterrupt):
                self.manager.apply(self.manager.plan(self.config))
        identifier = self.manager.core._journals()[-1]['transaction_id']
        self.assertTrue(self.graph)
        self.assertEqual(self.manager.runtime.journal(identifier)['status'], 'FW_APPLYING')
        self.manager.runtime.expire(identifier)
        self.assertFalse(self.graph)
        self.assertEqual(self.manager.runtime.journal(identifier)['status'], 'FW_ROLLED_BACK')


if __name__ == '__main__':
    unittest.main()

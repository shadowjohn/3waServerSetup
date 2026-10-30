"""Root-controlled networkd fallback recognition, never backend mutation."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from server_setting.network_watchdog import NetworkError, Runtime
import test_root_network as network_fixture


FALLBACK = b'[Match]\nKind=!*\nType=!loopback\n\n[Network]\nDHCP=yes\n\n[DHCPv4]\nClientIdentifier=mac\nRequestOptions=17\n\n[DHCPv6]\nRequestOptions=59 60\n'
RUNTIME = '/run/systemd/network/zzzz-dracut-default.network'
TEMPLATE = '/usr/lib/dracut/modules.d/11systemd-networkd/dracut-default.network'
PRODUCER = '/usr/lib/dracut/modules.d/11systemd-networkd/networkd-config.sh'
GENERATED = '/run/systemd/network/10-netplan-eth0.network'


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.events = []
        self.selected = GENERATED
        def run(argv, timeout=30):
            self.events.append(argv)
            return subprocess.CompletedProcess(argv, 0, '  Network File: '+self.selected+'\n', '')
        self.runtime = Runtime(self.root, run)

    def write(self, absolute, raw):
        target = self.root/absolute.lstrip('/')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        target.chmod(0o644)
        return target

    def standard(self):
        self.write(GENERATED, b'[Match]\nName=eth0\n\n[Network]\nDHCP=yes\n')
        self.write(RUNTIME, FALLBACK)
        self.write(TEMPLATE, FALLBACK)
        self.write(PRODUCER, b'#!/bin/sh\n# root-controlled installed dracut producer\n')

    def test_standard_inactive_dracut_is_pinned_without_any_write(self):
        self.standard()
        before = {str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result = self.runtime.backend_dependencies('eth0')
        self.assertEqual(set(result), {RUNTIME, TEMPLATE, PRODUCER})
        self.assertEqual(result[RUNTIME]['sha256'], '119cdd2a374b184b9191aee30dce9e0a27c30dcb6847cc3ba502593f2ef97d06')
        self.assertEqual(before, {str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        self.assertTrue(self.events)
        self.assertTrue(all(argv[0]=='networkctl' for argv in self.events))

    def test_no_fallback_preserves_old_hosts_without_networkctl_requirement(self):
        self.assertEqual(self.runtime.backend_dependencies('eth0'), {})
        self.assertEqual(self.events, [])

    def test_filename_template_drift_and_effective_fallback_are_rejected(self):
        self.standard()
        for absolute in (RUNTIME, TEMPLATE):
            with self.subTest(path=absolute):
                self.write(absolute, FALLBACK+b'\n[Network]\nDNS=192.0.2.1\n')
                with self.assertRaises(NetworkError):self.runtime.backend_dependencies('eth0')
                self.write(absolute, FALLBACK)
        self.selected = RUNTIME
        with self.assertRaises(NetworkError):self.runtime.backend_dependencies('eth0')

    def test_unknown_backend_and_dropins_are_never_whitelisted(self):
        self.standard()
        for absolute in ('/etc/systemd/network/20-custom.network', '/run/systemd/network/80-custom.netdev',
                         '/etc/systemd/network/10-netplan-eth0.network.d/50-extra.conf',
                         '/usr/lib/systemd/network/zzzz-dracut-default.network.d/50-extra.conf',
                         '/run/systemd/network/10-netplan-eth0.network.d/10-extra.conf'):
            with self.subTest(path=absolute):
                target = self.write(absolute, b'[Network]\nDHCP=yes\n')
                with self.assertRaises(NetworkError):self.runtime.backend_dependencies('eth0')
                target.unlink()
                if target.parent.name.endswith('.d'):target.parent.rmdir()

    def test_vendor_same_basename_override_is_rejected(self):
        self.standard()
        self.write('/usr/lib/systemd/network/10-netplan-eth0.network', b'[Network]\nDHCP=yes\n')
        with self.assertRaises(NetworkError):self.runtime.backend_dependencies('eth0')

    def test_new_dependency_field_detects_new_custom_and_fallback_drift(self):
        self.standard()
        pins = self.runtime.backend_dependencies('eth0')
        self.write('/etc/netplan/base.yaml', b'network:\n  version: 2\n').chmod(0o600)
        journal = {'source':{'path':'/etc/netplan/base.yaml','snapshot':self.runtime.snapshot('/etc/netplan/base.yaml')},
                   'file_hashes':{'candidate.yaml':'unused'},'candidate_size':0,
                   'inventory':self.runtime.inventory(),'overlay':'/run/netplan/base.yaml',
                   'config':{'interface':'eth0'},'backend_dependencies':pins}
        self.runtime.check_source(journal)
        injected = self.write('/run/systemd/network/01-injected.network', b'[Match]\nName=eth0\n')
        with self.assertRaises(NetworkError):self.runtime.check_source(journal)
        injected.unlink()
        self.write(RUNTIME, FALLBACK+b'\n[Network]\nDNS=192.0.2.1\n')
        with self.assertRaises(NetworkError):self.runtime.check_source(journal)
        # Old frozen workers/journals keep their original recovery contract.
        del journal['backend_dependencies']
        with patch.object(self.runtime, 'backend_dependencies', side_effect=AssertionError('legacy must remain readable')):
            self.runtime.check_source(journal)

    def test_vendor_inputs_are_pinned_but_generated_changes_are_not(self):
        self.standard()
        vendor = '/usr/lib/systemd/network/80-container-host0.network'
        self.write(vendor, b'[Match]\nName=host0\n\n[Network]\nDHCP=yes\n')
        before = self.runtime.backend_dependencies('eth0')
        self.assertIn(vendor, before)
        self.write(GENERATED, b'[Match]\nName=eth0\n\n[Network]\nDHCP=no\n')
        self.assertEqual(before, self.runtime.backend_dependencies('eth0'))
        self.write('/usr/local/lib/systemd/network/20-new.network', b'[Match]\nName=eth1\n')
        self.assertNotEqual(before, self.runtime.backend_dependencies('eth0'))

    def test_absent_present_and_custom_backend_races_change_the_pin(self):
        initial = self.runtime.backend_dependencies('eth0')
        self.standard()
        self.assertNotEqual(initial, self.runtime.backend_dependencies('eth0'))
        before = self.runtime.backend_dependencies('eth0')
        (self.root/RUNTIME.lstrip('/')).unlink()
        self.assertNotEqual(before, self.runtime.backend_dependencies('eth0'))
        self.write('/run/systemd/network/01-injected.network', b'[Match]\nName=eth0\n')
        with self.assertRaises(NetworkError):self.runtime.backend_dependencies('eth0')

    def test_cross_boot_backend_inspection_can_precede_networkd_readiness(self):
        self.standard()
        self.selected = RUNTIME
        self.assertIn(RUNTIME, self.runtime.backend_dependencies('eth0', require_effective=False))
        self.assertEqual(self.events, [])

    @unittest.skipUnless(__import__('os').name == 'posix', 'native owner/mode policy')
    def test_writable_runtime_or_vendor_source_is_rejected(self):
        self.standard()
        for absolute in (RUNTIME, TEMPLATE, PRODUCER):
            with self.subTest(path=absolute):
                target = self.root/absolute.lstrip('/')
                target.chmod(0o666)
                with self.assertRaises(NetworkError):self.runtime.backend_dependencies('eth0')
                target.chmod(0o644)

    @unittest.skipUnless(hasattr(__import__('os'), 'symlink'), 'symlinks unavailable')
    def test_symlink_fallback_is_rejected(self):
        self.standard()
        target = self.root/RUNTIME.lstrip('/')
        target.unlink()
        try:target.symlink_to(self.root/TEMPLATE.lstrip('/'))
        except OSError:self.skipTest('symlink privilege unavailable')
        with self.assertRaises(NetworkError):self.runtime.backend_dependencies('eth0')


class BackendRecoveryTests(unittest.TestCase):
    setUp = network_fixture.NetworkTests.setUp
    fresh = network_fixture.NetworkTests.fresh

    def known_backend(self):
        for absolute,raw in ((GENERATED,b'[Match]\nName=eth0\n\n[Network]\nDHCP=yes\n'),
                             (RUNTIME,FALLBACK),(TEMPLATE,FALLBACK),(PRODUCER,b'#!/bin/sh\n')):
            path=self.root/absolute.lstrip('/')
            path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw);path.chmod(0o644)
        real=self.manager.runtime.run
        self.networkctl_unavailable=False
        self.networkctl_calls=0
        def run(argv,timeout=30):
            if argv[0]=='networkctl':
                self.networkctl_calls+=1
                return subprocess.CompletedProcess(argv,1 if self.networkctl_unavailable else 0,
                    '' if self.networkctl_unavailable else 'Network File: '+GENERATED+'\n','')
            return real(argv,timeout=timeout)
        self.manager.runtime.run=run

    def test_same_boot_recovery_removes_trial_when_networkd_inspection_is_down(self):
        self.known_backend();before=self.source.read_bytes()
        identifier=self.manager.apply(self.manager.plan(self.config))['transaction_id']
        self.networkctl_unavailable=True;calls=self.networkctl_calls
        self.assertEqual(self.manager.recover(identifier)['status'],'ROLLED_BACK')
        self.assertEqual(self.networkctl_calls,calls,'Recovery must not depend on a healthy active NetworkFile query')
        self.assertFalse((self.root/'run/netplan/50-cloud-init.yaml').exists())
        self.assertEqual(self.source.read_bytes(),before)
        self.assertEqual(self.manager.runtime.journal(identifier)['status'],'ROLLED_BACK')
        self.assertEqual(self.address,'192.168.137.192/24')

    def test_new_ssh_confirmation_keeps_effective_backend_proof_strict(self):
        self.known_backend();before=self.source.read_bytes()
        identifier=self.manager.apply(self.manager.plan(self.config))['transaction_id']
        self.fresh(identifier);self.networkctl_unavailable=True
        with self.assertRaises(Exception):self.manager.confirm(identifier)
        self.assertEqual(self.source.read_bytes(),before)
        self.assertTrue((self.root/'run/netplan/50-cloud-init.yaml').exists())
        self.assertNotEqual(self.manager.runtime.journal(identifier)['status'],'APPLIED')

    def test_failed_native_apply_can_restore_baseline_before_networkd_recovers(self):
        self.known_backend();before=self.source.read_bytes();real=self.manager.runtime.run
        def fail_trial(argv,timeout=30):
            if argv[:2]==['netplan','apply'] and (self.root/'run/netplan/50-cloud-init.yaml').exists():
                self.networkctl_unavailable=True
                return subprocess.CompletedProcess(argv,1,'','networkd is restarting')
            return real(argv,timeout=timeout)
        self.manager.runtime.run=fail_trial
        with self.assertRaises(Exception):self.manager.apply(self.manager.plan(self.config))
        self.assertEqual(self.source.read_bytes(),before)
        self.assertFalse((self.root/'run/netplan/50-cloud-init.yaml').exists())
        self.assertEqual(self.manager.core._journals()[-1]['status'],'ROLLED_BACK')
        self.assertEqual(self.address,'192.168.137.192/24')

    def test_recovery_still_refuses_new_external_backend_even_when_networkd_is_down(self):
        self.known_backend();before=self.source.read_bytes()
        identifier=self.manager.apply(self.manager.plan(self.config))['transaction_id']
        self.networkctl_unavailable=True
        injected=self.root/'run/systemd/network/01-external.network'
        injected.write_bytes(b'[Match]\nName=eth0\n');injected.chmod(0o644)
        with self.assertRaises(Exception):self.manager.recover(identifier)
        self.assertEqual(self.source.read_bytes(),before)
        self.assertTrue((self.root/'run/netplan/50-cloud-init.yaml').exists())
        self.assertNotEqual(self.manager.runtime.journal(identifier)['status'],'ROLLED_BACK')


if __name__ == '__main__':unittest.main()

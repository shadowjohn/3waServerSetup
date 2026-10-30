"""IPv6 policy changes use the existing guarded Network transaction."""
import copy
import json
import unittest
from unittest.mock import patch

import test_root_network as fixture
from server_setting.root_actions import RootActionError
from server_setting.root_network import DRAFT, normalize


class NetworkIPv6Tests(unittest.TestCase):
    setUp = fixture.NetworkTests.setUp
    fresh = fixture.NetworkTests.fresh

    def node(self, **fields):
        document = copy.deepcopy(self.original)
        document['network']['ethernets']['eth0'].update(fields)
        self.source.write_text(json.dumps(document))
        return document

    def test_old_exact_five_fields_normalize_to_preserve_and_unknown_fields_fail(self):
        self.assertEqual(normalize(self.config), dict(self.config, ipv6='preserve'))
        for value in (True, False, None, '', 'enabled', ['auto']):
            with self.subTest(value=value), self.assertRaises(RootActionError):
                normalize(dict(self.config, ipv6=value))
        with self.assertRaises(RootActionError):
            normalize(dict(self.config, ipv6='preserve', unknown=True))

    def test_old_saved_draft_loads_without_rewrite_and_new_mode_roundtrips(self):
        original = (json.dumps(self.config) + '\n').encode()
        self.manager.runtime.atomic(DRAFT, original)
        self.assertEqual(self.manager.load_saved(), dict(self.config, ipv6='preserve'))
        self.assertEqual(self.manager.runtime.read(DRAFT), original)
        target = dict(self.config, ipv6='disabled')
        self.manager.save(target)
        self.assertEqual(self.manager.load_saved(), target)
        self.assertEqual(json.loads(self.source.read_bytes()), self.original)

    def test_auto_enables_dhcp_ra_and_ipv6_linklocal_preserving_ipv4_and_static_ipv6(self):
        before = self.node(dhcp6=False, **{'accept-ra':False, 'link-local':['ipv4'],
            'addresses':['2001:db8::10/64'], 'routes':[{'to':'default','via':'2001:db8::1'}]})
        result = self.manager.plan(dict(self.config, ipv6='auto'))
        node = result['candidate']['network']['ethernets']['eth0']
        self.assertTrue(node['dhcp6'])
        self.assertTrue(node['accept-ra'])
        self.assertEqual(node['link-local'], ['ipv4', 'ipv6'])
        self.assertIn('2001:db8::10/64', node['addresses'])
        self.assertIn({'to':'default','via':'2001:db8::1'}, node['routes'])
        self.assertEqual(json.loads(self.source.read_bytes()), before)

    def test_disabled_stops_dhcp_ra_and_only_ipv6_linklocal(self):
        self.node(dhcp6=True, **{'accept-ra':True, 'link-local':['ipv4','ipv6']})
        node = self.manager.plan(dict(self.config, ipv6='disabled'))['candidate']['network']['ethernets']['eth0']
        self.assertFalse(node['dhcp6'])
        self.assertFalse(node['accept-ra'])
        self.assertEqual(node['link-local'], ['ipv4'])

    def test_auto_already_matching_linklocal_order_does_not_start_a_new_trial(self):
        self.node(dhcp6=True, **{'accept-ra':True, 'link-local':['ipv6','ipv4']})
        config = dict(self.config, ipv6='auto', mode='dhcp', address='', gateway='', dns=[])
        plan = self.manager.plan(config)
        self.assertTrue(plan['no_change'])
        self.assertEqual(self.manager.apply(plan)['status'], 'NO_CHANGE')
        self.assertFalse(any(event[0] in {'netplan','systemd-run'} for event in self.events))

    def test_disabled_rejects_static_ipv6_gateway_routes_and_ipv6_dns_before_live_change(self):
        for extra in ({'addresses':['2001:db8::10/64']}, {'gateway6':'2001:db8::1'},
                      {'routes':[{'to':'default','via':'2001:db8::1'}]}):
            self.node(**extra)
            with self.subTest(extra=extra), self.assertRaises(RootActionError) as error:
                self.manager.plan(dict(self.config, ipv6='disabled'))
            self.assertEqual(error.exception.code, 'NETWORK_IPV6_POLICY')
        with self.assertRaises(RootActionError):
            normalize(dict(self.config, ipv6='disabled', dns=['2001:4860:4860::8888']))
        self.assertFalse(any(event[0] == 'netplan' for event in self.events))

    def test_status_infers_only_unambiguous_explicit_auto_or_disabled(self):
        for extra, addresses, wanted in (
                ({'dhcp6':False, 'accept-ra':False, 'link-local':[]}, [], 'disabled'),
                ({'dhcp6':True, 'accept-ra':True, 'link-local':['ipv6']}, ['fe80::2/64'], 'auto'),
                ({'dhcp6':False}, ['fe80::2/64'], 'preserve'),
                ({'dhcp6':True, 'accept-ra':True}, ['fe80::2/64'], 'preserve'),
                ({'dhcp6':True, 'accept-ra':False, 'link-local':['ipv6']}, ['fe80::2/64'], 'preserve')):
            self.node(**extra)
            self.ipv6_addresses = addresses
            state = self.manager.status()
            with self.subTest(extra=extra):
                self.assertTrue(state['supported'], state['detail'])
                self.assertEqual(state['config']['ipv6'], wanted)

    def test_auto_manual_dns_only_synchronizes_use_dns_and_keeps_other_override_guard(self):
        self.node(**{'dhcp4-overrides':{'route-metric':50}, 'dhcp6-overrides':{'route-metric':50}})
        config = dict(self.config, ipv6='auto', mode='dhcp', address='', gateway='', dns=['9.9.9.9'])
        node = self.manager.plan(config)['candidate']['network']['ethernets']['eth0']
        self.assertEqual(node['dhcp4-overrides'], {'route-metric':50, 'use-dns':False})
        self.assertEqual(node['dhcp6-overrides'], {'route-metric':50, 'use-dns':False})
        self.node(**{'dhcp4-overrides':{'route-metric':50}, 'dhcp6-overrides':{'route-metric':75}})
        with self.assertRaises(RootActionError) as error:
            self.manager.plan(config)
        self.assertEqual(error.exception.code, 'NETWORK_IPV6_POLICY')

    def test_disabled_trial_recovers_original_auto_ipv6_and_requires_new_ipv4_ssh(self):
        before = self.node(dhcp6=True, **{'accept-ra':True, 'link-local':['ipv6']})
        identifier = self.manager.apply(self.manager.plan(dict(self.config, ipv6='disabled')))['transaction_id']
        self.assertEqual(self.ipv6_addresses, [])
        self.assertEqual(json.loads(self.source.read_bytes()), before)
        with self.assertRaises(RootActionError):
            self.manager.confirm(identifier)
        self.manager.runtime.expire(identifier)
        self.assertTrue(any(value.startswith('fe80:') for value in self.ipv6_addresses))
        self.assertEqual(json.loads(self.source.read_bytes()), before)
        self.assertEqual(self.manager.runtime.journal(identifier)['status'], 'ROLLED_BACK')

    def test_auto_trial_confirm_persists_and_an_ipv6_origin_cannot_start_trial(self):
        self.node(**{'accept-ra':False, 'link-local':[]})
        self.ipv6_addresses = []
        (self.root / 'proc/sys/net/ipv6/conf/eth0/disable_ipv6').write_text('1\n')
        identifier = self.manager.apply(self.manager.plan(dict(self.config, ipv6='auto')))['transaction_id']
        self.assertTrue(any(value.startswith('fe80:') for value in self.ipv6_addresses))
        self.fresh(identifier)
        self.assertEqual(self.manager.confirm(identifier)['status'], 'CONFIRMED')
        node = json.loads(self.source.read_bytes())['network']['ethernets']['eth0']
        self.assertTrue(node['dhcp6'])
        self.assertTrue(node['accept-ra'])
        self.proof['server_ip'] = '2001:db8::10'
        with self.assertRaises(RootActionError) as error:
            self.manager.plan(dict(self.config, ipv6='disabled'))
        self.assertEqual(error.exception.code, 'NETWORK_SSH_REQUIRED')

    def test_backend_dependencies_are_fingerprinted_and_frozen_into_worker_journal(self):
        path = '/etc/systemd/networkd.conf'
        self.manager.runtime.atomic(path, b'# trusted backend dependency\n')
        dependencies = {path:self.manager.runtime.snapshot(path)}
        with patch.object(self.manager.runtime, 'backend_dependencies', return_value=dependencies, create=True):
            plan = self.manager.plan(dict(self.config, ipv6='auto'))
            self.assertEqual(plan['before']['backend_dependencies'], dependencies)
            self.assertEqual(plan['before']['dependencies'][path], dependencies[path])
            identifier = self.manager.apply(plan)['transaction_id']
            self.assertEqual(self.manager.runtime.journal(identifier)['backend_dependencies'], dependencies)

    def test_disabled_candidate_with_remaining_ipv6_rolls_back_the_whole_trial(self):
        before = self.node(dhcp6=True, **{'accept-ra':True, 'link-local':['ipv6']})
        real = self.manager.runtime.run
        def residual(argv, timeout=30):
            result = real(argv, timeout=timeout)
            if argv[:2] == ['netplan','apply'] and (self.root / 'run/netplan/50-cloud-init.yaml').exists():
                self.ipv6_addresses = ['fe80::2/64']
            return result
        with patch.object(self.manager.runtime, 'run', side_effect=residual):
            with self.assertRaises(RootActionError):
                self.manager.apply(self.manager.plan(dict(self.config, ipv6='disabled')))
        self.assertEqual(json.loads(self.source.read_bytes()), before)
        self.assertFalse((self.root / 'run/netplan/50-cloud-init.yaml').exists())
        self.assertEqual(self.manager.core._journals()[-1]['status'], 'ROLLED_BACK')
        self.assertTrue(any(value.startswith('fe80:') for value in self.ipv6_addresses))


if __name__ == '__main__':
    unittest.main()

"""IPv6 policy verifies the selected interface without requiring Internet IPv6."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from server_setting.network_watchdog import NetworkError, Runtime


class IPv6RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {'interface':'eth0','mode':'static','address':'192.0.2.2/24','gateway':'','dns':[]}
        self.value = {'addresses':['192.0.2.2/24'],'dynamic':[], 'routes':[], 'dns':[], 'mac':'00:15:5d:01:6a:05',
                      'ipv6_addresses':['fe80::215:5dff:fe01:6a05/64'], 'ipv6_routes':[{'dst':'fe80::/64','dev':'eth0'}],
                      'ipv6_disabled':False}
        self.runtime = Runtime(self.root)
        self.journal = {'mac':self.value['mac'],'config':self.config}

    def check(self, mode, value=None):
        return self.runtime.check_runtime(dict(self.journal,config=dict(self.config,ipv6=mode)),observed=value or self.value)

    def test_preserve_accepts_legacy_observations_and_keeps_ipv4_guards(self):
        value = {key:val for key,val in self.value.items() if not key.startswith('ipv6_')}
        self.check('preserve', value)
        changed = copy.deepcopy(value);changed['dynamic']=['192.0.2.2']
        with self.assertRaises(NetworkError):self.check('preserve', changed)

    def test_disabled_requires_no_ipv6_address_or_route(self):
        empty = dict(self.value,ipv6_addresses=[],ipv6_routes=[])
        self.check('disabled', empty)
        with self.assertRaises(NetworkError):self.check('disabled')
        with self.assertRaises(NetworkError):self.check('disabled', dict(empty,ipv6_routes=[{'dst':'default','dev':'eth0'}]))

    def test_auto_accepts_link_local_without_global_connectivity(self):
        self.check('auto')
        for value in (dict(self.value,ipv6_disabled=True), dict(self.value,ipv6_disabled=None),
                      dict(self.value,ipv6_addresses=[]), dict(self.value,ipv6_addresses=['2001:db8::1/64']),
                      dict(self.value,ipv6_link_local_ready=False)):
            with self.subTest(value=value),self.assertRaises(NetworkError):self.check('auto', value)

    def test_observe_ipv6_selected_nic_and_kernel_gate_without_writes(self):
        disabled = self.root/'proc/sys/net/ipv6/conf/eth0/disable_ipv6'
        disabled.parent.mkdir(parents=True);disabled.write_text('0\n')
        events = []
        def run(argv, timeout=30):
            events.append(argv)
            if argv[:3]==['ip','-j','link']:
                value=[{'ifname':'eth0','link_type':'ether','address':self.value['mac']}]
            elif argv[:3]==['ip','-j','-6']:
                value=([{'ifname':'eth0','addr_info':[{'family':'inet6','local':'fe80::215:5dff:fe01:6a05','prefixlen':64,'scope':'link'}]}]
                       if 'address' in argv else [{'dst':'fe80::/64','dev':'eth0','protocol':'kernel'}, {'dst':'default','dev':'eth1'}])
            elif argv[:3]==['ip','-j','-4']:
                value=([{'addr_info':[{'family':'inet','local':'192.0.2.2','prefixlen':24,'scope':'global'}]}] if 'address' in argv else [])
            elif argv[0]=='resolvectl':return subprocess.CompletedProcess(argv,0,'Link 2 (eth0):\n','')
            else:raise AssertionError(argv)
            return subprocess.CompletedProcess(argv,0,json.dumps(value),'')
        runtime = Runtime(self.root,run)
        result = runtime.observed('eth0')
        self.assertEqual(result['ipv6_addresses'], self.value['ipv6_addresses'])
        self.assertEqual(result['ipv6_routes'], [{'dst':'fe80::/64','dev':'eth0','protocol':'kernel'}])
        self.assertIs(result['ipv6_disabled'], False)
        disabled.unlink()
        self.assertIsNone(runtime.observed('eth0')['ipv6_disabled'])
        self.assertTrue(all(argv[0] in {'ip','resolvectl'} for argv in events))

    def test_kernel_without_ipv6_preserves_legacy_ipv4_but_cannot_claim_auto_or_disabled(self):
        def run(argv, timeout=30):
            if argv[:3]==['ip','-j','link']:
                value=[{'ifname':'eth0','link_type':'ether','address':self.value['mac']}]
            elif argv[:3]==['ip','-j','-6']:
                return subprocess.CompletedProcess(argv, 1, '', 'Address family not supported')
            elif argv[:3]==['ip','-j','-4']:
                value=[{'addr_info':[{'family':'inet','local':'192.0.2.2','prefixlen':24,'scope':'global'}]}] if 'address' in argv else []
            elif argv[0]=='resolvectl':return subprocess.CompletedProcess(argv,0,'Link 2 (eth0):\n','')
            else:raise AssertionError(argv)
            return subprocess.CompletedProcess(argv,0,json.dumps(value),'')
        value=Runtime(self.root, run).observed('eth0')
        self.check('preserve',value)
        self.assertIs(value['ipv6_disabled'],None)
        for policy in ('auto','disabled'):
            with self.subTest(policy=policy),self.assertRaises(NetworkError):self.check(policy,value)


if __name__ == '__main__':unittest.main()

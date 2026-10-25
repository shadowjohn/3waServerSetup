"""Real private files; fake only netplan/systemd/kernel/SSH boundaries."""
import copy
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from server_setting.root_actions import Host, RootActionError
try:
    from server_setting.root_network import NetworkManager, normalize
except ImportError:
    NetworkManager = normalize = None


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(NetworkManager, 'Guarded Network backend is missing')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'etc/netplan/50-cloud-init.yaml'
        self.source.parent.mkdir(parents=True)
        self.original = {'network': {'version': 2, 'ethernets': {'eth0': {'dhcp4': True, 'dhcp6': False}}}}
        self.source.write_bytes((json.dumps(self.original) + '\n').encode())
        self.source.chmod(0o600)
        for name, content in {'etc/os-release':'ID=ubuntu\nVERSION_ID="24.04"\n',
                'etc/cloud/cloud.cfg.d/90-installer-network.cfg':'{"network":{"config":"disabled"}}',
                'proc/sys/kernel/random/boot_id':'test-boot', 'proc/cmdline':'quiet',
                'sys/class/net/eth0/address':'00:15:5d:01:6a:02'}.items():
            path = self.root/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.events = []
        self.units=set()
        self.address = '192.168.137.192/24'
        self.dns = ['192.168.137.1']
        self.gateway = '192.168.137.1'
        self.fail_generate = self.fail_apply = self.fail_arm = False
        self.boot = 'test-boot'
        self.proof = {'caller_pid':os.getpid(),'caller_start_ticks':1,'transport_pid':111,
            'transport_start_ticks':1,'socket_inode':'100','peer_ip':'192.168.137.1',
            'peer_port':60001,'server_ip':'192.168.137.192','server_port':22,'boot_id':self.boot}
        outer = self
        class FakeHost(Host):
            def run(self, argv, timeout=30):
                outer.events.append(list(argv)); code = 0; output = ''
                if argv[0]=='python3':
                    output=json.dumps(json.loads(Path(argv[-1]).read_bytes()))
                elif argv[:3]==['ip','-j','link']:
                    output=json.dumps([{'ifname':'eth0','ifindex':2,'address':'00:15:5d:01:6a:02','link_type':'ether','flags':['UP']}])
                elif argv[:3]==['ip','-j','-4']:
                    if 'address' in argv:
                        ip,prefix=outer.address.split('/')
                        output=json.dumps([{'ifname':'eth0','ifindex':2,'addr_info':[{'family':'inet','scope':'global','local':ip,'prefixlen':int(prefix),'dynamic':outer.address.startswith('192.168.137.192')}]}])
                    else:
                        output=json.dumps([{'dst':'default','gateway':outer.gateway,'dev':'eth0'}] if outer.gateway else [])
                elif argv[0]=='resolvectl':
                    output='Link 2 (eth0): '+' '.join(outer.dns)+'\n'
                elif argv[0]=='findmnt': output='tmpfs\n'
                elif argv[:2]==['systemctl','show']:
                    output=('LoadState=loaded\nActiveState=active\n' if argv[2]=='systemd-networkd.service' or argv[2] in outer.units
                            else 'LoadState=not-found\nActiveState=inactive\n')
                elif argv[:2]==['netplan','generate']:
                    code=int(outer.fail_generate)
                elif argv[:2]==['netplan','apply']:
                    if outer.fail_apply: code=1
                    else:
                        overlay=outer.root/'run/netplan/50-cloud-init.yaml'
                        value=json.loads((overlay if overlay.exists() else outer.source).read_bytes())['network']['ethernets']['eth0']
                        outer.address='192.168.137.192/24' if value.get('dhcp4') else value['addresses'][0]
                        outer.gateway=(value.get('routes') or [{}])[0].get('via','192.168.137.1' if value.get('dhcp4') else '')
                        outer.dns=value.get('nameservers',{}).get('addresses',['192.168.137.1'] if value.get('dhcp4') else [])
                elif argv[0]=='systemd-run':
                    unit=next(value.split('=',1)[1] for value in argv if value.startswith('--unit='))
                    if unit in outer.units:return subprocess.CompletedProcess(argv,1,'','unit already exists')
                    outer.units.add(unit)
                    role,identifier=argv[-2:]
                    try:
                        if role=='guard':
                            if outer.fail_arm: code=1
                            else: outer.manager.runtime.arm(identifier)
                        elif role=='recover':outer.manager.runtime.expire(identifier)
                        else: getattr(outer.manager.runtime,role)(identifier)
                    except Exception:code=1
                    if code and '--collect' in argv:outer.units.discard(unit)
                return subprocess.CompletedProcess(argv,code,output,'')
        self.host=FakeHost(self.root)
        self.manager=NetworkManager(self.host)
        self.addCleanup(patch.stopall)
        patch('server_setting.root_actions.os.geteuid',return_value=0,create=True).start()
        @contextmanager
        def no_lock(*args,**kwargs): yield
        patch.object(self.manager.core,'_lock',no_lock).start()
        patch.object(self.manager.runtime,'lock',no_lock).start()
        patch.object(self.manager.runtime,'proof',side_effect=lambda pid=None:copy.deepcopy(outer.proof)).start()
        patch.object(self.manager.runtime,'boot_id',side_effect=lambda:outer.boot).start()
        self.config={'interface':'eth0','mode':'static','address':'192.168.137.24/24','gateway':'192.168.137.1','dns':['192.168.137.1']}

    def fresh(self, identifier, address='192.168.137.24'):
        j=self.manager.runtime.journal(identifier)
        self.proof.update(socket_inode=str(int(self.proof['socket_inode'])+100),peer_port=self.proof['peer_port']+1,transport_pid=self.proof['transport_pid']+1,
                          transport_start_ticks=j['applied_ticks']+1,server_ip=address)

    def test_status_and_plan_on_ubuntu24_are_readonly_and_services_stay_blocked(self):
        status=self.manager.status()
        self.assertTrue(status['supported'],status['detail'])
        self.assertEqual(status['config']['mode'],'dhcp')
        plan=self.manager.plan(self.config)
        self.assertEqual(plan['timeout_seconds'],120)
        self.assertEqual(plan['source']['path'],'/etc/netplan/50-cloud-init.yaml')
        self.assertFalse((self.root/'var').exists())
        self.assertFalse((self.root/'run').exists())
        with self.assertRaises(RootActionError) as error:self.manager.core._gate('time')
        self.assertEqual(error.exception.code,'UNSUPPORTED_OS')

    def test_normalize_dns_rows_and_reject_bad_static(self):
        self.assertEqual(normalize(dict(self.config,dns=['192.168.137.1','','']))['dns'],['192.168.137.1'])
        for change in ({'address':'192.168.137.0/24'},{'gateway':'10.0.0.1'}, {'dns':['1.1.1.1']*4}, {'interface':'../eth0'}):
            with self.subTest(change=change),self.assertRaises(RootActionError):normalize(dict(self.config,**change))

    def test_save_only_private_draft(self):
        before=self.source.read_bytes()
        self.assertEqual(self.manager.save(self.config)['status'],'SAVED')
        self.assertEqual(self.manager.load_saved(),self.config)
        self.assertEqual(self.source.read_bytes(),before)
        self.assertFalse(any(e[0]=='netplan' for e in self.events))

    def test_generate_failure_and_guard_failure_leave_original_and_no_overlay(self):
        for field in ('fail_generate','fail_arm'):
            with self.subTest(field=field):
                setattr(self,field,True)
                with self.assertRaises(RootActionError):self.manager.apply(self.manager.plan(self.config))
                self.assertEqual(json.loads(self.source.read_bytes()),self.original)
                self.assertFalse((self.root/'run/netplan/50-cloud-init.yaml').exists())
                setattr(self,field,False)

    def test_pending_overlay_never_changes_persistent_source_and_old_ssh_cannot_confirm(self):
        before=self.source.read_bytes()
        result=self.manager.apply(self.manager.plan(self.config)); identifier=result['transaction_id']
        self.assertEqual(result['status'],'AWAITING_NEW_SSH')
        self.assertEqual(self.source.read_bytes(),before)
        self.assertTrue((self.root/'run/netplan/50-cloud-init.yaml').exists())
        with self.assertRaises(RootActionError):self.manager.confirm(identifier)
        with self.assertRaises(RootActionError):self.manager.core._check_pending()
        self.fresh(identifier,address='192.168.137.192')
        with self.assertRaises(RootActionError):self.manager.confirm(identifier)

    def test_new_target_ssh_persists_candidate_and_shared_status_is_legacy_compatible(self):
        result=self.manager.apply(self.manager.plan(self.config));identifier=result['transaction_id']
        self.fresh(identifier)
        self.assertEqual(self.manager.confirm(identifier)['status'],'CONFIRMED')
        self.assertFalse((self.root/'run/netplan/50-cloud-init.yaml').exists())
        self.assertEqual(json.loads(self.source.read_bytes())['network']['ethernets']['eth0']['addresses'],['192.168.137.24/24'])
        self.assertEqual(self.manager.runtime.journal(identifier)['status'],'APPLIED')
        self.manager.core._check_pending()

    def test_timeout_recovers_without_ui_or_global_lock(self):
        before=self.source.read_bytes()
        identifier=self.manager.apply(self.manager.plan(self.config))['transaction_id']
        with patch.object(self.manager.core,'_lock',side_effect=AssertionError('UI lock must not be used')):
            self.manager.runtime.expire(identifier)
        self.assertEqual(self.source.read_bytes(),before)
        self.assertEqual(self.address,'192.168.137.192/24')
        self.assertEqual(self.manager.runtime.journal(identifier)['status'],'ROLLED_BACK')

    def test_dhcp_manual_dns_is_explicit_but_blank_inherits(self):
        config=dict(self.config,mode='dhcp',address='',gateway='',dns=[])
        self.assertNotIn('nameservers',self.manager.plan(config)['candidate']['network']['ethernets']['eth0'])
        config['dns']=['9.9.9.9','1.1.1.1']
        node=self.manager.plan(config)['candidate']['network']['ethernets']['eth0']
        self.assertEqual(node['dhcp4-overrides']['use-dns'],False)
        self.assertEqual(node['nameservers']['addresses'],config['dns'])

    def test_global_cloud_init_disabled_marker_accepts_stock_installer_network_policy(self):
        policy=self.root/'etc/cloud/cloud.cfg.d/90-installer-network.cfg'
        policy.write_text(json.dumps(self.original['network']))
        marker=self.root/'etc/cloud/cloud-init.disabled';marker.touch();marker.chmod(0o600)
        self.assertTrue(self.manager.status()['supported'])
        marker.unlink()
        self.assertFalse(self.manager.status()['supported'])

    def test_status_preserves_pending_identity_when_live_address_observation_fails(self):
        identifier=self.manager.apply(self.manager.plan(self.config))['transaction_id']
        with patch.object(self.manager.runtime,'observed',side_effect=ValueError('no live IPv4')):
            status=self.manager.status()
        self.assertIsNotNone(status['pending'])
        self.assertEqual(status['pending']['transaction_id'],identifier)

    def test_watchdog_uses_fixed_system_interpreter_outside_caller_session(self):
        self.manager.apply(self.manager.plan(self.config))
        commands=[e for e in self.events if e[0]=='systemd-run']
        self.assertTrue(commands)
        for argv in commands:
            self.assertIn('/usr/bin/python3',argv)
            self.assertFalse(set(argv)&{'--scope','--pty','--pipe','--wait'})

    def test_reboot_unconfirmed_never_changes_original_persistent_source(self):
        before=self.source.read_bytes();identifier=self.manager.apply(self.manager.plan(self.config))['transaction_id']
        (self.root/'run/netplan/50-cloud-init.yaml').unlink();self.boot='second-boot'
        (self.root/'proc/sys/kernel/random/boot_id').write_text(self.boot)
        self.address='192.168.137.192/24';self.manager.runtime.boot()
        value=self.manager.runtime.journal(identifier)
        self.assertEqual(value['status'],'ROLLED_BACK')
        self.assertEqual(value['network_status'],'NETWORK_BOOT_ROLLED_BACK')
        self.assertEqual(self.source.read_bytes(),before)

    def test_confirm_interruption_before_and_after_persistent_commit_is_recoverable(self):
        identifier=self.manager.apply(self.manager.plan(self.config))['transaction_id'];self.fresh(identifier)
        real=self.manager.runtime.atomic
        def crash_before(absolute,*args,**kwargs):
            if absolute=='/etc/netplan/50-cloud-init.yaml':raise KeyboardInterrupt('power loss before persistent write')
            return real(absolute,*args,**kwargs)
        with patch.object(self.manager.runtime,'atomic',side_effect=crash_before),self.assertRaises(KeyboardInterrupt):self.manager.confirm(identifier)
        self.assertEqual(self.manager.runtime.journal(identifier)['status'],'NETWORK_CONFIRMING')
        self.manager.runtime.recover(identifier)
        self.assertEqual(self.manager.runtime.journal(identifier)['status'],'ROLLED_BACK')
        self.proof['server_ip']=self.address.split('/')[0]
        identifier=self.manager.apply(self.manager.plan(self.config))['transaction_id'];self.fresh(identifier)
        def crash_after(absolute,*args,**kwargs):
            result=real(absolute,*args,**kwargs)
            if absolute=='/etc/netplan/50-cloud-init.yaml':raise KeyboardInterrupt('power loss after persistent write')
            return result
        with patch.object(self.manager.runtime,'atomic',side_effect=crash_after),self.assertRaises(KeyboardInterrupt):self.manager.confirm(identifier)
        self.manager.runtime.recover(identifier)
        self.assertEqual(self.manager.runtime.journal(identifier)['status'],'APPLIED')
        self.assertFalse((self.root/'run/netplan/50-cloud-init.yaml').exists())

    def test_foreign_overlay_and_source_edit_are_not_overwritten_by_recovery(self):
        identifier=self.manager.apply(self.manager.plan(self.config))['transaction_id']
        overlay=self.root/'run/netplan/50-cloud-init.yaml';before=overlay.read_bytes()
        overlay.write_bytes(b'foreign-edit')
        with self.assertRaises(Exception):self.manager.runtime.recover(identifier)
        self.assertEqual(overlay.read_bytes(),b'foreign-edit')
        overlay.write_bytes(before);self.source.write_bytes(b'foreign-persistent')
        with self.assertRaises(Exception):self.manager.runtime.recover(identifier)
        self.assertEqual(self.source.read_bytes(),b'foreign-persistent')
        self.assertNotIn(self.manager.runtime.journal(identifier)['status'],{'APPLIED','ROLLED_BACK'})

    def test_failed_restore_stays_pending_and_can_retry(self):
        identifier=self.manager.apply(self.manager.plan(self.config))['transaction_id'];self.fail_apply=True
        with self.assertRaises(Exception):self.manager.runtime.expire(identifier)
        self.assertNotIn(self.manager.runtime.journal(identifier)['status'],{'APPLIED','ROLLED_BACK'})
        self.fail_apply=False;self.manager.runtime.expire(identifier)
        self.assertEqual(self.manager.runtime.journal(identifier)['status'],'ROLLED_BACK')

    def test_plan_rejects_foreign_hierarchy_renderer_and_stale_before_source(self):
        plan=self.manager.plan(self.config);self.source.write_bytes(self.source.read_bytes()+b'\n')
        with self.assertRaises(RootActionError) as error:self.manager.apply(plan)
        self.assertEqual(error.exception.code,'STALE_PLAN')
        extra=self.root/'etc/netplan/99-other.yaml';extra.write_text('{}')
        self.assertFalse(self.manager.status()['supported']);extra.unlink()
        changed=copy.deepcopy(self.original);changed['network']['renderer']='NetworkManager'
        self.source.write_text(json.dumps(changed))
        self.assertFalse(self.manager.status()['supported'])

    def test_preserves_ipv6_address_routes_and_ra_without_changing_dhcp6_policy(self):
        changed=copy.deepcopy(self.original);node=changed['network']['ethernets']['eth0']
        node.update(dhcp6=True,**{'accept-ra':True,'addresses':['2001:db8::2/64'],
                                'routes':[{'to':'default','via':'2001:db8::1'}]})
        self.source.write_text(json.dumps(changed))
        result=self.manager.plan(self.config)['candidate']['network']['ethernets']['eth0']
        self.assertTrue(result['dhcp6']);self.assertTrue(result['accept-ra'])
        self.assertIn('2001:db8::2/64',result['addresses']);self.assertIn({'to':'default','via':'2001:db8::1'},result['routes'])
        with self.assertRaises(RootActionError):self.manager.plan(dict(self.config,mode='dhcp',address='',gateway=''))

    def test_fresh_controlmaster_channel_and_forged_ssh_environment_cannot_confirm(self):
        identifier=self.manager.apply(self.manager.plan(self.config))['transaction_id']
        self.proof['caller_pid']+=100;self.proof['caller_start_ticks']+=100
        with patch.dict(os.environ,{'SSH_CONNECTION':'192.168.137.1 60002 192.168.137.24 22'}),self.assertRaises(RootActionError):
            self.manager.confirm(identifier)

    def test_expiry_stops_apply_and_confirm_cgroups_before_operation_lock(self):
        identifier=self.manager.apply(self.manager.plan(self.config))['transaction_id'];self.events.clear()
        real=self.manager.runtime.lock
        @contextmanager
        def lock():
            self.events.append(['operation-lock'])
            with real():yield
        with patch.object(self.manager.runtime,'lock',lock):self.manager.runtime.expire(identifier)
        lock_index=self.events.index(['operation-lock'])
        self.assertTrue(any(value[:2]==['systemctl','stop'] for value in self.events[:lock_index]))

    def test_expiry_missing_transient_confirm_unit_cannot_block_rollback(self):
        identifier=self.manager.apply(self.manager.plan(self.config))['transaction_id']
        real=self.manager.runtime.run
        def native_missing(argv,timeout=30):
            if argv[:2]==['systemctl','show'] and 'threewa-net-' in argv[2]:
                found='-apply-' in argv[2]
                return subprocess.CompletedProcess(argv,0 if found else 1,
                    'LoadState=loaded\nActiveState=active\n' if found else 'LoadState=not-found\nActiveState=inactive\n','')
            if argv[:2]==['systemctl','stop'] and any('-confirm-' in v for v in argv):
                return subprocess.CompletedProcess(argv,5,'','Unit not loaded')
            return real(argv,timeout=timeout)
        with patch.object(self.manager.runtime,'run',side_effect=native_missing):self.manager.runtime.expire(identifier)
        self.assertEqual(self.manager.runtime.journal(identifier)['status'],'ROLLED_BACK')

    def test_guard_watchdog_covers_bounded_restore_without_killing_recovery_cgroup(self):
        identifier=self.manager.apply(self.manager.plan(self.config))['transaction_id']
        command=next(e for e in self.events if e[0]=='systemd-run' and e[-2]=='guard')
        setting=next(e.split('=',2)[2] for e in command if e.startswith('--property=WatchdogSec='))
        watchdog=int(setting.rstrip('s'))
        bounds=[];real=self.manager.runtime.run
        def track(argv,timeout=30):bounds.append(timeout);return real(argv,timeout=timeout)
        with patch.object(self.manager.runtime,'run',side_effect=track):self.manager.runtime.expire(identifier)
        self.assertGreater(watchdog,sum(bounds))

    def test_existing_systemd_directory_metadata_is_preserved(self):
        directory=self.root/'etc/systemd/system';directory.mkdir(parents=True);directory.chmod(0o755)
        before=directory.stat().st_mode
        real=self.manager.core._secure_dir
        def private_dir(absolute):
            if absolute=='/etc/systemd/system':raise AssertionError('Cannot make a system configuration directory private')
            return real(absolute)
        with patch.object(self.manager.core,'_secure_dir',side_effect=private_dir):self.manager.apply(self.manager.plan(self.config))
        self.assertEqual(directory.stat().st_mode,before)

    def test_static_runtime_rejects_old_address_or_unrequested_default_route(self):
        identifier=self.manager.apply(self.manager.plan(self.config))['transaction_id']
        real=self.manager.runtime.observed
        value=real('eth0');value['addresses'].append('192.168.137.192/24')
        with patch.object(self.manager.runtime,'observed',return_value=value),self.assertRaises(Exception):
            self.manager.runtime.check_runtime(self.manager.runtime.journal(identifier))
        journal=self.manager.runtime.journal(identifier);journal['config']['gateway']=''
        with self.assertRaises(Exception):self.manager.runtime.check_runtime(journal)

    def test_lib_vendor_yaml_cannot_bypass_hierarchy_inventory(self):
        foreign=self.root/'lib/netplan/20-vendor.yaml';foreign.parent.mkdir(parents=True);foreign.write_text('{}')
        self.assertFalse(self.manager.status()['supported'])

    def test_pending_foreign_module_blocks_apply_before_live_netplan_writes(self):
        plan=self.manager.plan(self.config);identifier='20261026T000001Z-123456abcdef'
        self.manager.runtime.write_json(self.manager.runtime.directory(identifier)+'/journal.json',
            {'transaction_id':identifier,'kind':'firewall','module':'firewall','status':'FW_APPLYING'})
        with self.assertRaises(RootActionError) as error:self.manager.apply(plan)
        self.assertEqual(error.exception.code,'RECOVERY_REQUIRED')
        self.assertFalse((self.root/'run/netplan/50-cloud-init.yaml').exists())

    def test_frozen_artifact_tamper_stays_pending_and_never_overwrites_source(self):
        before=self.source.read_bytes();identifier=self.manager.apply(self.manager.plan(self.config))['transaction_id']
        worker=self.manager.runtime.path(self.manager.runtime.directory(identifier)+'/watchdog.py');worker.write_bytes(b'foreign-code')
        with self.assertRaises(Exception):self.manager.runtime.expire(identifier)
        self.assertEqual(self.source.read_bytes(),before)
        self.assertNotIn(self.manager.runtime.journal(identifier)['status'],{'APPLIED','ROLLED_BACK'})

    def test_stale_code_detected_again_after_acquiring_shared_lock(self):
        from server_setting.release_update import ReleaseError
        current=[True]
        def source_check(source,allow_pending=False):
            if not current[0]:raise ReleaseError('Reopen console','UPDATE_RESTART_REQUIRED')
        @contextmanager
        def switched_lock():current[0]=False;yield
        with patch('server_setting.release_update.ensure_current_release',side_effect=source_check),patch.object(self.manager.core,'_lock',switched_lock):
            with self.assertRaises(RootActionError) as error:self.manager.save(self.config)
        self.assertEqual(error.exception.code,'UPDATE_RESTART_REQUIRED')
        self.assertFalse(self.manager.runtime.path('/opt/3waServerSetting/config/network/draft.json').exists())

    def test_enabled_inactive_competing_renderer_and_persistent_backend_override_are_rejected(self):
        real=self.host.run
        def competing(argv,timeout=30):
            if argv[:3]==['systemctl','show','NetworkManager.service']:
                return subprocess.CompletedProcess(argv,0,'LoadState=loaded\nActiveState=inactive\nUnitFileState=enabled\n','')
            return real(argv,timeout=timeout)
        with patch.object(self.host,'run',side_effect=competing):self.assertFalse(self.manager.status()['supported'])
        path=self.root/'etc/systemd/network/10-netplan-eth0.network';path.parent.mkdir(parents=True);path.write_text('[Network]\nDHCP=yes\n')
        self.assertFalse(self.manager.status()['supported'])

    def test_static_runtime_cannot_be_satisfied_by_a_remaining_dynamic_lease(self):
        identifier=self.manager.apply(self.manager.plan(self.config))['transaction_id']
        observed=self.manager.runtime.observed('eth0');observed['dynamic']=['192.168.137.24']
        with patch.object(self.manager.runtime,'observed',return_value=observed),self.assertRaises(Exception):
            self.manager.runtime.check_runtime(self.manager.runtime.journal(identifier))

    def test_inconsistent_persistent_baseline_and_unmanaged_routes_are_rejected_before_trial(self):
        changed=copy.deepcopy(self.original);changed['network']['ethernets']['eth0']={
            'dhcp4':False,'addresses':['192.168.137.99/24'],'routes':[{'to':'default','via':'192.168.137.254'}]}
        self.source.write_text(json.dumps(changed))
        self.assertFalse(self.manager.status()['supported'])
        self.source.write_text(json.dumps(self.original))
        real=self.manager.runtime.observed
        value=real('eth0');value['routes'].append({'dst':'10.70.0.0/16','gateway':'192.168.137.250','dev':'eth0'})
        with patch.object(self.manager.runtime,'observed',return_value=value):
            self.assertFalse(self.manager.status()['supported'])
            with self.assertRaises(RootActionError):self.manager.plan(self.config)

    def test_failed_recovery_transient_unit_does_not_prevent_retry(self):
        identifier=self.manager.apply(self.manager.plan(self.config))['transaction_id']
        self.fail_apply=True
        with self.assertRaises(RootActionError):self.manager.recover(identifier)
        self.fail_apply=False
        self.assertEqual(self.manager.recover(identifier)['status'],'ROLLED_BACK')

    def test_additive_dhcp_static_addresses_and_routes_are_not_silently_ignored(self):
        for extra in ({'addresses':['10.70.0.2/24']},{'gateway4':'192.168.137.254'},
                      {'routes':[{'to':'default','via':'192.168.137.254'}]}):
            changed=copy.deepcopy(self.original);changed['network']['ethernets']['eth0'].update(extra)
            self.source.write_text(json.dumps(changed))
            with self.subTest(extra=extra):
                self.assertFalse(self.manager.status()['supported'])
                with self.assertRaises(RootActionError):self.manager.plan(self.config)

    def test_original_backend_must_regenerate_before_trying_a_valid_replacement(self):
        plan=self.manager.plan(self.config);real=self.manager.runtime.run
        def cannot_restore(argv,timeout=30):
            if argv[:2]==['netplan','generate'] and '--root-dir' in argv:
                root=Path(argv[-1])
                if not (root/'run/netplan/50-cloud-init.yaml').exists():
                    return subprocess.CompletedProcess(argv,1,'','original source cannot regenerate')
            return real(argv,timeout=timeout)
        with patch.object(self.manager.runtime,'run',side_effect=cannot_restore),self.assertRaises(RootActionError):
            self.manager.apply(plan)
        self.assertFalse((self.root/'run/netplan/50-cloud-init.yaml').exists())
        self.assertEqual(json.loads(self.source.read_bytes()),self.original)


if __name__=='__main__':unittest.main()

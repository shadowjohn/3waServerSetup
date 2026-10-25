"""BanIP transactions: real private files, substituted kernel/systemd boundaries."""
import copy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from server_setting.root_actions import RootActionError
try:
    from server_setting.root_banip import BanIPHost, BanIPManager, prepare_script, verify_companion
    from server_setting import banip_watchdog as guard
except ImportError:
    BanIPManager = None

RAW = b'#!/usr/bin/bash\nwhile IFS= read -r line || [ -n "$line" ]; do\n        ip="$(echo "$line" | tr -d \'\\r\' | xargs)"\n echo "$ip"\ndone\n'
MANAGED = RAW.replace(
    b'        ip="$(echo "$line" | tr -d \'\\r\' | xargs)"',
    b'        ip="${line//$\'\\r\'/}"\n        ip="${ip#"${ip%%[![:space:]]*}"}"\n        ip="${ip%"${ip##*[![:space:]]}"}"')


class BanIPTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(BanIPManager, 'BanIP transaction backend is missing')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ('etc', 'root', 'proc/sys/net/ipv4', 'proc/sys/net/ipv6/conf/all'):
            (self.root/name).mkdir(parents=True, exist_ok=True)
        (self.root/'etc/os-release').write_text('ID=ubuntu\nVERSION_ID="26.04"\n')
        for name in ('ipv4/ip_forward', 'ipv6/conf/all/forwarding'):
            (self.root/'proc/sys/net'/name).write_text('0\n')
        self.sets, self.rules, self.events = {}, [], []
        self.table, self.fail_arm, self.fail_run = False, False, False
        self.bad_summary=False
        self.hidden_rule=False
        self.materialized_chains=('INPUT','FORWARD','OUTPUT')
        self.serialized_chains=None
        self.missing = set()
        self.download = RAW
        self.proof = {'caller_pid':os.getpid(),'caller_start_ticks':1,'transport_pid':100,
            'transport_start_ticks':1,'socket_inode':'100','peer_ip':'192.168.137.1','peer_port':42000,
            'server_ip':'192.168.137.26','server_port':22,'boot_id':'test-boot'}
        outer = self

        class FakeHost(BanIPHost):
            def available(self, name):
                return name not in outer.missing
            def download_script(self):
                return outer.download
            def run(self, argv, timeout=30):
                outer.events.append(list(argv))
                output, code = '', 0
                name = argv[0]
                args = list(argv[1:])
                if name=='iptables-save':
                    chains=outer.materialized_chains if outer.serialized_chains is None else outer.serialized_chains
                    output=('*filter\n'+''.join(':'+chain+' ACCEPT [0:0]\n' for chain in chains)+''.join(shlex.join(row)+'\n' for row in outer.rules)+'COMMIT\n') if outer.table else ''
                elif name == 'dpkg-query':
                    code = int(args[-1] in outer.missing)
                    output = '' if code else 'install ok installed\n'
                elif name == 'iptables':
                    if args == ['--version']:
                        output = 'iptables v1.8.11 (nf_tables)\n'
                    else:
                        if args[:2] == ['-w','5']: args=args[2:]
                        if args == ['-S']:
                            output='-P INPUT ACCEPT\n-P FORWARD ACCEPT\n-P OUTPUT ACCEPT\n'+''.join(shlex.join(row)+'\n' for row in outer.rules)
                        elif args[:2] == ['-I','INPUT']:
                            outer.table=True;outer.rules.insert(0,['-A','INPUT',*args[2:]])
                        elif args[:2] == ['-D','INPUT']:
                            outer.rules.remove(['-A','INPUT',*args[2:]])
                        else: raise AssertionError(argv)
                elif name == 'nft':
                    if args[:4] == ['delete','table','ip','handle']:
                        assert not outer.rules;outer.table=False
                    else:
                        graph=[]
                        if outer.table:
                            graph=[{'table':{'family':'ip','name':'filter','handle':20}}]
                            for chain in outer.materialized_chains:
                                graph.append({'chain':{'family':'ip','table':'filter','name':chain,'hook':chain.lower(),'type':'filter','prio':0,'policy':'accept'}})
                            graph += [{'rule':{'family':'ip','table':'filter','chain':'INPUT','expr':row}} for row in outer.rules]
                            if outer.hidden_rule:
                                graph.append({'rule':{'family':'ip','table':'filter','chain':'INPUT','expr':[{'unrecognized':True}]}})
                        output=json.dumps({'nftables':graph})
                elif name == 'ipset':
                    if args == ['list','-name']: output=''.join(key+'\n' for key in outer.sets)
                    elif args[:1] == ['save']:
                        key=args[1]
                        if key not in outer.sets: code=1
                        else: output=f'create {key} hash:ip family inet hashsize 4096 maxelem 65536\n'+''.join(f'add {key} {ip}\n' for ip in sorted(outer.sets[key]))
                    elif args[0]=='create': outer.sets[args[1]]=set()
                    elif args[0]=='destroy': outer.sets.pop(args[1])
                    elif args[0]=='swap': outer.sets[args[1]],outer.sets[args[2]]=outer.sets[args[2]],outer.sets[args[1]]
                    elif args[:1]==['-file']:
                        for line in Path(args[1]).read_text().splitlines():
                            fields=line.split()
                            if fields[0]=='create': outer.sets[fields[1]]=set()
                            else: outer.sets[fields[1]].add(fields[2])
                    else: raise AssertionError(argv)
                elif name == 'systemctl':
                    if args[0]=='show': output='LoadState=not-found\nActiveState=inactive\nUnitFileState=disabled\n'
                    elif args[0]=='start':
                        unit=args[1];identifier=unit.split('@')[1].split('.')[0]
                        runtime=outer.manager.runtime
                        if 'guard@' in unit:
                            if outer.fail_arm: code=1
                            else: runtime.arm(identifier)
                        elif 'run@' in unit:
                            assert outer.rules==runtime.rules(outer.proof)
                            assert outer.sets.get('blacklist') is not None
                            if outer.fail_run: code=1
                            else:
                                outer.sets['blacklist']={'198.51.100.8','203.0.113.9'}
                                runtime.write_json(runtime.directory(identifier)+'/data-summary.json',
                                    {'raw_sha256':'public-feed','count':2,'set_sha256': 'wrong' if outer.bad_summary else hashlib.sha256(b'198.51.100.8\n203.0.113.9\n').hexdigest(),
                                     'ends_with_newline':False,'source_url':'https://3wa.tw/myapache_banip/data/banip_csv.txt'})
                        elif 'rollback@' in unit: runtime.rollback(identifier,manual=True)
                    elif args[0] not in {'daemon-reload','stop'}: raise AssertionError(argv)
                elif name=='apt-get': outer.missing.clear()
                else: raise AssertionError(argv)
                return subprocess.CompletedProcess(argv,code,output,'')

        self.host=FakeHost(self.root)
        self.manager=BanIPManager(self.host)
        self.addCleanup(patch.stopall)
        patch('server_setting.root_actions.os.geteuid',return_value=0,create=True).start()
        patch('server_setting.root_banip.SOURCE_SHA256',hashlib.sha256(RAW).hexdigest()).start()
        patch('server_setting.root_banip.SCRIPT_SHA256',hashlib.sha256(MANAGED).hexdigest()).start()
        patch.object(self.manager.runtime,'proof',side_effect=lambda pid=None:copy.deepcopy(outer.proof)).start()
        patch.object(self.manager.runtime,'boot_id',return_value='test-boot').start()
        @contextmanager
        def unlocked(*args): yield
        patch.object(self.manager.core,'_lock',unlocked).start()
        patch.object(self.manager.runtime,'lock',unlocked).start()

    def test_readonly_plan_lists_only_missing_dependencies_without_state(self):
        self.missing={'ipset','curl'}
        plan=self.manager.plan()
        self.assertEqual(plan['packages'],['curl','ipset'])
        self.assertFalse((self.root/'var').exists())
        self.assertFalse((self.root/'root/my_ban_ips.sh').exists())
        self.assertFalse(any(row[0]=='apt-get' for row in self.events))

    def test_native_input_only_with_three_virtual_serialized_policies(self):
        self.materialized_chains=('INPUT',)
        self.serialized_chains=('INPUT','FORWARD','OUTPUT')
        result=self.manager.update(self.manager.plan())
        self.assertEqual(result['status'],'AWAITING_NEW_SSH')
        self.manager.runtime.expire(result['transaction_id'])
        self.assertFalse(self.table)
        self.assertFalse(self.rules)
        self.assertFalse(self.sets)

    def test_pinned_source_and_existing_script_are_checked_before_kernel_changes(self):
        self.download=b'untrusted'
        with self.assertRaises(RootActionError): self.manager.update(self.manager.plan())
        self.assertFalse(self.sets);self.assertFalse(self.rules)
        self.download=RAW
        (self.root/'root/my_ban_ips.sh').write_bytes(b'personal script')
        with self.assertRaises(RootActionError): self.manager.update(self.manager.plan())
        self.assertEqual((self.root/'root/my_ban_ips.sh').read_bytes(),b'personal script')

    def test_unknown_set_collision_and_modified_plan_are_rejected(self):
        plan=self.manager.plan();plan['packages']=['unreviewed']
        with self.assertRaises(RootActionError): self.manager.update(plan)
        self.sets['blacklist_new']={'203.0.113.10'}
        with self.assertRaises(RootActionError): self.manager.plan()
        self.assertFalse(self.rules)

    def test_guard_readiness_precedes_every_kernel_mutation(self):
        self.fail_arm=True
        with self.assertRaises(RootActionError): self.manager.update(self.manager.plan())
        self.assertFalse(self.sets);self.assertFalse(self.rules)

    def test_exact_managed_script_runs_in_private_tmp_after_rescue_rules(self):
        result=self.manager.update(self.manager.plan())
        self.assertEqual(result['status'],'AWAITING_NEW_SSH')
        self.assertEqual((self.root/'root/my_ban_ips.sh').read_bytes(),MANAGED)
        self.assertEqual((self.root/'var/lib/3wa-server-setup/banip/source.sh').read_bytes(),RAW)
        unit=(self.root/'etc/systemd/system/threewa-banip-run@.service').read_text()
        self.assertIn('TemporaryFileSystem=/tmp:rw,size=8M,mode=0700',unit)
        self.assertIn('KillMode=control-group',unit)
        self.assertEqual(self.rules,self.manager.runtime.rules(self.proof))
        self.assertEqual(self.sets['blacklist'],{'198.51.100.8','203.0.113.9'})
        self.assertFalse(self.manager.status()['persistent'])

    def test_confirmation_requires_new_physical_ssh_and_companion_detects_external_edits(self):
        result=self.manager.update(self.manager.plan());identifier=result['transaction_id']
        with self.assertRaises(RootActionError): self.manager.confirm(identifier)
        journal=self.manager.runtime.journal(identifier)
        self.proof.update(socket_inode='200',peer_port=42001,transport_start_ticks=journal['applied_ticks']+1)
        self.assertEqual(self.manager.confirm(identifier)['status'],'BANIP_CONFIRMED')
        self.assertTrue(verify_companion(self.manager.runtime))
        self.rules.append(['-A','INPUT','-j','ACCEPT'])
        self.assertFalse(verify_companion(self.manager.runtime))

    def test_independent_expiry_restores_initial_owned_objects(self):
        result=self.manager.update(self.manager.plan())
        with patch.object(self.manager.core,'_lock',side_effect=AssertionError('global lock unavailable')):
            self.manager.runtime.expire(result['transaction_id'])
        self.assertFalse(self.rules);self.assertFalse(self.sets);self.assertFalse(self.table)
        self.assertEqual(self.manager.runtime.journal(result['transaction_id'])['status'],'BANIP_ROLLED_BACK')

    def test_failed_update_restores_previous_confirmed_membership(self):
        result=self.manager.update(self.manager.plan());identifier=result['transaction_id']
        journal=self.manager.runtime.journal(identifier)
        self.proof.update(socket_inode='200',peer_port=42001,transport_start_ticks=journal['applied_ticks']+1)
        self.manager.confirm(identifier)
        previous=copy.deepcopy(self.sets)
        self.fail_run=True
        with self.assertRaises(RootActionError): self.manager.update(self.manager.plan())
        self.assertEqual(self.sets,previous)
        self.assertTrue(verify_companion(self.manager.runtime))

    def test_eof_fix_changes_only_the_reviewed_unique_loop(self):
        self.assertEqual(prepare_script(RAW),MANAGED)
        with self.assertRaises(RootActionError): prepare_script(RAW+RAW)
        if os.name=='posix':
            loop=MANAGED.decode().split('\n',1)[1]
            output=subprocess.check_output(['/bin/bash','--noprofile','--norc','-c',loop],input=b' 198.51.100.8 \r\n\t203.0.113.9')
            self.assertEqual(output,b'198.51.100.8\n203.0.113.9\n')

    def test_current_download_summary_must_match_the_full_loaded_set(self):
        self.bad_summary=True
        with self.assertRaises(RootActionError):
            self.manager.update(self.manager.plan())
        self.assertFalse(self.sets)
        self.assertFalse(self.rules)

    def test_strict_feed_summary_includes_unterminated_final_ip_and_numeric_order(self):
        self.assertTrue(hasattr(guard,'feed_summary'),'Current-feed verification is missing')
        summary=guard.feed_summary(b' 10.0.0.1 \r\n\r\n2.0.0.1')
        self.assertEqual(summary['count'],2)
        self.assertFalse(summary['ends_with_newline'])
        self.assertEqual(summary['set_sha256'],hashlib.sha256(b'2.0.0.1\n10.0.0.1\n').hexdigest())
        for raw in (b'',b'1.2.3.999',b'1.2.3.4\n1.2.3.4\n',b'"1.2.3.4"',b'1.2. 3.4'):
            with self.subTest(raw=raw),self.assertRaises((ValueError,guard.FirewallError)):
                guard.feed_summary(raw)

    def test_guard_restart_resumes_the_original_deadline(self):
        result=self.manager.update(self.manager.plan())
        identifier=result['transaction_id']
        original=self.manager.runtime.journal(identifier)['deadline_boottime']
        self.manager.runtime.arm(identifier)
        self.assertEqual(self.manager.runtime.journal(identifier)['deadline_boottime'],original)

    def test_private_standalone_worker_needs_no_application_imports(self):
        result=self.manager.update(self.manager.plan())
        source=(self.root/'var/lib/3wa-server-setup/banip/watchdog.py').read_text()
        namespace={'__name__':'private_banip_worker','__package__':None}
        exec(compile(source,'private-watchdog.py','exec'),namespace)
        worker=namespace['Runtime'](self.root,self.host.run)
        self.assertEqual(worker.journal(result['transaction_id'])['kind'],'banip')
        self.assertEqual(worker.snapshot()['count'],2)

    def test_pending_deadline_remains_visible_when_ownership_inspection_fails(self):
        result=self.manager.update(self.manager.plan())
        self.host.path(guard.OWNED).unlink()
        status=self.manager.status()
        self.assertFalse(status['supported'])
        self.assertIsNotNone(status['pending'])
        self.assertEqual(status['pending']['transaction_id'],result['transaction_id'])

    def test_native_filter_policy_packet_counters_are_observations(self):
        actual=self.host.run
        def counters(argv,timeout=30):
            result=actual(argv,timeout)
            if argv[0]=='iptables-save':result.stdout=result.stdout.replace('[0:0]','[99:12345]')
            return result
        with patch.object(self.host,'run',side_effect=counters):
            manager=BanIPManager(self.host)
            self.assertTrue(manager.status()['supported'])

    def test_nft_filter_rule_not_visible_to_iptables_cannot_be_adopted(self):
        self.table=True
        self.hidden_rule=True
        with self.assertRaises(RootActionError):
            self.manager.plan()
        self.assertFalse(self.sets)

    def test_native_materialized_input_only_updates_and_rolls_back(self):
        self.materialized_chains=('INPUT',)
        result=self.manager.update(self.manager.plan())
        self.assertEqual(result['status'],'AWAITING_NEW_SSH')
        self.assertEqual(self.manager.rollback(result['transaction_id'])['status'],'BANIP_ROLLED_BACK')
        self.assertFalse(self.table)
        self.assertFalse(self.sets)

    def test_initial_rollback_removes_drop_before_ssh_rescue_and_loopback(self):
        result=self.manager.update(self.manager.plan())
        self.events.clear()
        self.manager.rollback(result['transaction_id'])
        removed=[row for row in self.events if row[:5]==['iptables','-w','5','-D','INPUT']]
        self.assertEqual(removed[0][-1],'DROP')
        self.assertEqual(removed[-1][5:],['-i','lo','-j','ACCEPT'])

    def test_initial_rollback_recovers_a_kill_after_drop_was_removed(self):
        self.materialized_chains=('INPUT',)
        result=self.manager.update(self.manager.plan())
        self.rules.remove(self.manager.runtime.rules(self.proof)[-1])
        self.assertEqual(self.manager.rollback(result['transaction_id'])['status'],'BANIP_ROLLED_BACK')
        self.assertFalse(self.rules)
        self.assertFalse(self.sets)
        self.assertFalse(self.table)


if __name__=='__main__': unittest.main()

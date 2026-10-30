"""Network drafts and reviewed changes; backend fixtures never touch the host."""
import asyncio
import contextlib
import copy
import io
import json
import unittest
from unittest.mock import patch


DHCP = {'interface':'eth0','mode':'dhcp','address':'','gateway':'','dns':[], 'ipv6':'preserve'}
STATIC = {'interface':'eth0','mode':'static','address':'192.168.137.24/24','gateway':'192.168.137.1',
          'dns':['1.1.1.1','8.8.8.8','2606:4700:4700::1111'], 'ipv6':'preserve'}
COMMAND = '/opt/3waServerSetting/run.sh --network-confirm network-fixture --confirm'


class NetworkFixture:
    def __init__(self):
        self.calls = []
        self.current = copy.deepcopy(DHCP)
        self.saved = copy.deepcopy(STATIC)
        self.pending = None
        self.fail = False

    def status(self):
        self.calls.append(('status',))
        if self.fail:
            raise RuntimeError('PRIVATE_NETWORK_INTERNAL')
        return {'supported':True,'detail':'Single Ethernet Netplan recipe.','interface':'eth0','interfaces':['eth0','eth1'],
                'renderer':'networkd','config':copy.deepcopy(self.current),
                'observed':{'addresses':['192.168.137.192/24'],'routes':[{'dst':'default','gateway':'192.168.137.1','dev':'eth0'}],
                            'dns':['192.168.137.1']},'source':{'path':'/etc/netplan/50-cloud-init.yaml','sha256':'a'*64},
                'saved':bool(self.saved),'pending':copy.deepcopy(self.pending)}

    def load_saved(self):
        self.calls.append(('load_saved',));return copy.deepcopy(self.saved)

    def save(self, config):
        self.calls.append(('save',copy.deepcopy(config)));self.saved=copy.deepcopy(config)
        return {'status':'SAVED','module':'network'}

    def plan(self, config):
        self.calls.append(('plan',copy.deepcopy(config)))
        return {'schema':1,'module':'network','config':copy.deepcopy(config),'target_ip':config['address'].split('/')[0] or None,
                'timeout_seconds':120,'source':{'path':'/etc/netplan/50-cloud-init.yaml','sha256':'a'*64},
                'notes':['A new SSH connection to the target IP must confirm before timeout; otherwise rollback.'],
                'fingerprint':'network-plan','candidate':'PRIVATE_NETWORK_INTERNAL','before':{'secret':'PRIVATE_NETWORK_INTERNAL'}}

    def apply(self, plan):
        self.calls.append(('apply',copy.deepcopy(plan)))
        self.pending={'transaction_id':'network-fixture','status':'AWAITING_NEW_SSH','remaining_seconds':120,
                      'deadline_boottime':9999,'target_ip':plan['target_ip'],'confirmation_command':COMMAND}
        return {'status':'AWAITING_NEW_SSH','module':'network',**self.pending}

    def confirm(self, transaction_id):
        self.calls.append(('confirm',transaction_id));return {'status':'CONFIRMED','transaction_id':transaction_id}

    def recover(self, transaction_id=None):
        self.calls.append(('recover',transaction_id));self.pending=None
        return {'status':'ROLLED_BACK','module':'network'}


class NetworkCliTests(unittest.TestCase):
    def invoke(self, arguments, manager, config=None, root=True):
        from server_setting.__main__ import main
        output = io.StringIO()
        with patch('server_setting.__main__.network_management_available',return_value=root,create=True), \
                patch('sys.stdin',io.StringIO(json.dumps(config) if config is not None else '')), contextlib.redirect_stdout(output):
            code = main(arguments, network_manager=manager)
        return code,json.loads(output.getvalue()),output.getvalue()

    def test_status_readonly_and_root_and_confirmation_gates_before_backend(self):
        manager=NetworkFixture()
        code,result,_=self.invoke(['--network-status'],manager,root=False)
        self.assertEqual(code,0);self.assertEqual(result['observed']['addresses'],['192.168.137.192/24'])
        for operation in ('save','apply','confirm','recover'):
            arguments=['--network-'+operation]+(['network-fixture'] if operation=='confirm' else [])
            if operation in {'save','apply'}:arguments+=['--network-config','-']
            for root,confirm,expected in ((False,True,'ROOT_REQUIRED'),(True,False,'CONFIRMATION_REQUIRED')):
                fresh=NetworkFixture()
                code,result,_=self.invoke(arguments+(['--confirm'] if confirm else []),fresh,STATIC,root)
                self.assertNotEqual(code,0);self.assertEqual(result['code'],expected);self.assertEqual(fresh.calls,[])

    def test_load_save_plan_apply_and_new_ssh_confirmation_are_distinct(self):
        manager=NetworkFixture()
        code,result,_=self.invoke(['--network-load'],manager)
        self.assertEqual((code,result['config']),(0,STATIC))
        code,result,_=self.invoke(['--network-save','--network-config','-','--confirm'],manager,STATIC)
        self.assertEqual((code,result['status']),(0,'SAVED'));self.assertEqual(manager.calls[-1],('save',STATIC))
        code,result,text=self.invoke(['--network-plan','--network-config','-'],manager,STATIC)
        self.assertEqual(code,0);self.assertEqual(result['plan']['target_ip'],'192.168.137.24')
        self.assertNotIn('PRIVATE_NETWORK_INTERNAL',text)
        code,result,text=self.invoke(['--network-apply','--network-config','-','--confirm'],manager,STATIC)
        self.assertEqual((code,result['status']),(0,'AWAITING_NEW_SSH'));self.assertIn(COMMAND,text)
        self.assertEqual(manager.calls[-1][1]['candidate'],'PRIVATE_NETWORK_INTERNAL')
        code,result,_=self.invoke(['--network-confirm','network-fixture','--confirm'],manager)
        self.assertEqual((code,result['status']),(0,'CONFIRMED'))
        for arguments,expected in ((['--network-recover','--confirm'],None),(['--network-recover','network-fixture','--confirm'],'network-fixture')):
            code,result,_=self.invoke(arguments,manager)
            self.assertEqual((code,result['status']),(0,'ROLLED_BACK'));self.assertEqual(manager.calls[-1],('recover',expected))

    def test_invalid_combinations_do_not_read_or_mutate_backend(self):
        cases=[['--network-status','--update-status'],['--network-status','--text'],['--network-plan'],
               ['--network-load','--network-config','-'],['--network-config','-'],
               ['--network-status','--values','{}'],['--network-status','--start'],
               ['--network-status','--startup-config','-'],['--network-status','--release-tag','v2026.10.26.001'],
               ['--network-status','--config','-'],['--network-confirm','bad/id','--confirm'],
               ['--network-confirm','','--confirm']]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                manager=NetworkFixture();code,result,_=self.invoke(arguments,manager,DHCP)
                self.assertEqual(code,2);self.assertEqual(result['code'],'INVALID_ARGUMENT');self.assertEqual(manager.calls,[])

    def test_backend_errors_are_sanitized(self):
        manager=NetworkFixture();manager.fail=True
        code,result,text=self.invoke(['--network-status'],manager)
        self.assertEqual((code,result['code']),(1,'NETWORK_FAILED'));self.assertNotIn('PRIVATE_NETWORK_INTERNAL',text)

    def test_new_ssh_rejection_is_preserved_without_exposing_backend_details(self):
        class FreshSshError(RuntimeError):
            code='NEW_SSH_REQUIRED'
        manager=NetworkFixture()
        with patch.object(manager,'confirm',side_effect=FreshSshError('PRIVATE_NETWORK_INTERNAL')):
            code,result,text=self.invoke(['--network-confirm','network-fixture','--confirm'],manager)
        self.assertEqual((code,result['code']),(1,'NEW_SSH_REQUIRED'));self.assertNotIn('PRIVATE_NETWORK_INTERNAL',text)

    def test_platform_gate_supports_only_root_ubuntu22_24_and_26(self):
        from pathlib import Path
        import server_setting
        for system,release,expected in (('ubuntu','22.04',True),('ubuntu','24.04',True),
                ('ubuntu','26.04',True),('ubuntu','20.04',False),('ubuntu','unknown',False),('debian','22.04',False)):
            with self.subTest(system=system,release=release), patch('server_setting.platform.system',return_value='Linux'), \
                    patch('server_setting.os.geteuid',return_value=0,create=True), \
                    patch.object(Path,'read_text',return_value='ID='+system+'\nVERSION_ID="'+release+'"\n'):
                self.assertEqual(server_setting.network_management_available(),expected)
        with patch('server_setting.platform.system',return_value='Linux'),patch('server_setting.os.geteuid',return_value=1000,create=True):
            self.assertFalse(server_setting.network_management_available())


class NetworkUiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)

    def application(self,manager,root=True,updater=False):
        from server_setting.app import SettingApp
        from test_release_ui import ReleaseFixture
        with patch('server_setting.app.root_management_available',return_value=False), \
                patch('server_setting.app.update_management_available',return_value=updater), \
                patch('server_setting.app.network_management_available',return_value=root,create=True):
            return SettingApp(network_manager=manager,release_manager=ReleaseFixture())

    async def until(self,pilot,condition):
        for _ in range(150):
            await pilot.pause(.02)
            if condition():return
        self.fail('Network UI did not settle')

    async def button(self,app,pilot,identifier):
        app.screen.query_one('#'+identifier).focus();await pilot.press('enter');await pilot.pause(.04)

    def set_static(self,app):
        from textual.widgets import Input,Select
        app.query_one('#network-mode',Select).value='static'
        app.query_one('#network-address',Input).value=STATIC['address']
        app.query_one('#network-gateway',Input).value=STATIC['gateway']
        for index,value in enumerate(STATIC['dns'],1):app.query_one('#network-dns'+str(index),Input).value=value

    async def test_root24_draft_refresh_save_and_load_keep_operations_distinct(self):
        from textual.widgets import Input,Select,TabbedContent
        manager=NetworkFixture();app=self.application(manager)
        async with app.run_test(size=(100,32)) as pilot:
            await pilot.pause(.1);self.assertFalse(app.root_mode)
            app.query_one('#pages',TabbedContent).active='tab-network';await pilot.pause(.05)
            self.set_static(app)
            await self.button(app,pilot,'network-status');await self.until(pilot,lambda:not app.root_busy)
            self.assertEqual(app.query_one('#network-address',Input).value,STATIC['address'])
            await self.button(app,pilot,'network-save');await self.until(pilot,lambda:not app.root_busy)
            self.assertEqual(next(row for row in manager.calls if row[0]=='save'),('save',STATIC))
            self.assertFalse(any(row[0] in {'plan','apply','confirm'} for row in manager.calls))
            await self.button(app,pilot,'network-current');await self.until(pilot,lambda:not app.root_busy)
            self.assertEqual(app.query_one('#network-mode',Select).value,'dhcp')
            await self.button(app,pilot,'network-saved');await self.until(pilot,lambda:not app.root_busy)
            self.assertEqual(app.query_one('#network-address',Input).value,STATIC['address'])
            self.assertEqual(app.query_one('#network-dns3',Input).value,STATIC['dns'][2])
            app.query_one('#network-mode',Select).value='dhcp'
            await self.button(app,pilot,'network-save');await self.until(pilot,lambda:not app.root_busy)
            self.assertEqual([row for row in manager.calls if row[0]=='save'][-1],('save',dict(DHCP,dns=STATIC['dns'])))
            for index in range(1,4):app.query_one('#network-dns'+str(index),Input).value=''
            await self.button(app,pilot,'network-save');await self.until(pilot,lambda:not app.root_busy)
            self.assertEqual([row for row in manager.calls if row[0]=='save'][-1],('save',DHCP))

    async def test_ipv6_draft_survives_refresh_load_and_review_cancel(self):
        from textual.widgets import Select,TabbedContent,TextArea
        manager=NetworkFixture();manager.current.pop('ipv6')
        app=self.application(manager)
        async with app.run_test(size=(100,32)) as pilot:
            await pilot.pause(.1);app.query_one('#pages',TabbedContent).active='tab-network';await pilot.pause(.05)
            field=app.query_one('#network-ipv6',Select)
            self.assertEqual(field.value,'preserve')
            self.assertFalse(field.disabled)
            field.value='disabled'
            await self.button(app,pilot,'network-status');await self.until(pilot,lambda:not app.root_busy)
            self.assertEqual(field.value,'disabled')
            await self.button(app,pilot,'network-save');await self.until(pilot,lambda:not app.root_busy)
            self.assertEqual(manager.saved['ipv6'],'disabled')
            self.assertFalse(any(row[0] in {'plan','apply','confirm'} for row in manager.calls))
            field.value='auto'
            await self.button(app,pilot,'network-saved');await self.until(pilot,lambda:not app.root_busy)
            self.assertEqual(field.value,'disabled')
            await self.button(app,pilot,'network-apply');await self.until(pilot,lambda:type(app.screen).__name__=='RootReview')
            self.assertIn('"ipv6": "disabled"',app.screen.query_one('#root-review-detail',TextArea).text)
            await pilot.press('escape');await self.until(pilot,lambda:not app.root_busy)
            self.assertFalse(any(row[0] in {'apply','confirm'} for row in manager.calls))

    async def test_review_cancel_apply_pending_new_ssh_and_recover_are_separate(self):
        from textual.widgets import Button,TabbedContent,TextArea
        manager=NetworkFixture();app=self.application(manager,updater=True)
        async with app.run_test(size=(100,32)) as pilot:
            await pilot.pause(.1);app.query_one('#pages',TabbedContent).active='tab-network';await pilot.pause(.05)
            self.set_static(app)
            await self.button(app,pilot,'network-apply');await self.until(pilot,lambda:type(app.screen).__name__=='RootReview')
            detail=app.screen.query_one('#root-review-detail',TextArea).text
            self.assertIn('192.168.137.24',detail);self.assertIn('120',detail);self.assertIn('new SSH',detail)
            self.assertNotIn('PRIVATE_NETWORK_INTERNAL',detail)
            await pilot.press('escape');await self.until(pilot,lambda:not app.root_busy)
            self.assertFalse(any(row[0]=='apply' for row in manager.calls))
            await self.button(app,pilot,'network-apply');await self.until(pilot,lambda:type(app.screen).__name__=='RootReview')
            await self.button(app,pilot,'root-confirm');await self.until(pilot,lambda:not app.root_busy)
            self.assertIn(COMMAND,app.query_one('#out-network',TextArea).text)
            self.assertFalse(app.query('#network-confirm'));self.assertFalse(any(row[0]=='confirm' for row in manager.calls))
            self.assertTrue(app.query_one('#network-apply',Button).disabled)
            self.assertTrue(app.query_one('#update-apply',Button).disabled)
            self.assertFalse(app.query_one('#network-recover',Button).disabled)
            await self.button(app,pilot,'network-recover');await self.until(pilot,lambda:type(app.screen).__name__=='RootReview')
            await self.button(app,pilot,'root-confirm');await self.until(pilot,lambda:not app.root_busy)
            self.assertEqual(manager.calls[-2][0],'recover');self.assertFalse(manager.pending)

    async def test_nonroot_controls_are_observation_only(self):
        from textual.widgets import Input,Select,TextArea
        manager=NetworkFixture();app=self.application(manager,root=False)
        async with app.run_test(size=(80,24)) as pilot:
            await pilot.pause(.1)
            for identifier in ('network-save','network-apply','network-recover'):self.assertFalse(app.query('#'+identifier))
            self.assertTrue(app.query_one('#network-address',Input).disabled)
            self.assertTrue(app.query_one('#network-ipv6',Select).disabled)
            self.assertIn('192.168.137.192',str(app.query_one('#network-live').render()))
            app._begin_network_operation('apply')
            self.assertIn('ROOT_REQUIRED',app.query_one('#out-network',TextArea).text)
            self.assertFalse(any(row[0] in {'plan','save','apply','confirm'} for row in manager.calls))

    async def test_pending_and_status_failure_preserve_block_and_draft(self):
        from textual.widgets import Button,Input,Select,TabbedContent,TextArea
        manager=NetworkFixture();manager.pending={'transaction_id':'network-fixture','status':'AWAITING_NEW_SSH','remaining_seconds':100,'confirmation_command':COMMAND}
        app=self.application(manager)
        async with app.run_test(size=(80,24)) as pilot:
            await pilot.pause(.1);app.query_one('#pages',TabbedContent).active='tab-network';await pilot.pause(.05)
            self.assertTrue(app.query_one('#network-save',Button).disabled)
            self.assertTrue(app.query_one('#network-ipv6',Select).disabled)
            self.assertFalse(app.query_one('#network-recover',Button).disabled)
            manager.fail=True
            await self.button(app,pilot,'network-status');await self.until(pilot,lambda:not app.root_busy)
            self.assertTrue(app.network_state['pending']);self.assertTrue(app.query_one('#network-apply',Button).disabled)
            self.assertNotIn('PRIVATE_NETWORK_INTERNAL',app.query_one('#out-network',TextArea).text)

    async def test_network_page_keyboard_directions_from_buttons(self):
        from textual.widgets import TabbedContent
        app=self.application(NetworkFixture())
        async with app.run_test(size=(100,32)) as pilot:
            await pilot.pause(.1);pages=app.query_one('#pages',TabbedContent);pages.active='tab-network';await pilot.pause(.05)
            app.query_one('#network-status').focus();await pilot.press('ctrl+right');await pilot.pause()
            self.assertEqual(pages.active,'tab-firewall')
            app.query_one('#fw-preview').focus();await pilot.press('ctrl+left');await pilot.pause()
            self.assertEqual(pages.active,'tab-network')


if __name__=='__main__':unittest.main()

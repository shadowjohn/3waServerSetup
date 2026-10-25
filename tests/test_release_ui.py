"""Manual release checks and reviewed switches; all update actions are fixtures."""
import asyncio
import contextlib
import copy
import io
import json
import unittest
from unittest.mock import patch


class ReleaseFixture:
    def __init__(self):
        self.calls = []
        self.pending = None
        self.fail = False
        self.installed = {'version':'2026.10.25.001', 'tag':'v2026.10.25.001', 'commit':'a'*40, 'data_schema':1}
        self.target = {'version':'2026.10.25.002', 'tag':'v2026.10.25.002', 'commit':'b'*40, 'data_schema':1,
                       'validated_platforms':['ubuntu26.04'], 'archive':{'name':'package.tar.gz', 'sha256':'f'*64, 'size':100}}

    def status(self):
        self.calls.append(('status',))
        return {'supported':True, 'managed':True, 'installed':copy.deepcopy(self.installed),
                'previous':{'version':'2026.10.24.001','commit':'c'*40}, 'pending':self.pending, 'detail':'Signed local installation.'}

    def check(self, tag=None):
        self.calls.append(('check',tag))
        return {'status':'UPDATE_AVAILABLE','current':self.installed,'release':self.target,
                'notes':'Public release notes', 'signature_verified':True, 'fingerprint':'check'}

    def plan(self, tag=None):
        self.calls.append(('plan',tag))
        return {'schema':1,'module':'update','operation':'apply','current':self.installed,
                'release':self.target,'notes':['Program files only; host settings are preserved.'],
                'signature_verified':True,'fingerprint':'apply','internal':'PRIVATE_UPDATE_FIXTURE_DETAIL'}

    def apply(self, plan):
        self.calls.append(('apply',copy.deepcopy(plan)))
        if self.fail:
            raise RuntimeError('PRIVATE_UPDATE_FIXTURE_DETAIL')
        return {'status':'UPDATED','version':'2026.10.25.002','restart_required':True}

    def rollback_plan(self):
        self.calls.append(('rollback_plan',))
        return {'schema':1,'module':'update','operation':'rollback','current':self.installed,
                'release':{'version':'2026.10.24.001','commit':'c'*40},'fingerprint':'rollback',
                'notes':['Program rollback does not restore host data.']}

    def rollback(self, plan):
        self.calls.append(('rollback',copy.deepcopy(plan)))
        return {'status':'UPDATE_ROLLED_BACK','version':'2026.10.24.001','restart_required':True}


class ReleaseCliTests(unittest.TestCase):
    def invoke(self, arguments, manager, root=True):
        from server_setting.__main__ import main
        output = io.StringIO()
        with patch('server_setting.__main__.update_management_available',return_value=root), contextlib.redirect_stdout(output):
            code = main(arguments, release_manager=manager)
        return code, json.loads(output.getvalue()), output.getvalue()

    def test_readonly_operations_are_explicit_and_do_not_apply_or_require_root(self):
        manager = ReleaseFixture()
        for mode, expected in [('status','status'),('check','check'),('plan','plan')]:
            code, result, text = self.invoke(['--update-'+mode],manager,root=False)
            self.assertEqual(code,0)
            self.assertNotIn('PRIVATE_UPDATE_FIXTURE_DETAIL',text)
            self.assertEqual(manager.calls[-1][0],expected)
        self.assertFalse(any(row[0] in {'apply','rollback'} for row in manager.calls))

    def test_mutations_require_root_and_confirmation_before_any_backend_call(self):
        for mode in ('apply','rollback'):
            manager = ReleaseFixture()
            code,result,_ = self.invoke(['--update-'+mode,'--confirm'],manager,root=False)
            self.assertEqual((code,result['code']),(1,'ROOT_REQUIRED')); self.assertEqual(manager.calls,[])
            code,result,_ = self.invoke(['--update-'+mode],manager)
            self.assertEqual((code,result['code']),(2,'CONFIRMATION_REQUIRED')); self.assertEqual(manager.calls,[])

    def test_tag_apply_and_rollback_use_their_own_full_reviewed_plans(self):
        manager = ReleaseFixture()
        code,result,text = self.invoke(['--update-apply','--release-tag','v2026.10.25.002','--confirm'],manager)
        self.assertEqual((code,result['status']),(0,'UPDATED'))
        self.assertEqual(manager.calls[0],('plan','v2026.10.25.002'))
        self.assertEqual(manager.calls[1][1]['internal'],'PRIVATE_UPDATE_FIXTURE_DETAIL')
        self.assertNotIn('PRIVATE_UPDATE_FIXTURE_DETAIL',text)
        code,result,_ = self.invoke(['--update-rollback','--confirm'],manager)
        self.assertEqual((code,result['status']),(0,'UPDATE_ROLLED_BACK'))
        self.assertEqual([row[0] for row in manager.calls],['plan','apply','rollback_plan','rollback'])

    def test_mode_and_target_errors_do_not_fetch_or_mutate(self):
        cases = [['--update-check','--text'],['--update-check','--root-install','php'],
                 ['--update-status','--release-tag','v2026.10.25.002'],['--update-rollback','--release-tag','v2026.10.25.001'],
                 ['--update-check','--values','{}'],['--update-check','--start'],
                 ['--update-plan','--startup-config','-'],['--release-tag','v2026.10.25.001'],
                 ['--update-check','--release-tag','../bad'],['--update-check','--release-tag','v2026.10.25.001\nextra'],
                 ['--update-check','--release-tag','v2026.02.30.001'], ['--update-plan','--release-tag','v2026.10.25.000']]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                manager = ReleaseFixture()
                code,result,_ = self.invoke(arguments,manager)
                self.assertEqual((code,result['code']),(2,'INVALID_ARGUMENT')); self.assertEqual(manager.calls,[])

    def test_backend_error_is_sanitized(self):
        manager = ReleaseFixture(); manager.fail = True
        code,result,text = self.invoke(['--update-apply','--confirm'],manager)
        self.assertEqual((code,result['code']),(1,'UPDATE_FAILED'))
        self.assertNotIn('PRIVATE_UPDATE_FIXTURE_DETAIL',text)


class ReleaseUiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)

    def application(self, manager, root=True, firewall=None, services=True):
        from server_setting.app import SettingApp
        from test_root_ui import UiManager
        from test_firewall_ui import FirewallFixture
        from test_banip_ui import BanIPFixture
        from test_packages_ui import PackageFixture
        from test_php_feather_ui import FeatherFixture
        from test_startup_ui import StartupFixture
        with patch('server_setting.app.update_management_available',return_value=root), \
                patch('server_setting.app.root_management_available',return_value=False):
            return SettingApp(root_manager=UiManager() if root and services else None, release_manager=manager,
                firewall_manager=firewall or FirewallFixture(),banip_manager=BanIPFixture(),
                package_manager=PackageFixture(),php_feather_manager=FeatherFixture(),startup_manager=StartupFixture())

    async def until(self,pilot,condition):
        for _ in range(150):
            await pilot.pause(.02)
            if condition(): return
        self.fail('Release UI did not settle')

    async def button(self,app,pilot,name):
        app.screen.query_one('#'+name).focus(); await pilot.press('enter'); await pilot.pause(.04)

    async def test_startup_is_local_only_and_check_is_explicit_in_readonly_ui(self):
        from textual.widgets import Button,TabbedContent,TextArea
        manager=ReleaseFixture(); app=self.application(manager,root=False)
        async with app.run_test(size=(80,24)) as pilot:
            await pilot.pause(.1)
            self.assertFalse(any(row[0] in {'check','plan','apply','rollback'} for row in manager.calls))
            app.query_one('#pages',TabbedContent).active='tab-update'
            await pilot.pause(.05)
            self.assertFalse(app.query('#update-apply'))
            self.assertFalse(app.query('#update-rollback'))
            await self.button(app,pilot,'update-check')
            await self.until(pilot,lambda:not app.root_busy)
            self.assertIn('Public release notes',app.query_one('#out-update',TextArea).text)
            self.assertEqual([row for row in manager.calls if row[0]=='check'],[('check',None)])

    async def test_apply_review_cancel_preserves_full_plan_and_confirm_exits_old_console(self):
        from textual.widgets import Input,TabbedContent,TextArea
        manager=ReleaseFixture(); app=self.application(manager)
        async with app.run_test(size=(100,30)) as pilot:
            await pilot.pause(.1)
            app.query_one('#pages',TabbedContent).active='tab-update'; await pilot.pause(.05)
            app.query_one('#release-tag',Input).value='v2026.10.25.002'
            await self.button(app,pilot,'update-apply')
            await self.until(pilot,lambda:type(app.screen).__name__=='RootReview')
            text=app.screen.query_one('#root-review-detail',TextArea).text
            self.assertIn('2026.10.25.002',text); self.assertIn('b'*40,text)
            self.assertNotIn('PRIVATE_UPDATE_FIXTURE_DETAIL',text)
            await pilot.press('escape'); await self.until(pilot,lambda:not app.root_busy)
            self.assertFalse(any(row[0]=='apply' for row in manager.calls))
            await self.button(app,pilot,'update-apply')
            await self.until(pilot,lambda:type(app.screen).__name__=='RootReview')
            await self.button(app,pilot,'root-confirm')
            await self.until(pilot,lambda:app.return_value is not None)
            self.assertEqual(app.return_value['status'],'UPDATED')
            self.assertTrue(app.return_value['restart_required'])
            self.assertEqual(next(row for row in manager.calls if row[0]=='apply')[1]['internal'],'PRIVATE_UPDATE_FIXTURE_DETAIL')

    async def test_foreign_pending_disables_switch_and_error_preserves_console(self):
        from textual.widgets import Button,TabbedContent,TextArea
        from test_firewall_ui import FirewallFixture
        firewall=FirewallFixture(); firewall.pending={'transaction_id':'foreign'}
        manager=ReleaseFixture(); app=self.application(manager,firewall=firewall)
        async with app.run_test(size=(100,30)) as pilot:
            await pilot.pause(.1)
            self.assertTrue(app.query_one('#update-apply',Button).disabled)
        manager=ReleaseFixture(); manager.fail=True; app=self.application(manager)
        async with app.run_test(size=(100,30)) as pilot:
            await pilot.pause(.1); app.query_one('#pages',TabbedContent).active='tab-update'; await pilot.pause(.05)
            await self.button(app,pilot,'update-apply'); await self.until(pilot,lambda:type(app.screen).__name__=='RootReview')
            await self.button(app,pilot,'root-confirm'); await self.until(pilot,lambda:not app.root_busy)
            self.assertIsNone(app.return_value)
            self.assertIn('UPDATE_FAILED',app.query_one('#out-update',TextArea).text)
            self.assertNotIn('PRIVATE_UPDATE_FIXTURE_DETAIL',app.query_one('#out-update',TextArea).text)

    async def test_own_recovery_works_when_service_management_platform_is_readonly(self):
        from textual.widgets import Button,TabbedContent,TextArea
        manager=ReleaseFixture()
        manager.pending={'transaction_id':'update-test','status':'UPDATE_SWITCHING','recoverable':True}
        app=self.application(manager,root=True,services=False)
        async with app.run_test(size=(80,24)) as pilot:
            await pilot.pause(.1)
            self.assertFalse(app.root_mode)
            app.query_one('#pages',TabbedContent).active='tab-update'; await pilot.pause(.05)
            self.assertTrue(app.query_one('#update-apply',Button).disabled)
            self.assertFalse(app.query_one('#update-rollback',Button).disabled)
            await self.button(app,pilot,'update-rollback')
            await self.until(pilot,lambda:type(app.screen).__name__=='RootReview')
            self.assertIn('2026.10.24.001',app.screen.query_one('#root-review-detail',TextArea).text)
            await self.button(app,pilot,'root-confirm')
            await self.until(pilot,lambda:app.return_value is not None)
            self.assertEqual(app.return_value['status'],'UPDATE_ROLLED_BACK')
            self.assertEqual([row[0] for row in manager.calls if row[0] in {'rollback_plan','rollback'}],['rollback_plan','rollback'])


if __name__=='__main__':
    unittest.main()

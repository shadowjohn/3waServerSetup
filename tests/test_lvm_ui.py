"""LVM UI/CLI use a manager boundary; no block device commands execute."""
import asyncio
import contextlib
import copy
import io
import json
import unittest
from unittest.mock import patch

from test_root_ui import UiManager
from test_firewall_ui import FirewallFixture

GIB = 1024**3


class LvmFixture:
    def __init__(self):
        self.calls = []
        self.current = {'supported': True, 'lv_path': '/dev/vg/root', 'lv_bytes': 10*GIB,
                        'vg_free_bytes': 8*GIB, 'vg_free_extents': 2048,
                        'filesystem_bytes': 10*GIB, 'needs_filesystem_growth': False}

    def status(self):
        return dict(self.current)

    def plan(self, mode='all', add_gib=None):
        grow = self.current['vg_free_bytes'] if mode == 'all' else add_gib*GIB
        return {'module': 'lvm', 'mode': mode, 'add_gib': add_gib, 'before': self.status(),
                'target_bytes': self.current['lv_bytes']+grow, 'grow_bytes': grow,
                'command': ['lvextend', '--yes', '--resizefs', '--extents', '4096', '/dev/vg/root'],
                'fingerprint': 'lvm-fixture', 'notes': ['Cannot be undone by configuration restore.']}

    def check(self, plan):
        self.calls.append(('check', copy.deepcopy(plan)))
        return {'status': 'LVM_TEST_PASSED', 'message': 'No growth or filesystem resizing performed.'}

    def apply(self, plan):
        self.calls.append(('apply', copy.deepcopy(plan)))
        self.current.update(lv_bytes=plan['target_bytes'], filesystem_bytes=plan['target_bytes'],
                            vg_free_bytes=self.current['vg_free_bytes']-plan['grow_bytes'])
        return {'status': 'LVM_APPLIED', 'after_bytes': plan['target_bytes']}

    def recover(self, identifier):
        self.calls.append(('recover', identifier))
        return {'status': 'LVM_APPLIED', 'transaction_id': identifier}


class LvmCliTests(unittest.TestCase):
    def invoke(self, arguments, manager, root=True):
        from server_setting.__main__ import main
        output = io.StringIO()
        with patch('server_setting.__main__.root_management_available', return_value=root), contextlib.redirect_stdout(output):
            code = main(arguments, lvm_manager=manager)
        return code, json.loads(output.getvalue())

    def test_root_and_confirmation_gates_precede_manager_mutation(self):
        for arguments in (['--lvm-status'], ['--lvm-plan'], ['--lvm-check', '--confirm'],
                          ['--lvm-apply', '--confirm'], ['--lvm-recover', 'tx', '--confirm']):
            manager = LvmFixture()
            code, result = self.invoke(arguments, manager, root=False)
            self.assertEqual(result['status'], 'DENIED')
            self.assertEqual(manager.calls, [])
        for arguments in (['--lvm-check'], ['--lvm-apply'], ['--lvm-recover', 'tx']):
            manager = LvmFixture()
            code, result = self.invoke(arguments, manager)
            self.assertEqual(result['code'], 'CONFIRMATION_REQUIRED')
            self.assertEqual(manager.calls, [])

    def test_all_and_increment_plan_check_apply_recovery_are_separate(self):
        manager = LvmFixture()
        code, result = self.invoke(['--lvm-plan', '--grow-mode', 'add', '--add-gib', '2'], manager)
        self.assertEqual(code, 0)
        self.assertEqual(result['plan']['grow_bytes'], 2*GIB)
        self.assertEqual(manager.calls, [])
        code, result = self.invoke(['--lvm-check', '--confirm'], manager)
        self.assertEqual(result['status'], 'LVM_TEST_PASSED')
        self.assertEqual(manager.current['lv_bytes'], 10*GIB)
        code, result = self.invoke(['--lvm-apply', '--grow-mode', 'add', '--add-gib', '2', '--confirm'], manager)
        self.assertEqual(result['status'], 'LVM_APPLIED')
        self.assertEqual(manager.current['lv_bytes'], 12*GIB)
        self.invoke(['--lvm-recover', 'tx', '--confirm'], manager)
        self.assertEqual([call[0] for call in manager.calls], ['check', 'apply', 'recover'])

    def test_invalid_or_orphan_growth_options_do_not_run(self):
        for arguments in (['--lvm-plan', '--grow-mode', 'add'], ['--lvm-plan', '--add-gib', '2'],
                          ['--lvm-plan', '--grow-mode', 'add', '--add-gib', '0'],
                          ['--lvm-status', '--grow-mode', 'add'], ['--status-json', '--grow-mode', 'all']):
            manager = LvmFixture()
            code, result = self.invoke(arguments, manager)
            self.assertEqual(code, 2)
            self.assertEqual(manager.calls, [])


class LvmUiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 2

    async def until(self, pilot, condition):
        for _ in range(150):
            await pilot.pause(.02)
            if condition():
                return
        self.fail('LVM UI did not settle')

    async def button(self, app, pilot, identifier):
        app.screen.query_one('#'+identifier).focus()
        await pilot.press('enter')
        await pilot.pause()

    async def test_storage_testmode_then_irreversible_grow_review_and_cancel(self):
        from server_setting.app import SettingApp, RootReview
        from textual.widgets import Input, Select, TabbedContent, TextArea
        manager = LvmFixture()
        app = SettingApp(root_manager=UiManager(), firewall_manager=FirewallFixture(), lvm_manager=manager)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.query_one('#pages', TabbedContent).active = 'tab-storage'
            await pilot.pause()
            app.query_one('#lvm-mode', Select).value = 'add'
            await pilot.pause()
            app.query_one('#lvm-add-gib', Input).value = '2'
            await self.button(app, pilot, 'lvm-check')
            await self.until(pilot, lambda: isinstance(app.screen, RootReview))
            detail = app.screen.query_one('#root-review-detail', TextArea).text
            self.assertIn('TEST MODE', detail)
            self.assertIn('without filesystem resizing', detail)
            self.assertIn('/dev/vg/root', detail)
            await self.button(app, pilot, 'root-confirm')
            await self.until(pilot, lambda: not app.root_busy)
            self.assertEqual([call[0] for call in manager.calls], ['check'])
            self.assertEqual(manager.current['lv_bytes'], 10*GIB)
            await self.button(app, pilot, 'lvm-apply')
            await self.until(pilot, lambda: isinstance(app.screen, RootReview))
            self.assertIn('IRREVERSIBLE', app.screen.query_one('#root-review-detail', TextArea).text)
            await pilot.press('escape')
            await self.until(pilot, lambda: not app.root_busy)
            self.assertEqual([call[0] for call in manager.calls], ['check'])

    async def test_confirmed_growth_refreshes_actual_lv_and_filesystem_sizes(self):
        from server_setting.app import SettingApp, RootReview
        from textual.widgets import TabbedContent, Static
        manager = LvmFixture()
        app = SettingApp(root_manager=UiManager(), firewall_manager=FirewallFixture(), lvm_manager=manager)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app.query_one('#pages', TabbedContent).active = 'tab-storage'
            await pilot.pause()
            await self.button(app, pilot, 'lvm-apply')
            await self.until(pilot, lambda: isinstance(app.screen, RootReview))
            await self.button(app, pilot, 'root-confirm')
            await self.until(pilot, lambda: not app.root_busy)
            self.assertEqual(app.lvm_state['lv_bytes'], 18*GIB)
            self.assertEqual(app.lvm_state['filesystem_bytes'], 18*GIB)
            self.assertIn(str(18*GIB), str(app.query_one('#lvm-state', Static).content))

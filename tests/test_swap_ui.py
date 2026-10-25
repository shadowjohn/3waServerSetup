"""Swap manager boundaries preserve the existing file until explicit apply."""
import asyncio
import contextlib
import copy
import io
import json
import unittest
from unittest.mock import patch
from test_root_ui import UiManager
from test_firewall_ui import FirewallFixture
from test_lvm_ui import LvmFixture


class SwapFixture:
    def __init__(self):
        self.files = {'/swap.img': {'schema': 1, 'path': '/swap.img', 'size_mib': 3813, 'active': True, 'persistent': True}}
        self.calls = []
        self.saved = None

    def status(self, path=None):
        path = path or '/swap.img'
        return {'supported': True, 'recommended_size_mib': 16384, 'detail': 'Fixture ext4',
                **self.files.get(path, {'path': path, 'size_mib': 0, 'active': False, 'persistent': False})}

    def load_saved(self):
        return copy.deepcopy(self.saved)

    def save(self, config):
        self.saved = copy.deepcopy(config)
        self.calls.append(('save', copy.deepcopy(config)))
        return {'status': 'SAVED', 'module': 'swap'}

    def plan(self, config):
        return {'schema': 1, 'module': 'swap', 'config': copy.deepcopy(config),
                'observations': self.status(config['path']), 'notes': ['Memory and disk are checked before swapoff.'],
                'fingerprint': 'swap-fixture'}

    def apply(self, plan):
        config = copy.deepcopy(plan['config'])
        self.files[config['path']] = config
        self.calls.append(('apply', config))
        return {'status': 'SWAP_APPLIED', 'transaction_id': '20261025T010203Z-0123456789ab', 'verification': config}

    def recover(self, identifier):
        self.calls.append(('recover', identifier))
        return {'status': 'SWAP_ROLLED_BACK', 'transaction_id': identifier}


class SwapCliTests(unittest.TestCase):
    def invoke(self, arguments, manager, root=True):
        from server_setting.__main__ import main
        output = io.StringIO()
        with patch('server_setting.__main__.root_management_available', return_value=root), contextlib.redirect_stdout(output):
            code = main(arguments, swap_manager=manager)
        return code, json.loads(output.getvalue())

    def targets(self):
        return ['--swap-path', '/swapfile', '--size-mib', '32', '--swap-active', 'on', '--swap-persistent', 'off']

    def test_role_confirmation_and_complete_targets_precede_mutation(self):
        for arguments in (['--swap-status'], ['--swap-plan', *self.targets()],
                          ['--swap-apply', *self.targets(), '--confirm'], ['--swap-recover', 'tx', '--confirm']):
            manager = SwapFixture()
            code, result = self.invoke(arguments, manager, root=False)
            self.assertEqual(result['status'], 'DENIED')
            self.assertEqual(manager.calls, [])
        for arguments in (['--swap-save', *self.targets()], ['--swap-apply', *self.targets()], ['--swap-recover', 'tx']):
            manager = SwapFixture()
            code, result = self.invoke(arguments, manager)
            self.assertEqual(result['code'], 'CONFIRMATION_REQUIRED')
            self.assertEqual(manager.calls, [])
        for arguments in (['--swap-plan'], ['--swap-status', '--size-mib', '32'],
                          ['--swap-plan', *self.targets(), '--config', '-'], ['--status-json', '--swap-active', 'off']):
            code, result = self.invoke(arguments, SwapFixture())
            self.assertEqual(code, 2)

    def test_status_plan_save_apply_and_recovery_are_separate(self):
        manager = SwapFixture()
        code, result = self.invoke(['--swap-status'], manager)
        self.assertEqual(result['size_mib'], 3813)
        self.assertEqual(manager.calls, [])
        code, result = self.invoke(['--swap-plan', *self.targets()], manager)
        self.assertIs(result['plan']['config']['active'], True)
        self.assertIs(result['plan']['config']['persistent'], False)
        self.assertEqual(manager.calls, [])
        self.invoke(['--swap-save', *self.targets(), '--confirm'], manager)
        self.invoke(['--swap-apply', *self.targets(), '--confirm'], manager)
        self.invoke(['--swap-recover', 'tx', '--confirm'], manager)
        self.assertEqual([call[0] for call in manager.calls], ['save', 'apply', 'recover'])
        self.assertEqual(manager.files['/swap.img']['size_mib'], 3813)


class SwapUiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 2

    async def until(self, pilot, condition):
        for _ in range(150):
            await pilot.pause(.02)
            if condition():
                return
        self.fail('Swap UI did not settle')

    async def button(self, app, pilot, identifier):
        app.screen.query_one('#'+identifier).focus()
        await pilot.press('enter')
        await pilot.pause()

    async def test_existing_size_is_preserved_and_recommended_save_never_applies(self):
        from server_setting.app import SettingApp
        from textual.widgets import Input, Select, TabbedContent
        manager = SwapFixture()
        app = SettingApp(root_manager=UiManager(), firewall_manager=FirewallFixture(), lvm_manager=LvmFixture(), swap_manager=manager)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.query_one('#pages', TabbedContent).active = 'tab-storage'
            await pilot.pause()
            self.assertEqual(app.query_one('#swap-path', Select).value, '/swap.img')
            self.assertEqual(app.query_one('#swap-size-mib', Input).value, '3813')
            self.assertEqual(manager.calls, [])
            await self.button(app, pilot, 'swap-recommended')
            self.assertEqual(app.query_one('#swap-size-mib', Input).value, '16384')
            self.assertEqual(manager.calls, [])
            await self.button(app, pilot, 'swap-save')
            await self.until(pilot, lambda: not app.root_busy)
            self.assertEqual(manager.files['/swap.img']['size_mib'], 3813)
            self.assertEqual(manager.saved['size_mib'], 16384)
            await self.button(app, pilot, 'swap-load')
            await self.until(pilot, lambda: not app.root_busy)
            self.assertEqual(app.query_one('#swap-size-mib', Input).value, '16384')

    async def test_other_path_review_cancel_apply_and_recovery_do_not_chain_lvm(self):
        from server_setting.app import SettingApp, RootReview
        from textual.widgets import Input, Select, Switch, TabbedContent, TextArea
        manager, lvm = SwapFixture(), LvmFixture()
        app = SettingApp(root_manager=UiManager(), firewall_manager=FirewallFixture(), lvm_manager=lvm, swap_manager=manager)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.query_one('#pages', TabbedContent).active = 'tab-storage'
            await pilot.pause()
            app.query_one('#swap-path', Select).value = '/swapfile'
            await self.until(pilot, lambda: app.swap_state.get('path') == '/swapfile' and not app.root_busy)
            app.query_one('#swap-size-mib', Input).value = '32'
            app.query_one('#swap-active', Switch).value = True
            app.query_one('#swap-persistent', Switch).value = False
            await self.button(app, pilot, 'swap-apply')
            await self.until(pilot, lambda: isinstance(app.screen, RootReview))
            detail = app.screen.query_one('#root-review-detail', TextArea).text
            self.assertIn('/swapfile', detail)
            self.assertIn('LVM', detail)
            self.assertIn('persistent', detail)
            await pilot.press('escape')
            await self.until(pilot, lambda: not app.root_busy)
            self.assertEqual(manager.calls, [])
            await self.button(app, pilot, 'swap-apply')
            await self.until(pilot, lambda: isinstance(app.screen, RootReview))
            await self.button(app, pilot, 'root-confirm')
            await self.until(pilot, lambda: not app.root_busy)
            self.assertEqual(app.swap_state['size_mib'], 32)
            self.assertEqual(manager.files['/swap.img']['size_mib'], 3813)
            app.query_one('#swap-recovery-id', Input).value = '20261025T010203Z-0123456789ab'
            await self.button(app, pilot, 'swap-recover')
            await self.until(pilot, lambda: isinstance(app.screen, RootReview))
            await self.button(app, pilot, 'root-confirm')
            await self.until(pilot, lambda: not app.root_busy)
            self.assertEqual([call[0] for call in manager.calls], ['apply', 'recover'])
            self.assertEqual(lvm.calls, [])

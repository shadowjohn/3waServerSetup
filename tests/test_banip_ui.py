"""BanIP review and CLI boundaries; fixtures never download or change a firewall."""
import asyncio
import contextlib
import copy
import io
import json
import threading
import unittest
from unittest.mock import patch

from test_firewall_ui import FirewallFixture
from test_root_ui import UiManager


SOURCE = 'https://3wa.tw/myapache_banip/api.php?mode=download_script&type=linux'
SCRIPT_SHA = '2fc1b6592fd881a13bb34236ed8408ffc2a60e4e610a2c355ad9789aaa89dd99'
MANAGED_SHA = '4014fa33d31b7bd6de531b105b441a923a3355bea61e9d1699a69e7bc58ee1e7'


class BanIPFixture:
    def __init__(self):
        self.calls = []
        self.pending = None
        self.block = None
        self.started = threading.Event()

    def status(self):
        return {'supported': True, 'installed': False, 'packages_missing': ['ipset', 'curl'],
                'active': bool(self.pending), 'count': 0, 'set_sha256': None,
                'source_sha256': SCRIPT_SHA, 'script_sha256': MANAGED_SHA,
                'persistent': False, 'pending': self.pending, 'detail': 'Fixture'}

    def plan(self):
        self.calls.append(('plan',))
        return {'schema': 1, 'module': 'banip', 'packages': ['ipset', 'curl'],
                'rescue': {'port': 2222, 'sources': ['192.0.2.99/32']}, 'origin': {'verified': True},
                'before': {}, 'source_sha256': SCRIPT_SHA, 'script_sha256': MANAGED_SHA, 'source_url': SOURCE,
                'data_url': 'https://3wa.tw/myapache_banip/data/banip_csv.txt',
                'timeout_seconds': 120, 'fingerprint': 'banip-reviewed', 'notes': ['Run once only.']}

    def update(self, plan):
        self.started.set()
        if self.block is not None:
            self.block.wait(5)
        self.calls.append(('update', copy.deepcopy(plan)))
        self.pending = {'status': 'AWAITING_NEW_SSH', 'transaction_id': 'banip-test',
                        'deadline_boottime': 100000, 'remaining_seconds': 120}
        return {'module': 'banip', **self.pending,
                'confirmation_command': '/opt/3waServerSetting/run.sh --banip-confirm banip-test --confirm'}

    def confirm(self, identifier):
        self.calls.append(('confirm', identifier))
        return {'status': 'BANIP_CONFIRMED', 'transaction_id': identifier}

    def rollback(self, identifier):
        self.calls.append(('rollback', identifier))
        return {'status': 'BANIP_ROLLED_BACK', 'transaction_id': identifier}


class BanIPCliTests(unittest.TestCase):
    def invoke(self, arguments, manager, root=True):
        from server_setting.__main__ import main
        output = io.StringIO()
        with patch('server_setting.__main__.root_management_available', return_value=root), contextlib.redirect_stdout(output):
            code = main(arguments, banip_manager=manager)
        return code, json.loads(output.getvalue())

    def test_role_and_confirmation_are_checked_before_download_or_mutation(self):
        for mode in (['--banip-status'], ['--banip-plan'], ['--banip-update', '--confirm'],
                     ['--banip-confirm', 'id', '--confirm'], ['--banip-rollback', 'id', '--confirm']):
            manager = BanIPFixture()
            code, result = self.invoke(mode, manager, root=False)
            self.assertEqual((code, result['code']), (1, 'ROOT_REQUIRED'))
            self.assertEqual(manager.calls, [])
        for mode in (['--banip-update'], ['--banip-confirm', 'id'], ['--banip-rollback', 'id']):
            manager = BanIPFixture()
            code, result = self.invoke(mode, manager)
            self.assertEqual((code, result['code']), (2, 'CONFIRMATION_REQUIRED'))
            self.assertEqual(manager.calls, [])

    def test_fixed_plan_update_and_external_confirm_are_separate_json_operations(self):
        manager = BanIPFixture()
        code, status = self.invoke(['--banip-status'], manager)
        self.assertEqual((code, status['persistent']), (0, False))
        code, result = self.invoke(['--banip-plan'], manager)
        self.assertEqual((code, result['plan']['source_url']), (0, SOURCE))
        self.assertEqual([call[0] for call in manager.calls], ['plan'])
        code, result = self.invoke(['--banip-update', '--confirm'], manager)
        self.assertEqual((code, result['status']), (0, 'AWAITING_NEW_SSH'))
        self.assertEqual([call[0] for call in manager.calls], ['plan', 'plan', 'update'])
        code, result = self.invoke(['--banip-confirm', 'banip-test', '--confirm'], manager)
        self.assertEqual((code, result['status']), (0, 'BANIP_CONFIRMED'))
        code, result = self.invoke(['--banip-rollback', 'banip-test', '--confirm'], manager)
        self.assertEqual((code, result['status']), (0, 'BANIP_ROLLED_BACK'))

    def test_unrelated_modes_targets_and_arbitrary_sources_are_rejected(self):
        for arguments in (['--banip-plan', '--firewall-status'], ['--banip-status', '--text'],
                          ['--banip-plan', '--config', 'other.json'], ['--banip-plan', '--values', '{}'],
                          ['--banip-plan', '--start'], ['--banip-plan', '--remote', 'on'],
                          ['--banip-plan', '--swap-path', '/swapfile'], ['--banip-plan', '--grow-mode', 'all'],
                          ['--banip-plan', '--url', 'https://example.invalid'], ['--banip-plan', '--script', 'other.sh']):
            manager = BanIPFixture()
            code, result = self.invoke(arguments, manager)
            self.assertEqual((code, result['code']), (2, 'INVALID_ARGUMENT'))
            self.assertEqual(manager.calls, [])

    def test_backend_new_ssh_rejection_is_not_reported_as_confirmation(self):
        from server_setting.root_actions import RootActionError
        manager = BanIPFixture()
        with patch.object(manager, 'confirm', side_effect=RootActionError('A new SSH connection is required.', 'NEW_SSH_REQUIRED')):
            code, result = self.invoke(['--banip-confirm', 'banip-test', '--confirm'], manager)
        self.assertEqual((code, result['code']), (1, 'NEW_SSH_REQUIRED'))
        self.assertEqual(manager.calls, [])


class BanIPUiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Match the production loop; Windows debug stack collection dominates large forms.
        asyncio.get_running_loop().set_debug(False)

    async def until(self, pilot, condition):
        for _ in range(150):
            await pilot.pause(.02)
            if condition():
                return
        self.fail('BanIP UI did not settle')

    async def button(self, app, pilot, identifier):
        app.screen.query_one('#' + identifier).focus()
        await pilot.press('enter')
        await pilot.pause(.05)

    async def test_narrow_review_discloses_fixed_scope_and_cancel_never_downloads(self):
        from server_setting.app import SettingApp, RootReview
        from textual.widgets import TabbedContent, TextArea
        manager = BanIPFixture()
        app = SettingApp(root_manager=UiManager(), firewall_manager=FirewallFixture(), banip_manager=manager)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(.05)
            self.assertEqual(manager.calls, [])
            app.query_one('#pages', TabbedContent).active = 'tab-firewall'
            await pilot.pause(.05)
            await self.button(app, pilot, 'banip-update')
            await self.until(pilot, lambda: isinstance(app.screen, RootReview))
            detail = app.screen.query_one('#root-review-detail', TextArea).text
            for expected in (SOURCE, SCRIPT_SHA, MANAGED_SHA, 'ipset', 'curl', 'blacklist', 'DROP', '2222',
                             '192.0.2.99/32', 'IPv4 only', 'IPv6', 'cron', 'boot', 'new SSH'):
                self.assertIn(expected, detail)
            self.assertTrue(app.root_busy)
            await pilot.press('escape')
            await self.until(pilot, lambda: not app.root_busy)
            self.assertEqual([call[0] for call in manager.calls], ['plan'])
            self.assertIn('CANCELLED', app.query_one('#out-banip', TextArea).text)
            await self.button(app, pilot, 'banip-status')
            await self.until(pilot, lambda: not app.root_busy)
            self.assertEqual([call[0] for call in manager.calls], ['plan'])

    async def test_confirmed_update_is_busy_then_awaits_external_ssh_without_ui_confirmation(self):
        from server_setting.app import SettingApp, RootReview
        from textual.widgets import Button, Static, TabbedContent, TextArea
        manager = BanIPFixture()
        manager.block = threading.Event()
        app = SettingApp(root_manager=UiManager(), firewall_manager=FirewallFixture(), banip_manager=manager)
        try:
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause(.05)
                app.query_one('#pages', TabbedContent).active = 'tab-firewall'
                await pilot.pause(.05)
                await self.button(app, pilot, 'banip-update')
                await self.until(pilot, lambda: isinstance(app.screen, RootReview))
                await self.button(app, pilot, 'root-confirm')
                await self.until(pilot, manager.started.is_set)
                await pilot.press('ctrl+q')
                self.assertTrue(app.is_running)
                self.assertTrue(app.root_busy)
                manager.block.set()
                await self.until(pilot, lambda: not app.root_busy)
                self.assertIn('AWAITING_NEW_SSH', app.query_one('#out-banip', TextArea).text)
                live = str(app.query_one('#banip-live', Static).render())
                self.assertIn('--banip-confirm banip-test --confirm', live)
                self.assertIn('remaining', live)
                for identifier in ('banip-update', 'fw-apply', 'swap-apply', 'lvm-apply', 'root-apply-apache'):
                    self.assertTrue(app.query_one('#' + identifier, Button).disabled)
                self.assertFalse(app.query_one('#banip-status', Button).disabled)
                self.assertFalse(any('banip-confirm' in (button.id or '') for button in app.query(Button)))
                self.assertEqual([call[0] for call in manager.calls], ['plan', 'update'])
                await pilot.press('ctrl+q')
                self.assertFalse(any(call[0] in {'confirm', 'rollback'} for call in manager.calls))
        finally:
            manager.block.set()

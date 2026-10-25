"""Custom SQLite build/activation is separate from distro package installation."""
import asyncio
import contextlib
import copy
import io
import json
import unittest
from unittest.mock import patch

from test_banip_ui import BanIPFixture
from test_firewall_ui import FirewallFixture
from test_packages_ui import PackageFixture
from test_root_ui import UiManager


class FeatherFixture:
    def __init__(self):
        self.calls = []
        self.cli_enabled = False
        self.fpm_enabled = False

    def status(self):
        return {'supported': True, 'detail': 'Exact native PHP ABI; distro SQLite alone is not FeatherMountain.',
                'php_version': '8.5.4', 'package_version': '8.5.4-1ubuntu2', 'abi': '20250925',
                'cli_enabled': self.cli_enabled, 'fpm_enabled': self.fpm_enabled,
                'stock_available': True, 'artifact': None, 'pending': None}

    def plan(self, cli=True, fpm=True):
        self.calls.append(('plan', cli, fpm))
        return {'schema': 1, 'module': 'php-feather', 'cli': cli, 'fpm': fpm,
                'build_plan': {'source': {'package': 'php8.5', 'version': '8.5.4-1ubuntu2', 'authenticated': True},
                               'abi': '20250925', 'packages': ['build-essential', 'php8.5-dev'],
                               'compile': ['phpize', 'configure', 'make'], 'probe': 'SQL.so function'},
                'before': {'aliases': {}, 'service': {}, 'artifact': None},
                'notes': ['Verify SQL load_extension capability; do not overwrite the system SQLite module.'],
                'fingerprint': 'feather-reviewed'}

    def apply(self, plan):
        self.calls.append(('apply', copy.deepcopy(plan)))
        if plan['cli']:
            self.cli_enabled = True
        if plan['fpm']:
            self.fpm_enabled = True
        return {'status': 'FEATHER_APPLIED', 'module': 'php-feather', 'transaction_id': 'feather-test',
                'verification': {'cli': plan['cli'], 'fpm': plan['fpm'], 'sql_load_extension': True}}

    def restore_plan(self):
        self.calls.append(('restore_plan',))
        return {'schema': 1, 'module': 'php-feather', 'transaction_id': 'feather-test',
                'cli': self.cli_enabled, 'fpm': self.fpm_enabled, 'aliases': {},
                'notes': ['Restore recorded stock activation.'],
                'fingerprint': 'feather-restore-reviewed'}

    def restore(self, plan):
        self.calls.append(('restore', copy.deepcopy(plan)))
        self.cli_enabled = self.fpm_enabled = False
        return {'status': 'FEATHER_RESTORED', 'module': 'php-feather', 'transaction_id': plan['transaction_id']}


class FeatherCliTests(unittest.TestCase):
    def invoke(self, arguments, manager, root=True):
        from server_setting.__main__ import main
        output = io.StringIO()
        with patch('server_setting.__main__.root_management_available', return_value=root), contextlib.redirect_stdout(output):
            code = main(arguments, php_feather_manager=manager)
        return code, json.loads(output.getvalue())

    def test_role_and_confirmation_precede_build_and_restore(self):
        for arguments in (['--php-feather-status'], ['--php-feather-plan'],
                          ['--php-feather-apply', '--confirm'], ['--php-feather-restore', '--confirm']):
            manager = FeatherFixture()
            code, result = self.invoke(arguments, manager, root=False)
            self.assertEqual((code, result['code']), (1, 'ROOT_REQUIRED'))
            self.assertEqual(manager.calls, [])
        for arguments in (['--php-feather-apply'], ['--php-feather-restore']):
            manager = FeatherFixture()
            code, result = self.invoke(arguments, manager)
            self.assertEqual((code, result['code']), (2, 'CONFIRMATION_REQUIRED'))
            self.assertEqual(manager.calls, [])

    def test_explicit_scopes_plan_apply_and_reviewed_restore(self):
        manager = FeatherFixture()
        code, result = self.invoke(['--php-feather-status'], manager)
        self.assertEqual((code, result['cli_enabled']), (0, False))
        code, result = self.invoke(['--php-feather-plan'], manager)
        self.assertEqual((code, result['plan']['cli'], result['plan']['fpm']), (0, True, True))
        self.assertEqual(manager.calls, [('plan', True, True)])
        code, result = self.invoke(['--php-feather-apply', '--feather-cli', 'on', '--feather-fpm', 'off', '--confirm'], manager)
        self.assertEqual((code, result['status']), (0, 'FEATHER_APPLIED'))
        self.assertTrue(manager.cli_enabled)
        self.assertFalse(manager.fpm_enabled)
        code, result = self.invoke(['--php-feather-restore', '--confirm'], manager)
        self.assertEqual((code, result['status']), (0, 'FEATHER_RESTORED'))
        self.assertEqual([call[0] for call in manager.calls], ['plan', 'plan', 'apply', 'restore_plan', 'restore'])
        self.assertEqual(manager.calls[-1][1]['fingerprint'], 'feather-restore-reviewed')

    def test_modes_scopes_and_arbitrary_build_inputs_are_rejected(self):
        for arguments in (['--php-feather-plan', '--feather-cli', 'off', '--feather-fpm', 'off'],
                          ['--php-feather-status', '--feather-cli', 'on'],
                          ['--php-feather-restore', '--feather-fpm', 'on', '--confirm'],
                          ['--packages-catalog', '--feather-cli', 'on'],
                          ['--php-feather-plan', '--packages-catalog'], ['--php-feather-plan', '--text'],
                          ['--php-feather-plan', '--values', '{}'], ['--php-feather-plan', '--start'],
                          ['--php-feather-plan', '--config', 'other.json'],
                          ['--php-feather-plan', '--source-url', 'https://example.invalid'],
                          ['--php-feather-plan', '--module-path', '/tmp/module.so']):
            manager = FeatherFixture()
            code, result = self.invoke(arguments, manager)
            self.assertEqual((code, result['code']), (2, 'INVALID_ARGUMENT'))
            self.assertEqual(manager.calls, [])


class FeatherUiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)

    async def until(self, pilot, condition):
        for _ in range(150):
            await pilot.pause(.02)
            if condition():
                return
        self.fail('Feather SQLite UI did not settle')

    async def button(self, app, pilot, identifier):
        app.screen.query_one('#' + identifier).focus()
        await pilot.press('enter')
        await pilot.pause(.05)

    async def test_narrow_build_review_cancel_scopes_apply_and_stock_restore(self):
        from server_setting.app import RootReview, SettingApp
        from textual.widgets import Button, Switch, TabbedContent, TextArea
        manager = FeatherFixture()
        packages = PackageFixture()
        app = SettingApp(root_manager=UiManager(), firewall_manager=FirewallFixture(), banip_manager=BanIPFixture(),
                         package_manager=packages, php_feather_manager=manager)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(.05)
            app.query_one('#pages', TabbedContent).active = 'tab-packages'
            await pilot.pause(.05)
            app.query_one('#packages-refresh', Button).focus()
            await pilot.press('ctrl+right')
            self.assertEqual(app.query_one('#pages', TabbedContent).active, 'tab-network')
            await pilot.press('ctrl+right')
            self.assertEqual(app.query_one('#pages', TabbedContent).active, 'tab-firewall')
            await pilot.press('ctrl+left')
            self.assertEqual(app.query_one('#pages', TabbedContent).active, 'tab-network')
            await pilot.press('ctrl+left')
            self.assertEqual(app.query_one('#pages', TabbedContent).active, 'tab-packages')
            await pilot.pause(.05)
            self.assertEqual(manager.calls, [])
            self.assertTrue(app.query_one('#feather-cli', Switch).value)
            self.assertTrue(app.query_one('#feather-fpm', Switch).value)
            await self.button(app, pilot, 'feather-apply')
            await self.until(pilot, lambda: isinstance(app.screen, RootReview))
            detail = app.screen.query_one('#root-review-detail', TextArea).text
            for expected in ('CLI: on', 'FPM: on', 'php8.5', 'build-essential', 'phpize', 'make',
                             '20250925', 'SQL.so', 'load_extension', 'reload'):
                self.assertIn(expected, detail)
            await pilot.press('escape')
            await self.until(pilot, lambda: not app.root_busy)
            self.assertEqual(manager.calls, [('plan', True, True)])
            self.assertEqual(packages.calls, [])
            app.query_one('#feather-fpm', Switch).value = False
            await self.button(app, pilot, 'feather-apply')
            await self.until(pilot, lambda: isinstance(app.screen, RootReview))
            self.assertIn('FPM: off', app.screen.query_one('#root-review-detail', TextArea).text)
            self.assertIn('Unselected scopes are preserved', app.screen.query_one('#root-review-detail', TextArea).text)
            await self.button(app, pilot, 'root-confirm')
            await self.until(pilot, lambda: not app.root_busy)
            self.assertIn('FEATHER_APPLIED', app.query_one('#out-php-feather', TextArea).text)
            self.assertTrue(app.php_feather_state['cli_enabled'])
            self.assertFalse(app.php_feather_state['fpm_enabled'])
            await self.button(app, pilot, 'feather-restore')
            await self.until(pilot, lambda: isinstance(app.screen, RootReview))
            self.assertIn('stock', app.screen.query_one('#root-review-detail', TextArea).text.lower())
            self.assertIn('first stock aliases', app.screen.query_one('#root-review-detail', TextArea).text)
            self.assertIn('all currently managed', app.screen.query_one('#root-review-detail', TextArea).text)
            await pilot.press('escape')
            await self.until(pilot, lambda: not app.root_busy)
            self.assertFalse(any(call[0] == 'restore' for call in manager.calls))
            await self.button(app, pilot, 'feather-restore')
            await self.until(pilot, lambda: isinstance(app.screen, RootReview))
            await self.button(app, pilot, 'root-confirm')
            await self.until(pilot, lambda: not app.root_busy)
            self.assertIn('FEATHER_RESTORED', app.query_one('#out-php-feather', TextArea).text)
            self.assertFalse(app.php_feather_state['cli_enabled'])

    async def test_network_pending_blocks_build_and_restore_but_allows_status(self):
        from server_setting.app import SettingApp
        from textual.widgets import Button, TabbedContent
        manager = FeatherFixture()
        firewall = FirewallFixture()
        firewall.pending = {'transaction_id': 'fw-pending', 'remaining_seconds': 120, 'deadline_boottime': 100000}
        app = SettingApp(root_manager=UiManager(), firewall_manager=firewall, banip_manager=BanIPFixture(),
                         package_manager=PackageFixture(), php_feather_manager=manager)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause(.05)
            app.query_one('#pages', TabbedContent).active = 'tab-packages'
            await pilot.pause(.05)
            for identifier in ('feather-apply', 'feather-restore'):
                self.assertTrue(app.query_one('#' + identifier, Button).disabled)
            self.assertFalse(app.query_one('#feather-status', Button).disabled)
            await self.button(app, pilot, 'feather-status')
            await self.until(pilot, lambda: not app.root_busy)
            self.assertEqual(manager.calls, [])

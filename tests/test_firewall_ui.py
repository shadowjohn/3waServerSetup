"""Root firewall UI/CLI boundaries; no native firewall commands execute."""
import asyncio
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_root_ui import UiManager


class FirewallFixture:
    def __init__(self):
        self.calls = []
        self.saved = None
        self.pending = None

    def status(self):
        return {'supported': True, 'active': bool(self.pending), 'backend': 'nftables', 'detail': 'Fixture',
                'owned_table': bool(self.pending), 'persistent': False, 'pending': self.pending,
                'saved': self.saved is not None, 'initial_restore_available': False}

    def load_saved(self):
        return copy.deepcopy(self.saved)

    def save(self, config):
        self.saved = copy.deepcopy(config)
        self.calls.append(('save', copy.deepcopy(config)))
        return {'status': 'SAVED', 'module': 'firewall'}

    def plan(self, config):
        from server_setting.firewall_rules import normalize
        return {'schema': 1, 'module': 'firewall', 'config': normalize(config),
                'rescue': {'port': 2222, 'sources': ['192.0.2.99/32']}, 'origin': {'verified': True},
                'script': 'fixture effective inet rules', 'fingerprint': 'fixture', 'timeout_seconds': 120,
                'notes': ['SSH rescue is independent of service allow rules.']}

    def apply(self, plan):
        self.calls.append(('apply', copy.deepcopy(plan)))
        self.pending = {'status': 'AWAITING_NEW_SSH', 'transaction_id': 'fw-test',
                        'deadline_boottime': 100000, 'remaining_seconds': 120}
        return {'module': 'firewall', **self.pending,
                'confirmation_command': '/opt/3waServerSetting/run.sh --firewall-confirm fw-test --confirm'}

    def confirm(self, identifier):
        self.calls.append(('confirm', identifier))
        return {'status': 'CONFIRMED', 'transaction_id': identifier}

    def rollback(self, identifier):
        self.calls.append(('rollback', identifier))
        return {'status': 'ROLLED_BACK', 'transaction_id': identifier}


class FirewallCliTests(unittest.TestCase):
    def invoke(self, arguments, manager, root=True):
        from server_setting.__main__ import main
        output = io.StringIO()
        with patch('server_setting.__main__.root_management_available', return_value=root), contextlib.redirect_stdout(output):
            code = main(arguments, firewall_manager=manager)
        return code, json.loads(output.getvalue())

    def config_file(self, directory):
        config = {'schema': 2, 'services': [{'id': 'mariadb', 'enabled': True, 'protocol': 'tcp',
                                          'ports': [3306], 'sources': ['192.0.2.50/32', '2001:db8::50/128']}]}
        path = Path(directory) / '防火牆 recipe.json'
        path.write_text(json.dumps(config), encoding='utf-8')
        path.chmod(0o600)
        return path, config

    def test_nonroot_and_missing_confirmation_never_mutate(self):
        for arguments in (['--firewall-status'], ['--firewall-plan', '--config', '-'],
                          ['--firewall-save', '--config', '-', '--confirm'],
                          ['--firewall-apply', '--config', '-', '--confirm'],
                          ['--firewall-confirm', 'fw-test', '--confirm'], ['--firewall-rollback', 'fw-test', '--confirm']):
            manager = FirewallFixture()
            code, result = self.invoke(arguments, manager, root=False)
            self.assertNotEqual(code, 0)
            self.assertEqual(result['status'], 'DENIED')
            self.assertEqual(manager.calls, [])
        for arguments in (['--firewall-save', '--config', '-'], ['--firewall-apply', '--config', '-'],
                          ['--firewall-confirm', 'fw-test'], ['--firewall-rollback', 'fw-test']):
            manager = FirewallFixture()
            code, result = self.invoke(arguments, manager)
            self.assertEqual(result['code'], 'CONFIRMATION_REQUIRED')
            self.assertEqual(manager.calls, [])

    def test_config_save_plan_apply_pending_and_external_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            path, config = self.config_file(directory)
            manager = FirewallFixture()
            code, result = self.invoke(['--firewall-plan', '--config', str(path)], manager)
            self.assertEqual(code, 0)
            self.assertEqual(result['plan']['rescue']['port'], 2222)
            self.assertEqual(manager.calls, [])
            code, result = self.invoke(['--firewall-save', '--config', str(path), '--confirm'], manager)
            self.assertEqual(result['status'], 'SAVED')
            self.assertEqual(manager.saved, config)
            code, result = self.invoke(['--firewall-apply', '--config', str(path), '--confirm'], manager)
            self.assertEqual(result['status'], 'AWAITING_NEW_SSH')
            self.assertIn('--firewall-confirm fw-test', result['confirmation_command'])
            self.assertFalse(any(call[0] == 'confirm' for call in manager.calls))
            code, result = self.invoke(['--firewall-confirm', 'fw-test', '--confirm'], manager)
            self.assertEqual(result['status'], 'CONFIRMED')
            code, result = self.invoke(['--firewall-rollback', 'fw-test', '--confirm'], manager)
            self.assertEqual(result['status'], 'ROLLED_BACK')

    def test_bad_config_is_bounded_and_unrelated_options_are_rejected(self):
        for value in ('{"schema":2,"schema":2,"services":[]}', 'x' * (1024 * 1024 + 1)):
            manager = FirewallFixture()
            with patch('sys.stdin', io.StringIO(value)):
                code, result = self.invoke(['--firewall-apply', '--config', '-', '--confirm'], manager)
            self.assertNotEqual(code, 0)
            self.assertEqual(manager.calls, [])
        for arguments in (['--firewall-status', '--values', '{}'], ['--firewall-apply'],
                          ['--firewall-status', '--config', 'unused'], ['--status-json', '--config', 'unused']):
            code, result = self.invoke(arguments, FirewallFixture())
            self.assertEqual(code, 2)

    def test_confirmation_failure_from_backend_is_not_overridden(self):
        from server_setting.root_actions import RootActionError
        manager = FirewallFixture()
        with patch.object(manager, 'confirm', side_effect=RootActionError('A new verified SSH connection is required.', 'NEW_SSH_REQUIRED')):
            code, result = self.invoke(['--firewall-confirm', 'fw-test', '--confirm'], manager)
        self.assertNotEqual(code, 0)
        self.assertEqual(result['code'], 'NEW_SSH_REQUIRED')
        self.assertEqual(manager.calls, [])


class FirewallUiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 2

    async def until(self, pilot, condition):
        for _ in range(150):
            await pilot.pause(.02)
            if condition():
                return
        self.fail('Firewall UI did not settle')

    async def button(self, app, pilot, identifier):
        app.screen.query_one('#' + identifier).focus()
        await pilot.press('enter')
        await pilot.pause()

    async def test_per_service_edit_save_load_and_disabled_recipe(self):
        from server_setting.app import SettingApp
        from textual.widgets import Input, Select, Switch, TabbedContent
        manager = FirewallFixture()
        app = SettingApp(root_manager=UiManager(), firewall_manager=manager)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.query_one('#pages', TabbedContent).active = 'tab-firewall'
            await pilot.pause()
            app.query_one('#fw-service', Select).value = 'mariadb'
            await pilot.pause()
            app.query_one('#fw-enabled', Switch).value = True
            await self.button(app, pilot, 'fw-update')
            self.assertFalse(next(item for item in app.fw_draft['services'] if item['id'] == 'mariadb')['enabled'])
            app.query_one('#fw-sources', Input).value = '192.0.2.50,2001:db8::50'
            app.query_one('#fw-enabled', Switch).value = True
            await self.button(app, pilot, 'fw-update')
            app.query_one('#fw-service', Select).value = 'http'
            await pilot.pause()
            self.assertFalse(app.query_one('#fw-enabled', Switch).value)
            self.assertEqual(app.query_one('#fw-source-mode', Select).value, 'public')
            app.query_one('#fw-service', Select).value = '__new__'
            await pilot.pause()
            app.query_one('#fw-ports', Input).value = '45678'
            app.query_one('#fw-protocol', Select).value = 'udp'
            app.query_one('#fw-source-mode', Select).value = 'public'
            app.query_one('#fw-enabled', Switch).value = True
            await self.button(app, pilot, 'fw-update')
            custom = next(item for item in app.fw_draft['services'] if item['id'] == 'custom-1')
            self.assertEqual((custom['protocol'], custom['ports'], custom['sources']), ('udp', [45678], ['public']))
            await self.button(app, pilot, 'fw-save')
            await self.until(pilot, lambda: not app.root_busy)
            db = next(item for item in manager.saved['services'] if item['id'] == 'mariadb')
            self.assertEqual(set(db['sources']), {'192.0.2.50/32', '2001:db8::50/128'})
            self.assertEqual([call[0] for call in manager.calls], ['save'])
            await self.button(app, pilot, 'fw-load')
            await self.until(pilot, lambda: not app.root_busy)
            self.assertEqual(app.fw_draft, manager.saved)

    async def test_review_cancel_and_pending_never_confirm_from_ui(self):
        from server_setting.app import SettingApp, RootReview
        from textual.widgets import Button, TabbedContent, TextArea
        manager = FirewallFixture()
        app = SettingApp(root_manager=UiManager(), firewall_manager=manager)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.query_one('#pages', TabbedContent).active = 'tab-firewall'
            await pilot.pause()
            await self.button(app, pilot, 'fw-apply')
            await self.until(pilot, lambda: isinstance(app.screen, RootReview))
            review = app.screen.query_one('#root-review-detail', TextArea).text
            for expected in ('IPv4', 'IPv6', 'DROP', '192.0.2.99/32', '2222', 'new SSH'):
                self.assertIn(expected, review)
            await pilot.press('escape')
            await self.until(pilot, lambda: not app.root_busy)
            self.assertEqual(manager.calls, [])
            await self.button(app, pilot, 'fw-apply')
            await self.until(pilot, lambda: isinstance(app.screen, RootReview))
            await self.button(app, pilot, 'root-confirm')
            await self.until(pilot, lambda: not app.root_busy)
            output = app.query_one('#fw-output', TextArea).text
            self.assertIn('AWAITING_NEW_SSH', output)
            self.assertIn('--firewall-confirm fw-test --confirm', output)
            self.assertTrue(app.query_one('#fw-apply', Button).disabled)
            self.assertTrue(app.query_one('#fw-save', Button).disabled)
            self.assertFalse(any('fw-confirm' in (button.id or '') for button in app.query(Button)))
            self.assertEqual([call[0] for call in manager.calls], ['apply'])
            await pilot.press('ctrl+q')
            self.assertFalse(any(call[0] in {'confirm', 'rollback'} for call in manager.calls))

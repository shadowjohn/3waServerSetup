"""Startup configuration never implicitly executes a script; all OS actions are fixtures."""
import asyncio
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_banip_ui import BanIPFixture
from test_firewall_ui import FirewallFixture
from test_packages_ui import PackageFixture
from test_php_feather_ui import FeatherFixture
from test_root_ui import UiManager

BODY = '#!/bin/bash\nset -e\n# PRIVATE_STARTUP_BODY_SENTINEL\nprintf "canary\\n"\n'
CONFIG = {'schema': 1, 'script': BODY, 'enabled': False, 'wait_network': True, 'timeout_seconds': 120}


class StartupFixture:
    def __init__(self):
        self.calls = []
        self.current = copy.deepcopy(CONFIG)
        self.saved = None
        self.pending = None
        self.supported = True
        self.active = 'inactive'
        self.block = None
        self.started = threading.Event()

    def status(self):
        return {'module': 'startup', 'supported': self.supported, 'detail': 'Managed configuration; runtime is separate.',
                'managed': self.current is not None, **{key: self.current[key] for key in ('enabled', 'wait_network', 'timeout_seconds')},
                'script_sha256': hashlib.sha256(self.current['script'].encode()).hexdigest(),
                'unit': 'threewa-startup.service', 'runtime': {'load_state': 'loaded', 'active_state': self.active,
                'sub_state': 'exited' if self.active == 'active' else 'dead', 'result': 'success', 'exec_main_status': '0'},
                'rc_local': {'exists': True, 'executable': False, 'service_active_state': 'inactive'}, 'pending': self.pending}

    def load_current(self):
        return copy.deepcopy(self.current)

    def load_saved(self):
        return copy.deepcopy(self.saved)

    def save(self, config):
        self.calls.append(('save', copy.deepcopy(config)))
        self.saved = copy.deepcopy(config)
        return {'module': 'startup', 'status': 'SAVED', 'message': 'Draft saved; not applied or executed.'}

    def plan(self, config):
        self.calls.append(('plan', copy.deepcopy(config)))
        return {'schema': 1, 'module': 'startup', 'operation': 'apply', 'config': copy.deepcopy(config),
                'before': {'managed': True}, 'notes': ['Configuration and enablement only; no immediate execution.'], 'fingerprint': 'startup-apply'}

    def apply(self, plan):
        self.calls.append(('apply', copy.deepcopy(plan)))
        self.current = copy.deepcopy(plan['config'])
        return {'module': 'startup', 'status': 'STARTUP_APPLIED', 'message': 'Configured; script not executed.'}

    def run_plan(self):
        self.calls.append(('run_plan',))
        return {'schema': 1, 'module': 'startup', 'operation': 'run', 'unit': 'threewa-startup.service',
                'script_sha256': hashlib.sha256(self.current['script'].encode()).hexdigest(),
                'timeout_seconds': self.current['timeout_seconds'], 'before': {'runtime': self.active},
                'notes': ['Already active/exited does not run twice; side effects are not reversible.'], 'fingerprint': 'startup-run'}

    def run_once(self, plan):
        self.started.set()
        if self.block is not None:
            self.block.wait(10)
        self.calls.append(('run', copy.deepcopy(plan)))
        self.active = 'active'
        return {'module': 'startup', 'status': 'STARTUP_RUN_SUCCEEDED', 'message': 'Explicit run finished.', 'verification': {'exec_main_status': '0'}}

    def restore_plan(self):
        self.calls.append(('restore_plan',))
        return {'schema': 1, 'module': 'startup', 'operation': 'restore', 'transaction_id': 'startup-test',
                'recovery': bool(self.pending), 'before': {'runtime': self.active},
                'notes': ['Restore configuration and boot enablement; preserve runtime.'], 'fingerprint': 'startup-restore'}

    def restore(self, plan):
        self.calls.append(('restore', copy.deepcopy(plan)))
        self.pending = None
        self.supported = True
        return {'module': 'startup', 'status': 'STARTUP_RESTORED', 'transaction_id': 'startup-test', 'message': 'Runtime preserved.'}


class StartupCliTests(unittest.TestCase):
    def invoke(self, arguments, manager, root=True):
        from server_setting.__main__ import main
        output = io.StringIO()
        with patch('server_setting.__main__.root_management_available', return_value=root), contextlib.redirect_stdout(output):
            code = main(arguments, startup_manager=manager)
        return code, json.loads(output.getvalue()), output.getvalue()

    def config_file(self, directory):
        path = Path(directory)/'開機設定 draft.json'
        path.write_text(json.dumps(CONFIG), encoding='utf-8')
        path.chmod(0o600)
        return str(path)

    def test_nonroot_and_unconfirmed_mutations_never_read_or_execute(self):
        for mode in ('status', 'load', 'plan', 'save', 'apply', 'run', 'restore'):
            manager = StartupFixture()
            arguments = ['--startup-'+mode, '--confirm']
            if mode in {'plan', 'save', 'apply'}:
                arguments += ['--startup-config', 'unused.json']
            code, result, _ = self.invoke(arguments, manager, root=False)
            self.assertEqual((code, result['code']), (1, 'ROOT_REQUIRED'))
            self.assertEqual(manager.calls, [])
        for mode in ('save', 'apply', 'run', 'restore'):
            manager = StartupFixture()
            code, result, _ = self.invoke(['--startup-'+mode], manager)
            self.assertEqual((code, result['code']), (2, 'CONFIRMATION_REQUIRED'))
            self.assertEqual(manager.calls, [])

    def test_status_plan_and_results_omit_body_only_explicit_load_returns_it(self):
        manager = StartupFixture()
        for mode in ('status', 'load'):
            code, result, text = self.invoke(['--startup-'+mode], manager)
            self.assertEqual(code, 0)
            self.assertNotIn('PRIVATE_STARTUP_BODY_SENTINEL', text)
            if mode == 'load':
                self.assertEqual(result['status'], 'NO_SAVED_CONFIG')
        with tempfile.TemporaryDirectory() as directory:
            path = self.config_file(directory)
            code, result, text = self.invoke(['--startup-plan', '--startup-config', path], manager)
            self.assertEqual((code, result['status']), (0, 'PLANNED'))
            self.assertNotIn('PRIVATE_STARTUP_BODY_SENTINEL', text)
            self.assertIn(hashlib.sha256(BODY.encode()).hexdigest(), text)
            code, result, text = self.invoke(['--startup-save', '--startup-config', path, '--confirm'], manager)
            self.assertEqual((code, result['status']), (0, 'SAVED'))
            self.assertNotIn('PRIVATE_STARTUP_BODY_SENTINEL', text)
            code, result, _ = self.invoke(['--startup-load'], manager)
            self.assertEqual((code, result['config']['script']), (0, BODY))
            code, result, text = self.invoke(['--startup-apply', '--startup-config', path, '--confirm'], manager)
            self.assertEqual((code, result['status']), (0, 'STARTUP_APPLIED'))
            self.assertNotIn('PRIVATE_STARTUP_BODY_SENTINEL', text)
            self.assertEqual([call[0] for call in manager.calls], ['plan', 'save', 'plan', 'apply'])

    def test_run_and_restore_use_distinct_previews_and_never_implicitly_apply(self):
        manager = StartupFixture()
        for mode, status in (('run', 'STARTUP_RUN_SUCCEEDED'), ('restore', 'STARTUP_RESTORED')):
            code, result, text = self.invoke(['--startup-'+mode, '--confirm'], manager)
            self.assertEqual((code, result['status']), (0, status))
            self.assertNotIn('PRIVATE_STARTUP_BODY_SENTINEL', text)
        self.assertEqual([call[0] for call in manager.calls], ['run_plan', 'run', 'restore_plan', 'restore'])

    def test_argument_file_and_safe_error_boundaries(self):
        for arguments in (['--startup-status', '--startup-config', '-'], ['--startup-load', '--startup-config', '-'],
                          ['--startup-run', '--startup-config', '-', '--confirm'], ['--startup-plan'],
                          ['--startup-status', '--values', '{}'], ['--startup-status', '--start'],
                          ['--startup-status', '--packages-catalog'], ['--startup-status', '--text'],
                          ['--status-json', '--startup-config', 'unused']):
            manager = StartupFixture()
            code, result, _ = self.invoke(arguments, manager)
            self.assertEqual((code, result['code']), (2, 'INVALID_ARGUMENT'))
            self.assertEqual(manager.calls, [])
        for text in ('{"schema":1,"schema":1}', 'x'*(1024*1024+1)):
            with patch('sys.stdin', io.StringIO(text)):
                code, result, _ = self.invoke(['--startup-plan', '--startup-config', '-'], StartupFixture())
            self.assertEqual((code, result['code']), (2, 'INVALID_CONFIG'))
        from server_setting.root_actions import RootActionError
        manager = StartupFixture()
        with patch.object(manager, 'run_plan', side_effect=RootActionError(BODY, 'STARTUP_SYNTAX')):
            code, result, text = self.invoke(['--startup-run', '--confirm'], manager)
        self.assertEqual((code, result['code']), (1, 'STARTUP_SYNTAX'))
        self.assertNotIn('PRIVATE_STARTUP_BODY_SENTINEL', text)


class StartupUiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)

    async def until(self, pilot, condition):
        for _ in range(150):
            await pilot.pause(.02)
            if condition():
                return
        self.fail('Startup UI did not settle')

    async def button(self, app, pilot, identifier):
        app.screen.query_one('#'+identifier).focus()
        await pilot.press('enter')
        await pilot.pause(.05)

    def application(self, startup, firewall=None):
        from server_setting.app import SettingApp
        return SettingApp(root_manager=UiManager(), firewall_manager=firewall or FirewallFixture(),
                          banip_manager=BanIPFixture(), package_manager=PackageFixture(),
                          php_feather_manager=FeatherFixture(), startup_manager=startup)

    async def test_narrow_save_load_and_refresh_preserve_edited_draft_without_apply(self):
        from textual.widgets import Checkbox, Input, TabbedContent, TextArea
        manager = StartupFixture()
        app = self.application(manager)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(.05)
            app.query_one('#pages', TabbedContent).active = 'tab-startup'
            await pilot.pause(.05)
            editor = app.query_one('#startup-script', TextArea)
            self.assertEqual(editor.text, BODY)
            edited = BODY+'# EDITED DRAFT\n'
            editor.load_text(edited)
            app.query_one('#startup-enabled', Checkbox).value = True
            app.query_one('#startup-wait-network', Checkbox).value = False
            app.query_one('#startup-timeout', Input).value = '23'
            await app.action_refresh()
            self.assertEqual(editor.text, edited)
            self.assertEqual(app.query_one('#startup-timeout', Input).value, '23')
            await self.button(app, pilot, 'startup-save')
            await self.until(pilot, lambda:not app.root_busy)
            self.assertEqual(manager.saved, {**CONFIG, 'script': edited, 'enabled': True, 'wait_network': False, 'timeout_seconds': 23})
            self.assertNotIn('PRIVATE_STARTUP_BODY_SENTINEL', app.query_one('#out-startup', TextArea).text)
            editor.load_text('# unsaved\n')
            await self.button(app, pilot, 'startup-saved')
            await self.until(pilot, lambda:not app.root_busy)
            self.assertEqual(editor.text, edited)
            await self.button(app, pilot, 'startup-current')
            await self.until(pilot, lambda:not app.root_busy)
            self.assertEqual(editor.text, BODY)
            manager.saved = None
            await self.button(app, pilot, 'startup-saved')
            await self.until(pilot, lambda:not app.root_busy)
            self.assertEqual(editor.text, BODY)
            self.assertIn('NO_SAVED_CONFIG', app.query_one('#out-startup', TextArea).text)
            await self.button(app, pilot, 'startup-template')
            await self.until(pilot, lambda:not app.root_busy)
            self.assertTrue(editor.text.startswith('#!/bin/bash\n'))
            self.assertNotIn('PRIVATE_STARTUP_BODY_SENTINEL', editor.text)
            self.assertFalse(app.query_one('#startup-enabled', Checkbox).value)
            self.assertEqual(app.query_one('#startup-timeout', Input).value, '120')
            self.assertEqual([call[0] for call in manager.calls], ['save'])

    async def test_apply_cancel_then_run_review_is_separate_and_busy_locks_editor(self):
        from server_setting.app import RootReview
        from textual.widgets import TabbedContent, TextArea
        manager = StartupFixture()
        manager.block = threading.Event()
        app = self.application(manager)
        try:
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause(.05)
                app.query_one('#pages', TabbedContent).active = 'tab-startup'
                await pilot.pause(.05)
                await self.button(app, pilot, 'startup-apply')
                await self.until(pilot, lambda:isinstance(app.screen, RootReview))
                detail = app.screen.query_one('#root-review-detail', TextArea).text
                for text in ('PRIVATE_STARTUP_BODY_SENTINEL', 'threewa-startup.service', 'startup.sh', 'enabled', 'wait_network', 'not execute'):
                    self.assertIn(text, detail)
                self.assertTrue(app.query_one('#startup-script', TextArea).disabled)
                await pilot.press('escape')
                await self.until(pilot, lambda:not app.root_busy)
                self.assertEqual([call[0] for call in manager.calls], ['plan'])
                await self.button(app, pilot, 'startup-apply')
                await self.until(pilot, lambda:isinstance(app.screen, RootReview))
                await self.button(app, pilot, 'root-confirm')
                await self.until(pilot, lambda:not app.root_busy)
                self.assertEqual([call[0] for call in manager.calls], ['plan', 'plan', 'apply'])
                await self.button(app, pilot, 'startup-run')
                await self.until(pilot, lambda:isinstance(app.screen, RootReview))
                detail = app.screen.query_one('#root-review-detail', TextArea).text
                self.assertIn('side effects', detail)
                self.assertNotIn('PRIVATE_STARTUP_BODY_SENTINEL', detail)
                await pilot.press('escape')
                await self.until(pilot, lambda:not app.root_busy)
                self.assertFalse(any(call[0]=='run' for call in manager.calls))
                await self.button(app, pilot, 'startup-run')
                await self.until(pilot, lambda:isinstance(app.screen, RootReview))
                await self.button(app, pilot, 'root-confirm')
                await self.until(pilot, manager.started.is_set)
                self.assertTrue(app.query_one('#startup-script', TextArea).disabled)
                await pilot.press('ctrl+q')
                self.assertTrue(app.is_running)
                manager.block.set()
                await self.until(pilot, lambda:not app.root_busy)
                self.assertIn('STARTUP_RUN_SUCCEEDED', app.query_one('#out-startup', TextArea).text)
                self.assertNotIn('PRIVATE_STARTUP_BODY_SENTINEL', app.query_one('#out-startup', TextArea).text)
                self.assertEqual(manager.active, 'active')
        finally:
            manager.block.set()

    async def test_own_pending_blocks_other_mutations_but_allows_recovery_and_network_pending_blocks_it(self):
        from server_setting.app import RootReview
        from textual.widgets import Button, TabbedContent, TextArea
        manager = StartupFixture()
        manager.pending = {'transaction_id': 'startup-test', 'status': 'STARTUP_RUNNING', 'kind': 'run'}
        manager.supported = False  # Half-written managed files still need their recovery entry point.
        firewall = FirewallFixture()
        app = self.application(manager, firewall)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(.05)
            app.query_one('#pages', TabbedContent).active = 'tab-startup'
            await pilot.pause(.05)
            for identifier in ('startup-save', 'startup-apply', 'startup-run', 'fw-apply', 'root-apply-apache', 'packages-install', 'feather-apply'):
                self.assertTrue(app.query_one('#'+identifier, Button).disabled, identifier)
            self.assertFalse(app.query_one('#startup-restore', Button).disabled)
            self.assertTrue(app.query_one('#startup-script', TextArea).disabled)
            await self.button(app, pilot, 'startup-restore')
            await self.until(pilot, lambda:isinstance(app.screen, RootReview))
            detail = app.screen.query_one('#root-review-detail', TextArea).text
            self.assertIn('runtime', detail)
            self.assertIn('not rerun', detail)
            await self.button(app, pilot, 'root-confirm')
            await self.until(pilot, lambda:not app.root_busy)
            self.assertEqual([call[0] for call in manager.calls], ['restore_plan', 'restore'])
            self.assertFalse(app.query_one('#root-apply-apache', Button).disabled)
            firewall.pending = {'transaction_id':'fw-test', 'remaining_seconds':120, 'deadline_boottime':100000}
            await app.action_refresh()
            self.assertTrue(app.query_one('#startup-restore', Button).disabled)

    async def test_nonroot_startup_remains_observational(self):
        from server_setting.app import SettingApp
        from textual.widgets import TabbedContent
        with patch('server_setting.app.root_management_available', return_value=False):
            app = SettingApp()
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause(.05)
                app.query_one('#pages', TabbedContent).active = 'tab-startup'
                await pilot.pause(.05)
                self.assertFalse(list(app.query('#startup-script')))
                self.assertFalse(list(app.query('#startup-apply')))
                self.assertTrue(list(app.query('#preview-startup')))

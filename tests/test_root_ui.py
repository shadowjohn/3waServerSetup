import asyncio
import copy
import threading
import unittest
from server_setting.model import FIELDS


class RootReviewDetailsTests(unittest.TestCase):
    def test_mariadb_security_keep_review_does_not_claim_remote_changes(self):
        from types import SimpleNamespace
        from server_setting.app import SettingApp
        detail = SettingApp._review_details(SimpleNamespace(), 'mariadb-security', 'security',
                                           {'remote_enabled': None, 'change_password': True, 'notes': []})
        self.assertIn('KEEP', detail)
        self.assertIn('listener, account lock and grants unchanged', detail)
        self.assertNotIn('OFF / lock', detail)
        self.assertNotIn('ON / bind', detail)

    def test_noop_time_review_always_shows_selected_timezone_and_service_targets(self):
        from types import SimpleNamespace
        from server_setting.app import SettingApp
        context = SimpleNamespace(root_capabilities={'time': {'configured': {'Timezone': 'Asia/Taipei'}}})
        plan = {'values': {'Timezone': 'Asia/Taipei'}, 'service_active': None, 'service_enabled': None,
                'files': [], 'validate': [], 'apply_commands': [], 'notes': []}
        for operation in ('apply', 'apply-recommended'):
            with self.subTest(operation=operation):
                detail = SettingApp._review_details(context, 'time', operation, plan)
                self.assertIn('NO DIFFERENCE', detail)
                self.assertIn('Selected targets:\nTimezone: Asia/Taipei', detail)
                self.assertIn('Service now: keep current', detail)
                self.assertIn('At boot: keep current', detail)


class UiManager:
    def __init__(self):
        self.current = {'ServerTokens': 'Full', 'ServerSignature': 'On'}
        self.security_remote = False
        self.operations = []
        self.saved = None
        self.block = None
        self.started = threading.Event()

    def capabilities(self, module):
        return {'supported': True, 'packages': ['apache2'], 'installed': True,
                'unit': 'apache2.service', 'active': 'inactive', 'enabled': 'disabled',
                'configured': dict(self.current) if module == 'apache' else dict(FIELDS[module])}

    def recommended(self, module):
        return dict(FIELDS[module])

    def load_saved(self, module):
        return self.saved

    def plan(self, module, values=None, service_active=None, service_enabled=None):
        return {'schema': 1, 'module': module, 'values': dict(values or FIELDS[module]),
                'service_active': service_active, 'service_enabled': service_enabled,
                'files': [{'path': '/etc/apache2/conf-available/3wa.conf', 'before_sha256': 'a' * 64,
                           'content': 'ServerTokens Prod\n', 'mode': 0o644}],
                'validate': [['apache2ctl', 'configtest']], 'apply_commands': [],
                'health_scope': 'configuration only; inactive service is unverified',
                'notes': [], 'fingerprint': 'planned'}

    def install_plan(self, module):
        return {'module': module, 'packages': ['apache2'], 'notes': ['Packages are not rolled back.']}

    def restore_plan(self, module):
        return {'module': module, 'transaction_id': 'last', 'files': [{'path': '/etc/apache2/conf-available/3wa.conf'}],
                'service_snapshot': {'active': 'inactive', 'enabled': 'disabled'}, 'notes': ['Restore prior files.'],
                'fingerprint': 'reviewed-restore'}

    def apply(self, plan):
        self.started.set()
        if self.block is not None:
            self.block.wait(3)
        self.operations.append(('apply', copy.deepcopy(plan)))
        self.current = dict(plan['values'])
        self.saved = copy.deepcopy(plan)
        return {'status': 'APPLIED', 'module': plan['module'], 'message': 'Saved custom values',
                'verification': {'verified': False, 'health_scope': 'configuration only; service inactive'}}

    def install(self, module):
        self.operations.append(('install', module))
        return {'status': 'INSTALLED', 'module': module, 'message': 'Selected package installed'}

    def restore_last(self, module, expected_fingerprint=None):
        if expected_fingerprint != 'reviewed-restore':
            raise ValueError('Restore must match the reviewed transaction')
        self.operations.append(('restore', module))
        self.current = {'ServerTokens': 'Full', 'ServerSignature': 'On'}
        return {'status': 'RESTORED', 'module': module, 'message': 'Previous files restored'}

    def apt_plan(self, operation):
        return {'module': 'apt', 'operation': operation, 'command': ['apt-get', operation],
                'packages': [{'name': 'demo', 'installed': '1', 'candidate': '2'}], 'kept_back': ['held'],
                'reboot_required': False, 'notes': ['No removals or reboot; preserve modified conffiles.'],
                'fingerprint': 'apt-preview'}

    def apt_run(self, plan):
        self.operations.append(('apt', plan['operation']))
        return {'status': 'APT_UPDATED' if plan['operation'] == 'update' else 'APT_UPGRADED',
                'module': 'apt', 'reboot_required': False}

    def mariadb_security_status(self):
        return {'installed': True, 'active': True, 'supported': True, 'detail': 'Local socket available',
                'remote_enabled': self.security_remote, 'root_remote_exists': self.security_remote, 'root_remote_locked': not self.security_remote,
                'bind_address': '0.0.0.0' if self.security_remote else '127.0.0.1',
                'local_socket_preserved': True, 'local_password_set': False}

    def mariadb_security_plan(self, remote_enabled=None, change_password=False):
        return {'module': 'mariadb-security', 'remote_enabled': remote_enabled,
                'change_password': change_password, 'fingerprint': 'security-preview', 'notes': []}

    def mariadb_security_apply(self, plan, password=None):
        self.operations.append(('mariadb-security', copy.deepcopy(plan)))
        self.security_password = password
        if plan['remote_enabled'] is not None:
            self.security_remote = plan['remote_enabled']
        return {'status': 'APPLIED', 'module': 'mariadb-security', 'message': 'Root access updated'}


class RootUiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 2

    async def until(self, pilot, condition):
        for _ in range(100):
            await pilot.pause(0.02)
            if condition():
                return
        self.fail('UI operation did not settle')

    async def press_button(self, app, pilot, widget_id):
        from textual.widgets import Button
        app.screen.query_one('#' + widget_id, Button).focus()
        await pilot.press('enter')
        await pilot.pause()

    async def test_mariadb_security_masked_inputs_clear_before_review_and_cancel(self):
        from server_setting.app import SettingApp, RootReview
        from textual.widgets import Input, Select, TabbedContent, TextArea
        manager = UiManager()
        app = SettingApp(root_manager=manager)
        secret = 'ui-password-never-in-review'
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.query_one('#pages', TabbedContent).active = 'tab-mariadb'
            await pilot.pause()
            for widget_id in ('mariadb-new-password', 'mariadb-confirm-password'):
                field = app.query_one('#' + widget_id, Input)
                self.assertTrue(field.password)
                field.value = secret
            app.query_one('#mariadb-remote', Select).value = 'on'
            await self.press_button(app, pilot, 'root-security-mariadb')
            await self.until(pilot, lambda: isinstance(app.screen, RootReview))
            for widget_id in ('mariadb-new-password', 'mariadb-confirm-password'):
                self.assertEqual(app.query_one('#' + widget_id, Input).value, '')
            review = app.screen.query_one('#root-review-detail', TextArea).text
            self.assertNotIn(secret, review)
            self.assertIn('root@%', review)
            self.assertIn('WITH GRANT OPTION', review)
            self.assertIn('0.0.0.0', review)
            self.assertIn('unix_socket', review)
            self.assertEqual(manager.operations, [])
            await pilot.press('escape')
            await self.until(pilot, lambda: not app.root_busy)
            self.assertIsNone(app._mariadb_password)
            self.assertEqual(manager.operations, [])

    async def test_mariadb_security_confirm_blank_keep_and_safe_failure(self):
        from server_setting.app import SettingApp, RootReview
        from textual.widgets import Input, Select, TabbedContent, TextArea
        from unittest.mock import patch
        manager = UiManager()
        app = SettingApp(root_manager=manager)
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            app.query_one('#pages', TabbedContent).active = 'tab-mariadb'
            await pilot.pause()
            app.query_one('#mariadb-remote', Select).value = 'off'
            await self.press_button(app, pilot, 'root-security-mariadb')
            await self.until(pilot, lambda: isinstance(app.screen, RootReview))
            self.assertIn('127.0.0.1', app.screen.query_one('#root-review-detail', TextArea).text)
            await self.press_button(app, pilot, 'root-confirm')
            await self.until(pilot, lambda: not app.root_busy)
            self.assertIsNone(manager.security_password)
            self.assertFalse(manager.operations[-1][1]['change_password'])
            secret = 'failure-must-not-echo-this'
            app.query_one('#mariadb-remote', Select).value = 'on'
            app.query_one('#mariadb-new-password', Input).value = secret
            app.query_one('#mariadb-confirm-password', Input).value = secret
            await self.press_button(app, pilot, 'root-security-mariadb')
            await self.until(pilot, lambda: isinstance(app.screen, RootReview))
            await self.press_button(app, pilot, 'root-confirm')
            await self.until(pilot, lambda: not app.root_busy)
            self.assertEqual(manager.security_password, secret)
            self.assertTrue(app.mariadb_security['remote_enabled'])
            self.assertEqual(app.mariadb_security['bind_address'], '0.0.0.0')
            self.assertEqual(app.query_one('#mariadb-new-password', Input).value, '')
            self.assertEqual(app.query_one('#mariadb-confirm-password', Input).value, '')
            self.assertNotIn(secret, str(manager.operations))
            self.assertIsNone(app._mariadb_password)
            app.query_one('#mariadb-new-password', Input).value = secret
            app.query_one('#mariadb-confirm-password', Input).value = secret
            await self.press_button(app, pilot, 'root-security-mariadb')
            await self.until(pilot, lambda: isinstance(app.screen, RootReview))
            with patch.object(manager, 'mariadb_security_apply', side_effect=RuntimeError(secret)):
                await self.press_button(app, pilot, 'root-confirm')
                await self.until(pilot, lambda: not app.root_busy)
            self.assertIsNone(app._mariadb_password)
            self.assertNotIn(secret, app.query_one('#out-mariadb-security', TextArea).text)

    async def test_mariadb_security_mismatch_clears_and_unavailable_service_is_explicit(self):
        from server_setting.app import SettingApp
        from textual.widgets import Button, Input, Static, TabbedContent
        from unittest.mock import patch
        manager = UiManager()
        app = SettingApp(root_manager=manager)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.query_one('#pages', TabbedContent).active = 'tab-mariadb'
            await pilot.pause()
            app.query_one('#mariadb-new-password', Input).value = 'mismatched-secret'
            app.query_one('#mariadb-confirm-password', Input).value = 'different-secret'
            await self.press_button(app, pilot, 'root-security-mariadb')
            self.assertFalse(app.root_busy)
            self.assertIsNone(app._mariadb_password)
            self.assertEqual(app.query_one('#mariadb-new-password', Input).value, '')
            self.assertEqual(app.query_one('#mariadb-confirm-password', Input).value, '')
            self.assertEqual(manager.operations, [])
            with patch.object(manager, 'mariadb_security_status', return_value={
                    'supported': False, 'installed': False, 'active': False, 'detail': 'MariaDB is not installed.'}):
                await app.action_refresh()
            self.assertTrue(app.query_one('#root-security-mariadb', Button).disabled)
            self.assertIn('not installed', str(app.query_one('#mariadb-security-status', Static).content))

    async def test_apt_page_never_autoexecutes_and_requires_review_confirmation(self):
        from server_setting.app import SettingApp, RootReview
        from textual.widgets import TabbedContent, TextArea
        manager = UiManager()
        app = SettingApp(root_manager=manager)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            self.assertEqual(manager.operations, [])
            app.query_one('#pages', TabbedContent).active = 'tab-apt'
            await pilot.pause()
            for operation in ('update', 'upgrade'):
                await self.press_button(app, pilot, 'root-' + operation + '-apt')
                await self.until(pilot, lambda: isinstance(app.screen, RootReview))
                review = app.screen.query_one('#root-review-detail', TextArea).text
                self.assertIn('demo', review)
                self.assertIn('held', review)
                self.assertEqual(manager.operations, [])
                await pilot.press('escape')
                await self.until(pilot, lambda: not app.root_busy)
                self.assertEqual(manager.operations, [])
            await self.press_button(app, pilot, 'root-upgrade-apt')
            await self.until(pilot, lambda: isinstance(app.screen, RootReview))
            await self.press_button(app, pilot, 'root-confirm')
            await self.until(pilot, lambda: not app.root_busy)
            self.assertEqual(manager.operations, [('apt', 'upgrade')])
            self.assertIn('APT_UPGRADED', app.query_one('#out-apt', TextArea).text)

    async def test_custom_form_is_reviewed_once_and_cancel_does_not_apply(self):
        from server_setting.app import SettingApp, RootReview
        from textual.widgets import Input, Select, TabbedContent, TextArea
        manager = UiManager()
        app = SettingApp(root_manager=manager)
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            app.query_one('#pages', TabbedContent).active = 'tab-apache'
            await pilot.pause()
            app.query_one('#apache-0', Input).value = 'Prod'
            app.query_one('#root-active-apache', Select).value = 'start'
            app.query_one('#root-enabled-apache', Select).value = 'disable'
            await self.press_button(app, pilot, 'root-apply-apache')
            await self.until(pilot, lambda: isinstance(app.screen, RootReview))
            review = app.screen.query_one('#root-review-detail', TextArea).text
            self.assertIn('Full', review)
            self.assertIn('Prod', review)
            self.assertIn('start', review.lower())
            self.assertIn('disable', review.lower())
            self.assertEqual(manager.operations, [])
            await self.press_button(app, pilot, 'root-cancel')
            await self.until(pilot, lambda: not app.root_busy)
            self.assertEqual(manager.operations, [])

    async def test_confirmed_custom_apply_and_restore_refresh_current_values(self):
        from server_setting.app import SettingApp, RootReview
        from textual.widgets import Input, Select, TabbedContent, TextArea, Static
        manager = UiManager()
        app = SettingApp(root_manager=manager)
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            app.query_one('#pages', TabbedContent).active = 'tab-apache'
            await pilot.pause()
            app.query_one('#apache-0', Input).value = 'Prod'
            app.query_one('#root-active-apache', Select).value = 'stop'
            app.query_one('#root-enabled-apache', Select).value = 'enable'
            await self.press_button(app, pilot, 'root-apply-apache')
            await self.until(pilot, lambda: isinstance(app.screen, RootReview))
            await self.press_button(app, pilot, 'root-confirm')
            await self.until(pilot, lambda: not app.root_busy)
            self.assertEqual(manager.operations[0][1]['values']['ServerTokens'], 'Prod')
            self.assertIs(manager.operations[0][1]['service_active'], False)
            self.assertIs(manager.operations[0][1]['service_enabled'], True)
            self.assertIn('APPLIED', app.query_one('#out-apache', TextArea).text)
            self.assertIn('configuration only', app.query_one('#out-apache', TextArea).text)
            self.assertIn('Prod', str(app.query_one('#state-apache', Static).content))
            await self.press_button(app, pilot, 'root-restore-apache')
            await self.until(pilot, lambda: isinstance(app.screen, RootReview))
            await self.press_button(app, pilot, 'root-confirm')
            await self.until(pilot, lambda: not app.root_busy)
            self.assertEqual(manager.current['ServerTokens'], 'Full')
            self.assertEqual(app.query_one('#apache-0', Input).value, 'Full')

    async def test_busy_transaction_blocks_quit_and_reentry(self):
        from server_setting.app import SettingApp, RootReview
        from textual.widgets import TabbedContent, Button
        manager = UiManager()
        manager.block = threading.Event()
        app = SettingApp(root_manager=manager)
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            app.query_one('#pages', TabbedContent).active = 'tab-apache'
            await pilot.pause()
            await self.press_button(app, pilot, 'root-apply-recommended-apache')
            await self.until(pilot, lambda: isinstance(app.screen, RootReview))
            await self.press_button(app, pilot, 'root-confirm')
            await self.until(pilot, manager.started.is_set)
            self.assertTrue(app.root_busy)
            self.assertTrue(app.query_one('#root-install-apache', Button).disabled)
            await pilot.press('ctrl+q')
            self.assertTrue(app.is_running)
            manager.block.set()
            await self.until(pilot, lambda: not app.root_busy)
            self.assertEqual(len(manager.operations), 1)

    async def test_narrow_root_buttons_are_visible_by_tab_and_open_review(self):
        from server_setting.app import SettingApp, RootReview
        from textual.widgets import TabbedContent, Button
        app = SettingApp(root_manager=UiManager())
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            self.assertFalse(app.query_one('#mascot').display)
            app.query_one('#pages', TabbedContent).active = 'tab-apache'
            await pilot.pause()
            for widget_id in ('root-apply-apache', 'root-restore-apache'):
                for _ in range(40):
                    if getattr(app.focused, 'id', None) == widget_id:
                        break
                    await pilot.press('tab')
                    await pilot.pause(0.02)
                else:
                    self.fail('Tab did not reach ' + widget_id)
                button = app.query_one('#' + widget_id, Button)
                self.assertGreaterEqual(button.region.x, 0)
                self.assertLessEqual(button.region.right, 80)
                self.assertGreaterEqual(button.region.y, 0)
                self.assertLessEqual(button.region.bottom, 24)
                await pilot.press('enter')
                await self.until(pilot, lambda: isinstance(app.screen, RootReview))
                await pilot.press('escape')
                await self.until(pilot, lambda: not app.root_busy)

    async def test_install_review_explains_package_service_effects_before_confirm(self):
        from server_setting.app import SettingApp, RootReview
        from textual.widgets import TabbedContent, TextArea
        manager = UiManager()
        app = SettingApp(root_manager=manager)
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            app.query_one('#pages', TabbedContent).active = 'tab-apache'
            await pilot.pause()
            await self.press_button(app, pilot, 'root-install-apache')
            await self.until(pilot, lambda: isinstance(app.screen, RootReview))
            text = app.screen.query_one('#root-review-detail', TextArea).text
            self.assertIn('apache2', text)
            self.assertIn('may start or enable', text)
            self.assertNotIn('Service now: keep current', text)
            self.assertEqual(manager.operations, [])
            await self.press_button(app, pilot, 'root-confirm')
            await self.until(pilot, lambda: not app.root_busy)
            self.assertEqual(manager.operations, [('install', 'apache')])
            self.assertIn('INSTALLED', app.query_one('#out-apache', TextArea).text)

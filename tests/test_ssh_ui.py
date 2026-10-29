"""SSH overview is unit-only; keepalive edits require the ordinary review UI."""
import asyncio
import copy
import unittest
from unittest.mock import patch

from server_setting.model import FIELDS
from tests.test_root_ui import UiManager


class SSHOverviewTests(unittest.TestCase):
    def test_ssh_overview_observes_unit_without_reading_config_keys_or_native_effective_values(self):
        from server_setting.probes import collect
        reads, commands = [], []
        def read(path):
            reads.append(str(path))
            return {'/etc/os-release': 'ID=ubuntu\nVERSION_ID="26.04"\n'}.get(str(path))
        def command(argv):
            commands.append(argv)
            if argv[:3] == ['systemctl', 'show', 'ssh.service']:
                return 'LoadState=loaded\nActiveState=active\nUnitFileState=enabled\n'
            return None
        with patch('server_setting.probes._read', side_effect=read), \
                patch('server_setting.probes._command', side_effect=command), \
                patch('server_setting.probes.Path.glob', return_value=[]):
            snapshot = collect(system='Linux')
        self.assertIn('ssh', snapshot['modules'])
        observed = snapshot['modules']['ssh']
        self.assertEqual(observed['status'], 'OBSERVED')
        self.assertIn('ActiveState=active', observed['detail'])
        self.assertEqual(observed['configured'], {})
        self.assertEqual(observed['runtime'], 'UNVERIFIED')
        self.assertFalse(any('/ssh' in path for path in reads))
        self.assertFalse(any(argv[0] in {'ssh', 'sshd'} for argv in commands))


class SSHUiManager(UiManager):
    def __init__(self):
        super().__init__()
        self.ssh_current = {'ClientAliveInterval': '0', 'TCPKeepAlive': 'yes'}

    def capabilities(self, module):
        result = super().capabilities(module)
        if module == 'ssh':
            result.update(unit=None, active=None, enabled=None, packages=['openssh-server'],
                          configured=dict(self.ssh_current))
        return result

    def plan(self, module, values=None, service_active=None, service_enabled=None):
        if module != 'ssh':
            return super().plan(module, values, service_active, service_enabled)
        if service_active is not None or service_enabled is not None:
            raise AssertionError('SSH UI must not send service state targets')
        return {'schema': 1, 'module': 'ssh', 'values': dict(values or FIELDS['ssh']),
                'service_active': None, 'service_enabled': None,
                'files': [{'path': '/etc/ssh/sshd_config.d/00-3wa-server-setup.conf',
                           'before_sha256': None, 'content': 'ClientAliveInterval 60\nTCPKeepAlive yes\n'}],
                'validate': [['sshd', '-t']], 'apply_commands': [['systemctl', 'reload', 'ssh.service']],
                'notes': ['Global settings; reload only. Existing sessions are not verified.'],
                'health_scope': 'global config only', 'fingerprint': 'ssh-reviewed'}

    def apply(self, plan):
        if plan['module'] != 'ssh':
            return super().apply(plan)
        self.operations.append(('apply', copy.deepcopy(plan)))
        self.ssh_current = dict(plan['values'])
        self.saved = copy.deepcopy(plan)
        return {'status': 'APPLIED', 'module': 'ssh', 'verification': {
            'verified': True, 'runtime_verified': False, 'detail': 'Global config only; existing sessions unverified.'}}


class SSHUiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        from server_setting.probes import collect
        for target, value in (
                ('server_setting.app.collect', collect(system='Windows')),
                ('server_setting.release_update.ReleaseManager.status',
                 {'supported': False, 'managed': False, 'installed': None, 'previous': None, 'pending': None}),
                ('server_setting.root_network.NetworkManager.status',
                 {'supported': False, 'pending': None, 'interfaces': [], 'config': None})):
            observer = patch(target, return_value=value)
            observer.start()
            self.addCleanup(observer.stop)

    async def until(self, pilot, condition):
        for _ in range(100):
            await pilot.pause(.02)
            if condition():
                return
        self.fail('SSH page did not settle')

    async def press(self, app, pilot, identifier):
        app.screen.query_one('#' + identifier).focus()
        await pilot.press('enter')
        await pilot.pause(.02)

    async def test_recommended_keepalive_fields_disabled_service_controls_and_review_cancel_apply(self):
        from server_setting.app import SettingApp, RootReview
        from textual.widgets import Input, Select, TabbedContent, TextArea
        manager = SSHUiManager()
        app = SettingApp(root_manager=manager)
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            app.query_one('#pages', TabbedContent).active = 'tab-ssh'
            await pilot.pause()
            self.assertTrue(app.query_one('#root-active-ssh', Select).disabled)
            self.assertTrue(app.query_one('#root-enabled-ssh', Select).disabled)
            await self.press(app, pilot, 'root-recommended-ssh')
            await self.until(pilot, lambda: not app.root_busy)
            self.assertEqual(app.query_one('#ssh-0', Input).value, '60')
            self.assertEqual(app.query_one('#ssh-1', Input).value, 'yes')
            await self.press(app, pilot, 'root-apply-ssh')
            await self.until(pilot, lambda: isinstance(app.screen, RootReview))
            detail = app.screen.query_one('#root-review-detail', TextArea).text
            self.assertIn('ClientAliveInterval: 60', detail)
            self.assertIn('TCPKeepAlive: yes', detail)
            self.assertIn('reload', detail)
            self.assertNotIn('"restart"', detail)
            self.assertEqual(manager.operations, [])
            await pilot.press('escape')
            await self.until(pilot, lambda: not app.root_busy)
            self.assertEqual(manager.operations, [])
            await self.press(app, pilot, 'root-apply-ssh')
            await self.until(pilot, lambda: isinstance(app.screen, RootReview))
            await self.press(app, pilot, 'root-confirm')
            await self.until(pilot, lambda: not app.root_busy)
            self.assertEqual(len(manager.operations), 1)
            plan = manager.operations[0][1]
            self.assertEqual(plan['values'], {'ClientAliveInterval': '60', 'TCPKeepAlive': 'yes'})
            self.assertIsNone(plan['service_active'])
            self.assertIsNone(plan['service_enabled'])
            self.assertIn('APPLIED', app.query_one('#out-ssh', TextArea).text)
            self.assertTrue(app.query_one('#root-active-ssh', Select).disabled)
            self.assertTrue(app.query_one('#root-enabled-ssh', Select).disabled)


if __name__ == '__main__':
    unittest.main()

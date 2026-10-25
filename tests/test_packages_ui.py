"""Selected missing-package installation through real UI/CLI boundaries."""
import asyncio
import contextlib
import copy
import io
import json
import unittest
from unittest.mock import patch

from test_banip_ui import BanIPFixture
from test_firewall_ui import FirewallFixture
from test_root_ui import UiManager


class PackageFixture:
    def __init__(self):
        self.calls = []
        self.installed = {'proj-bin', 'php-common'}
        self.search_hits = [{'name': 'jq', 'description': 'JSON processor'}]

    def status(self, package_names=None):
        names = package_names or ['gdal-bin', 'proj-bin', 'php-common', 'jq']
        if len(names) > 32:
            raise ValueError('Status accepts at most 32 package names.')
        return {'supported': True, 'detail': 'Configured APT candidates; runtime modules unverified.',
                'items': [{'name': name, 'installed': name in self.installed,
                           'current': '1.0' if name in self.installed else None, 'candidate': '2.0'} for name in names]}

    def catalog(self):
        rows = {row['name']: row for row in self.status()['items']}
        return [
            {'id': 'gis_cli', 'label': 'GIS CLI', 'packages': ['gdal-bin', 'proj-bin'], 'detail': 'CLI tools',
             'items': [dict(rows[name], id=name, label=name, packages=[name], detail='APT package')
                       for name in ('gdal-bin', 'proj-bin')]},
            {'id': 'php', 'label': 'PHP', 'packages': ['php-common'], 'detail': 'Official PHP packages',
             'items': [dict(rows['php-common'], id='json', label='JSON', packages=['php-common'],
                            detail='Provider package; runtime extension not verified.', provided_by='php-common', component_status='builtin/provided'),
                       {'id': 'custom-sqlite', 'label': 'FeatherMountain SQLite', 'packages': [], 'detail': 'Separate custom compiler',
                        'installed': None, 'current': None, 'candidate': None, 'component_status': 'external build'}]}]

    def search(self, query):
        self.calls.append(('search', query))
        return copy.deepcopy(self.search_hits)

    def plan(self, names):
        self.calls.append(('plan', list(names)))
        if self.installed.intersection(names):
            raise ValueError('This page must not upgrade installed packages.')
        return {'schema': 1, 'module': 'packages', 'names': list(names),
                'changes': [{'name': name, 'action': 'install', 'installed': None, 'candidate': '2.0'} for name in names] +
                           [{'name': 'libfixture1', 'action': 'install', 'installed': None, 'candidate': '1.5'}],
                'download_bytes': 1048576, 'disk_bytes': 4194304,
                'notes': ['Dependency versions are included; no removal.'], 'fingerprint': 'package-review'}

    def install(self, plan):
        self.calls.append(('install', copy.deepcopy(plan)))
        self.installed.update(plan['names'])
        return {'status': 'PACKAGES_INSTALLED', 'module': 'packages', 'transaction_id': 'packages-test',
                'message': 'Selected missing packages installed.', 'versions': {name: '2.0' for name in plan['names']}}


class PackagesCliTests(unittest.TestCase):
    def invoke(self, arguments, manager, root=True):
        from server_setting.__main__ import main
        output = io.StringIO()
        with patch('server_setting.__main__.root_management_available', return_value=root), contextlib.redirect_stdout(output):
            code = main(arguments, package_manager=manager)
        return code, json.loads(output.getvalue())

    def test_root_and_confirmation_precede_any_package_transaction(self):
        for arguments in (['--packages-catalog'], ['--packages-search', 'gdal'],
                          ['--packages-plan', 'gdal-bin'], ['--packages-install', 'gdal-bin', '--confirm']):
            manager = PackageFixture()
            code, result = self.invoke(arguments, manager, root=False)
            self.assertEqual((code, result['code']), (1, 'ROOT_REQUIRED'))
            self.assertEqual(manager.calls, [])
        manager = PackageFixture()
        code, result = self.invoke(['--packages-install', 'gdal-bin'], manager)
        self.assertEqual((code, result['code']), (2, 'CONFIRMATION_REQUIRED'))
        self.assertEqual(manager.calls, [])

    def test_catalog_search_plan_and_selected_install_are_separate_json_operations(self):
        manager = PackageFixture()
        code, result = self.invoke(['--packages-catalog'], manager)
        self.assertEqual((code, result['catalog'][0]['items'][1]['installed']), (0, True))
        self.assertEqual(manager.calls, [])
        code, result = self.invoke(['--packages-search', 'JSON processor'], manager)
        self.assertEqual((code, result['items'][0]['name']), (0, 'jq'))
        code, result = self.invoke(['--packages-plan', 'gdal-bin'], manager)
        self.assertEqual((code, result['plan']['names']), (0, ['gdal-bin']))
        self.assertFalse(any(call[0] == 'install' for call in manager.calls))
        code, result = self.invoke(['--packages-install', 'gdal-bin', '--confirm'], manager)
        self.assertEqual((code, result['status']), (0, 'PACKAGES_INSTALLED'))
        self.assertEqual(result['versions'], {'gdal-bin': '2.0'})

    def test_names_are_bounded_and_modes_and_unrelated_targets_are_rejected(self):
        for arguments in (['--packages-plan', '--bad'], ['--packages-plan', 'x;touch'],
                          ['--packages-plan', 'x=1'], ['--packages-plan', 'local.deb'],
                          ['--packages-search', '.*'], ['--packages-plan', *[f'package-{i}' for i in range(33)]],
                          ['--packages-plan', 'gdal-bin', '--values', '{}'],
                          ['--packages-plan', 'gdal-bin', '--start'], ['--packages-catalog', '--config', '-'],
                          ['--packages-catalog', '--firewall-status'], ['--packages-catalog', '--banip-status'],
                          ['--packages-catalog', '--text']):
            manager = PackageFixture()
            code, result = self.invoke(arguments, manager)
            self.assertEqual((code, result['code']), (2, 'INVALID_ARGUMENT'))
            self.assertEqual(manager.calls, [])

    def test_empty_search_cannot_fall_through_to_a_readonly_mode(self):
        with patch('server_setting.__main__.collect', return_value={'modules': {}}):
            code, result = self.invoke(['--packages-search', '', '--status-json'], PackageFixture())
        self.assertEqual((code, result.get('code')), (2, 'INVALID_ARGUMENT'))


class PackagesUiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)

    async def until(self, pilot, condition):
        for _ in range(150):
            await pilot.pause(.02)
            if condition():
                return
        self.fail('Package UI did not settle')

    async def button(self, app, pilot, identifier):
        app.screen.query_one('#' + identifier).focus()
        await pilot.press('enter')
        await pilot.pause(.05)

    def checkbox(self, app, label):
        from textual.widgets import Checkbox
        return next(box for box in app.query(Checkbox) if label in str(box.label))

    async def test_catalog_versions_missing_selection_cancel_and_confirm_refresh(self):
        from server_setting.app import SettingApp, RootReview
        from textual.widgets import TabbedContent, TextArea
        manager = PackageFixture()
        app = SettingApp(root_manager=UiManager(), firewall_manager=FirewallFixture(),
                         banip_manager=BanIPFixture(), package_manager=manager)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(.05)
            app.query_one('#pages', TabbedContent).active = 'tab-packages'
            await pilot.pause(.05)
            await self.until(pilot, lambda: len(list(app.query('Checkbox'))) >= 4)
            self.assertEqual(manager.calls, [])
            self.assertIn('1.0', str(self.checkbox(app, 'proj-bin').label))
            self.assertIn('2.0', str(self.checkbox(app, 'gdal-bin').label))
            for label in ('proj-bin', 'JSON', 'FeatherMountain SQLite'):
                self.assertTrue(self.checkbox(app, label).disabled)
            self.checkbox(app, 'gdal-bin').value = True
            await self.button(app, pilot, 'packages-install')
            await self.until(pilot, lambda: isinstance(app.screen, RootReview))
            detail = app.screen.query_one('#root-review-detail', TextArea).text
            for expected in ('gdal-bin', 'libfixture1', '2.0', '1048576', '4194304', 'postinst', 'restart'):
                self.assertIn(expected, detail)
            await pilot.press('escape')
            await self.until(pilot, lambda: not app.root_busy)
            self.assertEqual(manager.calls, [('plan', ['gdal-bin'])])
            await self.button(app, pilot, 'packages-install')
            await self.until(pilot, lambda: isinstance(app.screen, RootReview))
            await self.button(app, pilot, 'root-confirm')
            await self.until(pilot, lambda: not app.root_busy)
            self.assertIn('PACKAGES_INSTALLED', app.query_one('#out-packages', TextArea).text)
            self.assertTrue(self.checkbox(app, 'gdal-bin').disabled)
            self.assertFalse(self.checkbox(app, 'gdal-bin').value)
            self.assertEqual([call[0] for call in manager.calls], ['plan', 'plan', 'install'])

    async def test_search_refreshes_installed_status_and_network_pending_blocks_install(self):
        from server_setting.app import SettingApp, RootReview
        from textual.widgets import Button, Input, TabbedContent, TextArea
        manager = PackageFixture()
        manager.search_hits += [{'name': f'package-{index}', 'description': 'Other result'} for index in range(39)]
        banip = BanIPFixture()
        banip.pending = {'transaction_id': 'pending-banip', 'remaining_seconds': 120, 'deadline_boottime': 100000}
        app = SettingApp(root_manager=UiManager(), firewall_manager=FirewallFixture(), banip_manager=banip, package_manager=manager)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause(.05)
            app.query_one('#pages', TabbedContent).active = 'tab-packages'
            await pilot.pause(.05)
            app.query_one('#packages-query', Input).value = 'JSON processor'
            await self.button(app, pilot, 'packages-search')
            await self.until(pilot, lambda: not app.root_busy)
            self.assertIn('SEARCHED', app.query_one('#out-packages', TextArea).text)
            self.assertFalse(self.checkbox(app, 'jq').value)
            self.assertFalse(self.checkbox(app, 'jq').disabled)
            self.assertTrue(app.query_one('#packages-install', Button).disabled)
            self.assertFalse(app.query_one('#packages-refresh', Button).disabled)
            self.assertEqual(manager.calls, [('search', 'JSON processor')])
            banip.pending = None  # Another SSH session resolved the backend transaction.
            await app.action_refresh()
            await pilot.pause(.05)
            self.checkbox(app, 'jq').value = True
            await self.button(app, pilot, 'packages-install')
            await self.until(pilot, lambda: isinstance(app.screen, RootReview))
            await self.button(app, pilot, 'root-confirm')
            await self.until(pilot, lambda: not app.root_busy)
            self.assertTrue(self.checkbox(app, 'jq').disabled)
            self.assertFalse(self.checkbox(app, 'jq').value)
            self.assertEqual(manager.calls[-1][1]['names'], ['jq'])

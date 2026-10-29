"""Firewall editing workflow; fake manager boundaries never touch host rules."""
import asyncio
import copy
import unittest
from unittest.mock import patch

from test_firewall_ui import FirewallFixture
from test_root_ui import UiManager


class RecordingFirewall(FirewallFixture):
    def __init__(self):
        super().__init__()
        self.plans = []

    def plan(self, config):
        self.plans.append(copy.deepcopy(config))
        return super().plan(config)


class FirewallEditorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 2
        for target, value in (
                ('server_setting.release_update.ReleaseManager.status',
                 {'supported': False, 'managed': False, 'installed': None,
                  'previous': None, 'pending': None, 'detail': 'Outside this fixture.'}),
                ('server_setting.root_network.NetworkManager.status',
                 {'supported': False, 'pending': None, 'interfaces': [],
                  'config': None, 'detail': 'Outside this fixture.'})):
            observer = patch(target, return_value=value)
            observer.start()
            self.addCleanup(observer.stop)

    async def until(self, pilot, condition):
        for _ in range(150):
            await pilot.pause(.02)
            if condition():
                return
        self.fail('Firewall editor did not settle')

    async def button(self, app, pilot, identifier):
        app.screen.query_one('#' + identifier).focus()
        await pilot.press('enter')
        await pilot.pause()

    async def show_firewall(self, app, pilot):
        from textual.widgets import TabbedContent
        await self.until(pilot, lambda: not app.root_busy)
        app.query_one('#pages', TabbedContent).active = 'tab-firewall'
        await pilot.pause()

    async def test_separator_help_remains_when_source_field_is_filled(self):
        from server_setting.app import SettingApp
        from textual.widgets import Input, Select, Static
        manager = RecordingFirewall()
        app = SettingApp(root_manager=UiManager(), firewall_manager=manager)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.show_firewall(app, pilot)
            app.query_one('#fw-service', Select).value = 'mariadb'
            await pilot.pause()
            app.query_one('#fw-sources', Input).value = '192.168.0.0/16,111.246.98.67/32'
            help_text = str(app.query_one('#fw-help', Static).render())
            self.assertIn('192.168.0.0/16,111.246.98.67/32', help_text)
            for action in ('Update service draft', 'Save recipe', 'Review & apply'):
                self.assertIn(action, help_text)
            self.assertEqual(manager.calls, [])
            self.assertEqual(manager.plans, [])

    async def test_saved_recipe_warns_until_explicitly_loaded(self):
        from server_setting.app import SettingApp
        from server_setting.firewall_rules import default_config
        from textual.widgets import Static
        manager = RecordingFirewall()
        manager.saved = default_config()
        next(item for item in manager.saved['services'] if item['id'] == 'http')['enabled'] = True
        app = SettingApp(root_manager=UiManager(), firewall_manager=manager)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.show_firewall(app, pilot)
            state = str(app.query_one('#fw-draft-state', Static).render())
            self.assertIn('not loaded', state.lower())
            self.assertIn('Load saved', state)
            self.assertFalse(any(item['enabled'] for item in app.fw_draft['services']))
            self.assertEqual(manager.calls, [])
            await self.button(app, pilot, 'fw-load')
            await self.until(pilot, lambda: not app.root_busy)
            self.assertEqual(app.fw_draft, manager.saved)
            self.assertNotIn('not loaded',
                             str(app.query_one('#fw-draft-state', Static).render()).lower())
            self.assertEqual(manager.calls, [])
            self.assertEqual(manager.plans, [])

    async def test_multiple_sources_and_services_are_saved_then_applied_together_once(self):
        from server_setting.app import RootReview, SettingApp
        from textual.widgets import Input, Select, Switch
        manager = RecordingFirewall()
        app = SettingApp(root_manager=UiManager(), firewall_manager=manager)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.show_firewall(app, pilot)
            app.query_one('#fw-service', Select).value = 'mariadb'
            await pilot.pause()
            app.query_one('#fw-sources', Input).value = '192.168.0.0/16,111.246.98.67/32'
            app.query_one('#fw-enabled', Switch).value = True
            await self.button(app, pilot, 'fw-update')
            app.query_one('#fw-service', Select).value = 'http'
            await pilot.pause()
            app.query_one('#fw-enabled', Switch).value = True
            await self.button(app, pilot, 'fw-update')
            self.assertEqual(manager.calls, [])
            self.assertEqual(manager.plans, [])
            await self.button(app, pilot, 'fw-save')
            await self.until(pilot, lambda: not app.root_busy)
            saved = {item['id']: item for item in manager.saved['services']}
            self.assertEqual(set(saved['mariadb']['sources']),
                             {'192.168.0.0/16', '111.246.98.67/32'})
            self.assertTrue(saved['mariadb']['enabled'])
            self.assertTrue(saved['http']['enabled'])
            self.assertEqual([call[0] for call in manager.calls], ['save'])
            self.assertEqual(manager.plans, [])
            await self.button(app, pilot, 'fw-apply')
            await self.until(pilot, lambda: isinstance(app.screen, RootReview))
            self.assertEqual(len(manager.plans), 1)
            self.assertEqual(manager.plans[0], manager.saved)
            self.assertEqual([call[0] for call in manager.calls], ['save'])
            await self.button(app, pilot, 'root-confirm')
            await self.until(pilot, lambda: not app.root_busy)
            self.assertEqual([call[0] for call in manager.calls], ['save', 'apply'])
            self.assertEqual(manager.calls[-1][1]['config'], manager.saved)
            self.assertEqual(len(manager.plans), 1)
            self.assertEqual(manager.pending['status'], 'AWAITING_NEW_SSH')

    async def test_deny_services_button_only_edits_draft_and_keeps_ssh_rescue(self):
        from server_setting.app import SettingApp
        from server_setting.firewall_rules import render
        from textual.widgets import Button, Switch
        manager = RecordingFirewall()
        app = SettingApp(root_manager=UiManager(), firewall_manager=manager)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.show_firewall(app, pilot)
            app.query_one('#fw-enabled', Switch).value = True
            await self.button(app, pilot, 'fw-update')
            self.assertTrue(any(item['enabled'] for item in app.fw_draft['services']))
            self.assertEqual(str(app.query_one('#fw-reset', Button).label), 'Deny services draft')
            await self.button(app, pilot, 'fw-reset')
            self.assertTrue(app.fw_draft['services'])
            self.assertFalse(any(item['enabled'] for item in app.fw_draft['services']))
            self.assertEqual(manager.calls, [])
            self.assertEqual(manager.plans, [])
            await self.button(app, pilot, 'fw-preview')
            await self.until(pilot, lambda: not app.root_busy)
            self.assertEqual(len(manager.plans), 1)
            self.assertEqual(manager.calls, [])
            compiled = render(manager.plans[0],
                              {'port': 2222, 'sources': ['192.0.2.99/32']})
            self.assertIn('policy drop;', compiled)
            self.assertIn('192.0.2.99/32', compiled)
            self.assertIn('tcp dport 2222', compiled)
            self.assertIn('mandatory SSH rescue', compiled)
            self.assertNotIn('service:', compiled)

"""Explicit Apache handler review/cancel/apply/restore UI boundaries."""
import asyncio
import copy
import unittest
from unittest.mock import patch

from test_root_ui import UiManager


class HandlerFixture:
    def __init__(self):
        self.calls = []

    def status(self):
        self.calls.append('status')
        return {'supported': True, 'event_enabled': False, 'detail': 'Native handler fixture.'}

    def plan(self):
        self.calls.append('plan')
        return {'module': 'apache-fpm', 'action': 'apply', 'target': {'version': '8.5'},
                'links': [{'path': '/etc/apache2/mods-enabled/mpm_event.load',
                           'before': None, 'after': '../mods-available/mpm_event.load'}],
                'notes': ['Restart Apache after a local FPM request; HTTP application unverified.'],
                'fingerprint': 'reviewed-apply'}

    def apply(self, plan):
        if plan['fingerprint'] != 'reviewed-apply':
            raise ValueError('Apply must receive the reviewed plan')
        self.calls.append(('apply', copy.deepcopy(plan)))
        return {'module': 'apache-fpm', 'status': 'APACHE_FPM_APPLIED'}

    def restore_plan(self):
        self.calls.append('restore-plan')
        return {'module': 'apache-fpm', 'action': 'restore', 'target': {'version': '8.5'},
                'notes': ['Restore the previous aliases and service state.'],
                'fingerprint': 'reviewed-restore'}

    def restore(self, plan):
        if plan['fingerprint'] != 'reviewed-restore':
            raise ValueError('Restore must receive the reviewed plan')
        self.calls.append(('restore', copy.deepcopy(plan)))
        return {'module': 'apache-fpm', 'status': 'APACHE_FPM_RESTORED'}


class ApacheHandlerUiTests(unittest.IsolatedAsyncioTestCase):
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
        self.fail('Apache handler UI did not settle')

    async def button(self, app, pilot, identifier):
        app.screen.query_one('#' + identifier).focus()
        await pilot.press('enter')
        await pilot.pause()

    async def test_status_and_cancel_never_activate_and_only_confirm_applies_or_restores(self):
        from server_setting.app import RootReview, SettingApp
        from textual.widgets import TabbedContent, TextArea
        handler = HandlerFixture()
        app = SettingApp(root_manager=UiManager(), apache_fpm_manager=handler)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            self.assertEqual(handler.calls, [])
            app.query_one('#pages', TabbedContent).active = 'tab-apache'
            await pilot.pause()
            self.assertEqual(handler.calls, [])
            await self.button(app, pilot, 'apache-fpm-status')
            await self.until(pilot, lambda: not app.root_busy)
            self.assertEqual(handler.calls, ['status'])
            self.assertIn('Native handler fixture', app.query_one('#out-apache-fpm', TextArea).text)
            await self.button(app, pilot, 'apache-fpm-apply')
            await self.until(pilot, lambda: isinstance(app.screen, RootReview))
            self.assertIn('HTTP', app.screen.query_one('#root-review-detail', TextArea).text)
            await pilot.press('escape')
            await self.until(pilot, lambda: not app.root_busy)
            self.assertEqual(handler.calls, ['status', 'plan'])
            await self.button(app, pilot, 'apache-fpm-apply')
            await self.until(pilot, lambda: isinstance(app.screen, RootReview))
            self.assertFalse(any(isinstance(call, tuple) for call in handler.calls))
            await self.button(app, pilot, 'root-confirm')
            await self.until(pilot, lambda: not app.root_busy)
            mutations = [call for call in handler.calls if isinstance(call, tuple)]
            self.assertEqual([call[0] for call in mutations], ['apply'])
            self.assertIn('APACHE_FPM_APPLIED', app.query_one('#out-apache-fpm', TextArea).text)
            await self.button(app, pilot, 'apache-fpm-restore')
            await self.until(pilot, lambda: isinstance(app.screen, RootReview))
            await pilot.press('escape')
            await self.until(pilot, lambda: not app.root_busy)
            self.assertEqual([call[0] for call in handler.calls if isinstance(call, tuple)], ['apply'])
            await self.button(app, pilot, 'apache-fpm-restore')
            await self.until(pilot, lambda: isinstance(app.screen, RootReview))
            await self.button(app, pilot, 'root-confirm')
            await self.until(pilot, lambda: not app.root_busy)
            self.assertEqual([call[0] for call in handler.calls if isinstance(call, tuple)], ['apply', 'restore'])
            self.assertIn('APACHE_FPM_RESTORED', app.query_one('#out-apache-fpm', TextArea).text)

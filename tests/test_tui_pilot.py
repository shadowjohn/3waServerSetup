import importlib.util
import unittest
import asyncio

HAS_TEXTUAL = importlib.util.find_spec('textual') is not None


@unittest.skipUnless(HAS_TEXTUAL, 'Textual missing; UI execution NOT VERIFIED')
class PilotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Windows headless rendering may take >100ms under instrumented execution.
        asyncio.get_running_loop().slow_callback_duration = 2
        # These cases cover the ordinary-user view even when the suite runs as root.
        from unittest.mock import patch
        for target in ('server_setting.probes.root_management_available',
                       'server_setting.app.root_management_available',
                       'server_setting.app.update_management_available'):
            actor = patch(target, return_value=False)
            actor.start()
            self.addCleanup(actor.stop)

    async def test_next_page_from_button_focus_does_not_bounce(self):
        from server_setting.app import SettingApp
        from textual.widgets import Button, TabbedContent
        app = SettingApp()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app.query_one('#refresh', Button).focus()
            await pilot.pause()
            await pilot.press('ctrl+right')
            await pilot.pause()
            self.assertEqual(app.query_one('#pages', TabbedContent).active, 'tab-apache')

    async def test_previous_page_from_button_focus_does_not_bounce(self):
        from server_setting.app import SettingApp
        from textual.widgets import Button, TabbedContent
        app = SettingApp()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app.query_one('#pages', TabbedContent).active = 'tab-apache'
            await pilot.pause()
            app.query_one('#preview-apache', Button).focus()
            await pilot.pause()
            await pilot.press('ctrl+left')
            await pilot.pause()
            self.assertEqual(app.query_one('#pages', TabbedContent).active, 'tab-overview')

    async def test_software_update_page_participates_in_both_keyboard_directions(self):
        from server_setting.app import SettingApp
        from textual.widgets import Button, TabbedContent
        app = SettingApp()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            pages = app.query_one('#pages', TabbedContent)
            pages.active = 'tab-firewall'
            for source, key, destination in (
                    ('fw-preview', 'ctrl+right', 'tab-update'),
                    ('update-check', 'ctrl+left', 'tab-firewall'),
                    ('fw-preview', 'ctrl+right', 'tab-update'),
                    ('update-check', 'ctrl+right', 'tab-sqlite'),
                    ('sqlite-run', 'ctrl+left', 'tab-update')):
                await pilot.pause()
                app.query_one('#'+source, Button).focus()
                await pilot.press(key)
                await pilot.pause()
                self.assertEqual(pages.active, destination)

    async def test_tabs_preview_resize_and_no_apply(self):
        from server_setting.app import SettingApp
        from textual.widgets import Button, TabbedContent, TextArea
        app = SettingApp()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            self.assertTrue(app.query_one('#mascot').display)
            self.assertFalse(any('apply' in (button.id or '') for button in app.query(Button)))
            pages = app.query_one('#pages', TabbedContent)
            pages.active = 'tab-php'
            app.query_one('#preview-php', Button).focus()
            await pilot.press('enter')
            await pilot.pause()
            self.assertIn('PREVIEW ONLY', app.query_one('#out-php', TextArea).text)
            pages.active = 'tab-firewall'
            await pilot.pause()
            app.query_one('#fw-preview', Button).focus()
            await pilot.press('enter')
            await pilot.pause()
            self.assertIn('OFFLINE TEXT ONLY', app.query_one('#fw-output', TextArea).text)
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            self.assertFalse(app.query_one('#mascot').display)
            await pilot.resize_terminal(140, 40)
            await pilot.pause()
            self.assertTrue(app.query_one('#mascot').display)
            await pilot.press('ctrl+q')

    async def test_keyboard_custom_firewall_and_invalid_source(self):
        from server_setting.app import SettingApp
        from textual.widgets import Button, Input, Select, Switch, TabbedContent, TextArea
        app = SettingApp()
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            pages = app.query_one('#pages', TabbedContent)
            self.assertEqual(pages.active, 'tab-overview')
            await pilot.press('ctrl+right')
            self.assertEqual(pages.active, 'tab-apache')
            await pilot.press('ctrl+left')
            self.assertEqual(pages.active, 'tab-overview')
            pages.active = 'tab-firewall'
            await pilot.pause()
            count = len(app.fw_config['rules'])
            app.query_one('#fw-ports', Input).value = '45678'
            app.query_one('#fw-protocol', Select).value = 'udp'
            app.query_one('#fw-enabled', Switch).value = True
            app.query_one('#fw-add', Button).focus()
            await pilot.press('enter')
            await pilot.pause()
            self.assertIn('INVALID DRAFT', app.query_one('#fw-output', TextArea).text)
            self.assertEqual(len(app.fw_config['rules']), count)
            app.query_one('#fw-source-mode', Select).value = 'public'
            app.query_one('#fw-add', Button).focus()
            await pilot.press('enter')
            await pilot.pause()
            self.assertIn('-p udp -s 0.0.0.0/0 --dport 45678 -j ACCEPT', app.query_one('#fw-output', TextArea).text)
            await pilot.press('tab')
            self.assertEqual(app.focused.id, 'fw-preview')
            await pilot.press('ctrl+q')

    async def test_pixel_mascot_keeps_main_form_width_and_focus(self):
        import os
        from unittest.mock import patch
        from server_setting.app import SettingApp
        from textual.widgets import Static
        with patch.dict(os.environ):
            os.environ.pop('NO_COLOR', None)
            app = SettingApp()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            mascot = app.query_one('#mascot', Static)
            self.assertTrue(mascot.display)
            self.assertTrue(any('\u2801' <= char <= '\u28ff' for char in str(mascot.render())))
            self.assertGreaterEqual(app.query_one('#main').size.width, 100)
            self.assertFalse(mascot.can_focus)
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            self.assertFalse(mascot.display)
            self.assertGreaterEqual(app.query_one('#main').size.width, 78)
            await pilot.press('ctrl+q')

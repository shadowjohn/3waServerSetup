"""Native SQLite diagnostics run only after the selected button is pressed."""
import asyncio
import unittest
from unittest.mock import patch

from test_root_ui import UiManager


class SQLiteUiTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_driver_and_module_workflows_do_not_run_at_startup(self):
        from server_setting.app import SettingApp
        from textual.widgets import Input, TabbedContent, TextArea
        asyncio.get_running_loop().slow_callback_duration = 2
        with patch('server_setting.app.sqlite_smoke', return_value='PDO and RTree PASS') as basic, \
                patch('server_setting.diagnostics.sqlite_spatial_smoke', return_value='CLI ONLY; FPM UNVERIFIED') as spatial:
            app = SettingApp(root_manager=UiManager())
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                app.query_one('#pages', TabbedContent).active = 'tab-sqlite'
                await pilot.pause()
                basic.assert_not_called()
                spatial.assert_not_called()
                selected = '/usr/lib/x86_64-linux-gnu/mod_spatialite.so'
                app.query_one('#sqlite-module', Input).value = selected
                for identifier in ('sqlite-run', 'sqlite-spatial', 'sqlite-acceptance'):
                    app.query_one('#' + identifier).focus()
                    await pilot.press('enter')
                    for _ in range(100):
                        await pilot.pause(.02)
                        if not app.root_busy:
                            break
                    self.assertFalse(app.root_busy)
                basic.assert_called_once_with()
                self.assertEqual(spatial.call_count, 2)
                self.assertEqual(spatial.call_args_list[0].args, (selected,))
                self.assertEqual(spatial.call_args_list[0].kwargs, {'acceptance': False})
                self.assertEqual(spatial.call_args_list[1].args, (selected,))
                self.assertEqual(spatial.call_args_list[1].kwargs, {'acceptance': True})
                self.assertIn('FPM UNVERIFIED', app.query_one('#sqlite-output', TextArea).text)


if __name__ == '__main__':
    unittest.main()

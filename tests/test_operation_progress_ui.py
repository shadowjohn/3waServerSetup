"""The live display must update before a long command returns."""
import asyncio
import threading
import unittest
from unittest.mock import patch

from test_root_ui import UiManager


class ProgressUiTests(unittest.IsolatedAsyncioTestCase):
    async def test_safe_phase_visible_while_install_is_still_running(self):
        from server_setting.app import SettingApp
        from server_setting.operation_progress import emit_progress
        from textual.widgets import Static
        release = threading.Event()
        entered = threading.Event()
        manager = UiManager()

        def install(module):
            emit_progress('compile', command='make', output_bytes=8192, idle_seconds=2)
            entered.set()
            if not release.wait(15):
                raise RuntimeError('Progress fixture was not released')
            return {'status': 'INSTALLED', 'module': module}

        with patch.object(manager, 'install', side_effect=install):
            app = SettingApp(root_manager=manager)
            async with app.run_test(size=(100, 35)) as pilot:
                await pilot.pause()
                app._start_root_busy()
                task = asyncio.create_task(app._execute_root_operation('apache', 'install', {}))
                try:
                    for _ in range(100):
                        await pilot.pause(.02)
                        if entered.is_set():
                            break
                    self.assertTrue(entered.is_set())
                    await pilot.pause(.1)
                    self.assertFalse(task.done())
                    live = app.query_one('#operation-progress', Static)
                    self.assertTrue(live.display)
                    self.assertIn('compile', str(live.render()))
                    self.assertIn('make', str(live.render()))
                    self.assertIn('8.0 KiB', str(live.render()))
                finally:
                    release.set()
                    await task
                self.assertFalse(app.query_one('#operation-progress', Static).display)


if __name__ == '__main__':
    unittest.main()

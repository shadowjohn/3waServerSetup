"""Real child processes prove progress stays live without exposing diagnostics."""
import asyncio
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

try:
    from server_setting.operation_progress import progress_context, phase_context, emit_progress, run_captured
except ImportError:
    progress_context = phase_context = emit_progress = run_captured = None


class OperationProgressTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(run_captured, 'Bounded live progress is not implemented')

    def test_output_is_reported_before_process_finishes_without_raw_text(self):
        events = []
        with progress_context(lambda event: events.append((time.monotonic(), event))):
            result = run_captured([sys.executable, '-c',
                'import sys,time;print("https://user:secret@private.invalid/credential",flush=True);'
                'sys.stderr.write("password=hidden\\n");sys.stderr.flush();time.sleep(.6)'],
                timeout=3, command='python')
        self.assertEqual(result.returncode, 0)
        self.assertIn('private.invalid', result.stdout)
        self.assertIn('hidden', result.stderr)
        active = [stamp for stamp, event in events if event['state'] == 'running' and event['output_bytes'] > 0]
        self.assertTrue(active, events)
        self.assertGreater(events[-1][0] - active[0], .2)
        self.assertEqual(events[-1][1]['state'], 'completed')
        for secret in ('private.invalid', 'credential', 'password', 'hidden', 'secret'):
            self.assertNotIn(secret, str(events))

    def test_stdout_and_stderr_are_drained_and_capture_tail_is_bounded(self):
        events = []
        with progress_context(events.append):
            result = run_captured([sys.executable, '-c',
                'import sys;sys.stdout.write("a"*200000+"stdout-tail");'
                'sys.stderr.write("b"*200000+"stderr-tail")'],
                timeout=3, command='python', maximum_output=4096)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.output_truncated)
        self.assertLessEqual(len(result.stdout.encode()) + len(result.stderr.encode()), 4096)
        self.assertTrue(result.stdout.endswith('stdout-tail'))
        self.assertTrue(result.stderr.endswith('stderr-tail'))
        self.assertEqual(events[-1]['output_bytes'], 400022)

    def test_timeout_reports_failure_and_reaps_the_child(self):
        events = []
        children = []
        native_popen = subprocess.Popen
        def launch(*args, **kwargs):
            child = native_popen(*args, **kwargs)
            children.append(child)
            return child
        started = time.monotonic()
        with progress_context(events.append), patch('server_setting.operation_progress.subprocess.Popen', side_effect=launch), \
                self.assertRaises(subprocess.TimeoutExpired):
            run_captured([sys.executable, '-c', 'import time;time.sleep(5)'], timeout=.2, command='python')
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(events[-1]['state'], 'failed')
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].returncode)

    def test_nonzero_exit_reports_failed_while_diagnostics_stay_private(self):
        events = []
        with progress_context(events.append):
            result = run_captured([sys.executable, '-c',
                'import sys;sys.stderr.write("private failure password=hidden");sys.exit(7)'],
                timeout=3, command='python')
        self.assertEqual(result.returncode, 7)
        self.assertIn('password=hidden', result.stderr)
        self.assertEqual(events[-1]['state'], 'failed')
        self.assertNotIn('password', str(events))

    def test_interruption_kills_and_reaps_the_live_child_promptly(self):
        children = []
        native_popen = subprocess.Popen
        def launch(*args, **kwargs):
            child = native_popen(*args, **kwargs)
            children.append(child)
            return child
        def interrupted(event):
            if event['state'] == 'running' and event['output_bytes'] > 0:
                raise KeyboardInterrupt()
        started = time.monotonic()
        with progress_context(interrupted), patch('server_setting.operation_progress.subprocess.Popen', side_effect=launch), \
                self.assertRaises(KeyboardInterrupt):
            run_captured([sys.executable, '-c', 'import time;print("started",flush=True);time.sleep(3)'],
                         timeout=5, command='python')
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].returncode)
        self.assertLess(time.monotonic() - started, 2)

    def test_silent_child_has_running_heartbeat_and_phase_follows_to_thread(self):
        async def operation():
            with progress_context(events.append), phase_context('compile'):
                return await asyncio.to_thread(run_captured,
                    [sys.executable, '-c', 'import time;time.sleep(.5)'], timeout=3, command='python')
        events = []
        result = asyncio.run(operation())
        self.assertEqual(result.returncode, 0)
        self.assertTrue(all(event['phase'] == 'compile' for event in events))
        self.assertGreaterEqual(len([event for event in events if event['state'] == 'running']), 2)
        self.assertTrue(any(event['idle_seconds'] >= .2 for event in events))

    def test_sink_failure_never_changes_native_success_and_context_does_not_leak(self):
        def broken(_):
            raise RuntimeError('UI unavailable')
        with progress_context(broken):
            result = run_captured([sys.executable, '-c', 'print("ok")'], timeout=3, command='python')
        self.assertEqual((result.returncode, result.stdout.strip()), (0, 'ok'))
        events = []
        with progress_context(events.append):
            emit_progress('verify')
            with progress_context(lambda _: None):
                emit_progress('activate')
            emit_progress('restore')
        self.assertEqual([event['phase'] for event in events], ['verify', 'restore'])

    def test_apt_download_summary_is_numeric_only_and_comes_from_native_output(self):
        events = []
        with progress_context(events.append):
            run_captured([sys.executable, '-c',
                'print("Get:1 https://user:secret@private.invalid/ubuntu resolute package [14 kB]");'
                'print("Fetched 1234 kB in 2s (617 kB/s)")'], timeout=3, command='apt-get')
        self.assertEqual(events[-1]['download_bytes'], 1234000)
        self.assertNotIn('private.invalid', str(events))
        self.assertNotIn('secret', str(events))


if __name__ == '__main__':
    unittest.main()

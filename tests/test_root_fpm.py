import importlib
import json
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch


class FpmProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fpm = importlib.import_module('server_setting.root_fpm')

    def test_fastcgi_response_uses_framing_and_rejects_nonzero_exit(self):
        evidence = {'sapi': 'fpm-fcgi', 'values': {'memory_limit': '128M'}, 'scanned': 'x', 'extensions': {}}
        body = b'Content-type: application/json\r\n\r\n' + json.dumps(evidence).encode()
        def exchange(exitcode=0):
            client, server = socket.socketpair()
            def response():
                with server:
                    server.recv(65536)
                    for payload in (body[:20], body[20:]):
                        server.sendall(struct.pack('!BBHHBB', 1, 6, 1, len(payload), 0, 0) + payload)
                    end = struct.pack('!IB3s', exitcode, 0, b'\0' * 3)
                    server.sendall(struct.pack('!BBHHBB', 1, 3, 1, len(end), 0, 0) + end)
            thread = threading.Thread(target=response)
            thread.start()
            try:
                with client:
                    return self.fpm._exchange(client, '/run/probe.php')
            finally:
                thread.join(timeout=2)
        self.assertEqual(exchange(), evidence)
        with self.assertRaisesRegex(ValueError, 'FPM|FastCGI'):
            exchange(1)

    def test_probe_uses_private_nonce_file_with_fixed_fields_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as directory:
            class Host:
                root = Path(directory)
                def path(self, value):
                    return self.root / value.lstrip('/')
            host = Host()
            host.path('/run').mkdir()
            seen = []
            def request(socket_path, script_path):
                file = host.path(script_path)
                seen.append(file)
                self.assertTrue(file.is_file())
                self.assertIn('ini_get', file.read_text())
                self.assertNotIn('phpinfo', file.read_text())
                self.assertNotIn('$_', file.read_text())
                if os.name != 'nt':
                    self.assertEqual(file.stat().st_mode & 0o777, 0o644)
                    self.assertEqual(file.parent.stat().st_mode & 0o777, 0o711)
                return {'sapi': 'fpm-fcgi', 'values': {}, 'scanned': '', 'extensions': {}}
            with patch('server_setting.root_fpm._request', side_effect=request):
                self.fpm.probe_values(host, '/run/php/php8.5-fpm.sock')
            self.assertEqual(len(seen), 1)
            self.assertFalse(seen[0].parent.exists())
            self.assertEqual(list(host.path('/run').iterdir()), [])

    def test_fastcgi_params_never_mutate_worker_ini_with_any_overrides(self):
        params = self.fpm._params('/run/probe.php')
        self.assertNotIn(b'PHP_ADMIN_VALUE', params)
        self.assertNotIn(b'PHP_VALUE', params)
        self.assertNotIn(b'auto_prepend_file', params)
        self.assertNotIn(b'auto_append_file', params)
        for value in (b'memory_limit=', b'date.timezone=', b'expose_php='):
            self.assertNotIn(value, params)

    def test_failed_socket_request_also_removes_probe_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            class Host:
                root = Path(directory)
                def path(self, value):
                    return self.root / value.lstrip('/')
            host = Host()
            host.path('/run').mkdir()
            with patch('server_setting.root_fpm._request', side_effect=OSError('socket refused')):
                with self.assertRaises(OSError):
                    self.fpm.probe_values(host, '/run/php/php8.5-fpm.sock')
            self.assertEqual(list(host.path('/run').iterdir()), [])


if __name__ == '__main__':
    unittest.main()

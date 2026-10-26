"""Exercise the literal isolated system-Python reader used by Netplan/cloud-init."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from server_setting.root_network import _YAML_READER


SYSTEM_PYTHON = '/usr/bin/python3'


@unittest.skipUnless(os.name == 'posix' and Path(SYSTEM_PYTHON).is_file(), 'Requires native POSIX system Python and Netplan PyYAML')
class NetworkYamlTests(unittest.TestCase):
    def parse(self, text, *, cloud=True, valid=True):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'source.yaml'
            path.write_text(text, encoding='utf-8')
            args = [SYSTEM_PYTHON, '-I', '-c', _YAML_READER]
            if cloud:
                args.append('--cloud-network')
            result = subprocess.run([*args, str(path)], capture_output=True, text=True, timeout=10)
        if not valid:
            self.assertNotEqual(result.returncode, 0, 'Ambiguous YAML was accepted: ' + result.stdout[:200])
            return
        self.assertEqual(result.returncode, 0, result.stderr[-3000:])
        self.assertLess(len(result.stdout), 65536)
        return json.loads(result.stdout)

    def test_cloud_stock_logging_aliases_are_not_constructed_or_returned(self):
        text = '''log_base: &log_base |
  [loggers]
  keys=root,cloudinit
log_cfgs:
  - [*log_base]
network:
  config: disabled
'''
        self.assertEqual(self.parse(text), {'network': {'config': 'disabled'}})
        self.assertEqual(self.parse(text.replace('network:\n  config: disabled\n', '')), {})

    def test_netplan_keeps_rejecting_all_aliases(self):
        self.parse('network: &net {version: 2}\ncopy: *net\n', cloud=False, valid=False)
        self.parse('logging: &log [one]\nother: *log\nnetwork: {version: 2}\n', cloud=False, valid=False)
        self.assertEqual(self.parse('network: {version: 2}\n', cloud=False), {'network': {'version': 2}})

    def test_cloud_network_shared_nodes_and_cycles_are_rejected(self):
        examples = (
            'network: &net {config: disabled}\nlogging: *net\n',
            'logging: &net {config: disabled}\nnetwork: *net\n',
            'network: {config: &config disabled}\nlogging: *config\n',
            'network: {a: &net {config: disabled}, b: *net}\n',
            'network: &net {self: *net}\n',
            'network: {&key config: disabled}\nlogging: *key\n',
            'key: &key network\n*key: {config: disabled}\n',
            '&key network: {config: disabled}\nlogging: *key\n',
        )
        for text in examples:
            with self.subTest(text=text):
                self.parse(text, valid=False)

    def test_cloud_root_keys_must_be_unique_strings_without_merge(self):
        examples = (
            'network: {config: disabled}\nnetwork: {config: enabled}\n',
            'logging: one\nlogging: two\n',
            '1: value\nnetwork: {config: disabled}\n',
            '? [key]\n: value\n',
            'base: &base {network: {config: disabled}}\n<<: *base\n',
            '"<<": {network: {config: disabled}}\n',
            '[network, config]\n',
            '!custom network: {config: disabled}\n',
        )
        for text in examples:
            with self.subTest(text=text):
                self.parse(text, valid=False)

    def test_cloud_network_retains_duplicate_merge_tag_and_nonfinite_rejection(self):
        examples = (
            'network: {config: disabled, config: enabled}\n',
            'network: {nested: {key: one, key: two}}\n',
            'network: {<<: {config: disabled}}\n',
            'network: !custom {config: disabled}\n',
            'network: {config: !custom disabled}\n',
            'network: {config: .nan}\n',
            'network: {config: .inf}\n',
            'network: {config: 2026-10-26}\n',
        )
        for text in examples:
            with self.subTest(text=text):
                self.parse(text, valid=False)

    def test_cloud_ignored_cycle_and_alias_dag_are_not_expanded(self):
        self.assertEqual(self.parse('logging: &cycle [*cycle]\nnetwork: {config: disabled}\n'), {'network': {'config': 'disabled'}})
        lines = ['logging_0: &n0 [log]']
        for index in range(1, 25):
            lines.append('logging_%d: &n%d [%s]' % (index, index, ', '.join(['*n%d' % (index - 1)] * 12)))
        lines.append('network: {config: disabled}')
        self.assertEqual(self.parse('\n'.join(lines)), {'network': {'config': 'disabled'}})

    def test_cloud_graph_nodes_edges_and_network_depth_are_bounded(self):
        too_many_nodes = 'network: {config: disabled}\nlogging:\n' + ''.join('  - node%d\n' % i for i in range(4200))
        self.assertLess(len(too_many_nodes.encode()), 65536)
        self.parse(too_many_nodes, valid=False)
        too_many_edges = 'network: {config: disabled}\nlogging: &log value\nother: [' + ','.join(['*log'] * 8200) + ']\n'
        self.assertLess(len(too_many_edges.encode()), 65536)
        self.parse(too_many_edges, valid=False)
        self.parse('network: ' + '[' * 32 + 'value' + ']' * 32, valid=False)
        expected = 'value'
        for _ in range(31):
            expected = [expected]
        self.assertEqual(self.parse('network: ' + '[' * 31 + 'value' + ']' * 31), {'network': expected})

    def test_cloud_empty_document_is_an_empty_projection(self):
        self.assertEqual(self.parse('# cloud-config\n'), {})


if __name__ == '__main__':
    unittest.main()

import copy
import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class FirewallPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = ROOT / 'firewall_plan.py'
        if not path.exists():
            raise AssertionError('Missing offline firewall planner')
        spec = importlib.util.spec_from_file_location('firewall_plan', path)
        cls.planner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.planner)

    def config(self):
        return {'schema': 1, 'blacklist_set': 'threewa_ban4',
                'ssh_rescue': {'port': 2222, 'sources': ['192.0.2.10/32']},
                'rules': [{'protocol': 'tcp', 'ports': [80, 443], 'sources': ['public']},
                          {'protocol': 'tcp', 'ports': [21, '65000-65100'], 'sources': ['198.51.100.0/24']}]}

    def test_candidate_preserves_order_and_scope(self):
        output = self.planner.render(self.config())
        self.assertLess(output.index('-i lo'), output.index('--match-set'))
        self.assertLess(output.index('--dport 2222'), output.index('--match-set'))
        self.assertLess(output.index('--match-set'), output.index('ESTABLISHED,RELATED'))
        self.assertIn('-p tcp -s 0.0.0.0/0 --dport 443 -j ACCEPT', output)
        self.assertIn('-p tcp -s 198.51.100.0/24 --dport 65000:65100 -j ACCEPT', output)
        self.assertTrue(output.endswith('-A THREEWA_INPUT4 -j DROP\nCOMMIT\n'))
        for forbidden in ['-F', '-X', '-P ', '-A INPUT', 'FORWARD', '*nat', 'sysctl']:
            self.assertNotIn(forbidden, output)

    def test_repeated_render_is_identical_and_no_duplicate_rules(self):
        config = self.config()
        config['rules'].append(copy.deepcopy(config['rules'][0]))
        config['rules'].append({'protocol': 'tcp', 'ports': [2222], 'sources': ['192.0.2.10/32']})
        output = self.planner.render(config)
        self.assertEqual(output, self.planner.render(config))
        lines = [line for line in output.splitlines() if line.startswith('-A ')]
        self.assertEqual(len(lines), len(set(lines)))

    def test_rejects_broad_or_missing_rescue(self):
        for sources in [[], ['0.0.0.0/0'], ['public'], ['::1/128'], ['192.0.2.1/24']]:
            config = self.config()
            config['ssh_rescue']['sources'] = sources
            with self.subTest(sources=sources), self.assertRaises(ValueError):
                self.planner.render(config)

    def test_rejects_unsafe_values(self):
        cases = [('blacklist_set', 'foo;reboot'), ('schema', True), ('extra', 'unknown')]
        for key, value in cases:
            config = self.config()
            config[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.planner.render(config)

    def test_limits_source_and_expanded_rule_count(self):
        config = self.config()
        config['rules'][0]['sources'] = [f'192.0.2.{n}/32' for n in range(65)]
        with self.assertRaises(ValueError):
            self.planner.render(config)
        config = self.config()
        config['rules'] = [{'protocol': 'tcp', 'ports': list(range(1, 129)),
                            'sources': [f'192.0.2.{n}/32' for n in range(64)]}]
        with self.assertRaises(ValueError):
            self.planner.render(config)
        for rule in [
            {'protocol': 'gre', 'ports': [80], 'sources': ['public']},
            {'protocol': 'tcp', 'ports': ['1-65536'], 'sources': ['public']},
            {'protocol': 'tcp', 'ports': [True], 'sources': ['public']},
            {'protocol': 'tcp', 'ports': [80], 'sources': ['::/0']},
            {'protocol': 'tcp', 'ports': [80], 'sources': ['192.0.2.7/24']},
        ]:
            config = self.config()
            config['rules'] = [rule]
            with self.subTest(rule=rule), self.assertRaises(ValueError):
                self.planner.render(config)

    def test_disabled_presets_do_not_open_services(self):
        config = self.planner.load_config(ROOT / 'config/firewall.example.json')
        self.assertTrue(config['rules'])
        self.assertTrue(all(rule.get('enabled') is False for rule in config['rules']))
        output = self.planner.render(config)
        self.assertNotIn('--dport 80 ', output)
        self.assertNotIn('--dport 443 ', output)
        config['rules'][0]['enabled'] = True
        self.assertIn('--dport 80 ', self.planner.render(config))

    def test_custom_udp_public_is_only_selected_port(self):
        config = self.config()
        config['rules'] = [{'enabled': True, 'protocol': 'udp', 'ports': [45678], 'sources': ['public']}]
        output = self.planner.render(config)
        self.assertIn('-p udp -s 0.0.0.0/0 --dport 45678 -j ACCEPT', output)
        self.assertNotIn('--dport 80 ', output)
        for line in output.splitlines():
            if '-s 0.0.0.0/0' in line:
                self.assertIn('--dport 45678 ', line)
        self.assertIn('-j DROP\nCOMMIT', output)

    def test_invalid_port_sources_and_enable_flags(self):
        bad_rules = [
            {'protocol': 'tcp', 'ports': [value], 'sources': ['public']}
            for value in [0, 65536, -1, '90-80', '1-65536', '80', ' 80 ', '80;echo']
        ]
        bad_rules += [{'protocol': 'tcp', 'ports': [80], 'sources': value}
                      for value in [[], [''], [' '], ['999.1.1.1'], ['192.0.2.0/99'], ['::1'], ['public', '192.0.2.1']]]
        bad_rules += [{'enabled': value, 'protocol': 'tcp', 'ports': [80], 'sources': ['public']}
                      for value in ['false', 0, None]]
        for rule in bad_rules:
            config = self.config()
            config['rules'] = [rule]
            with self.subTest(rule=rule), self.assertRaises(ValueError):
                self.planner.render(config)

    def test_ip_and_cidr_restricted_custom_range(self):
        config = self.config()
        config['rules'] = [{'protocol': 'tcp', 'ports': [1, 65535, '10000-10010'],
                            'sources': ['192.0.2.5', '198.51.100.0/24']}]
        output = self.planner.render(config)
        self.assertIn('-s 192.0.2.5/32 --dport 10000:10010', output)
        self.assertIn('-s 198.51.100.0/24 --dport 65535', output)

    def test_first_release_catalog_scope_and_ftp_passive(self):
        config = self.planner.load_config(ROOT / 'config/firewall.example.json')
        self.assertEqual({port for rule in config['rules'] for port in rule['ports']},
                         {80, 443, 22, 21, 445, 3306, '60000-60100'})
        self.assertNotIn('ping', config)
        self.assertTrue(all(rule['enabled'] is False for rule in config['rules']))
        ftp = next(rule for rule in config['rules'] if 21 in rule['ports'])
        passive = next(rule for rule in config['rules'] if '60000-60100' in rule['ports'])
        self.assertEqual(ftp['sources'], passive['sources'])
        ftp['enabled'] = passive['enabled'] = True
        output = self.planner.render(config)
        self.assertIn('--dport 60000:60100', output)
        self.assertNotIn('--dport 20 ', output)
        self.assertNotIn('--dport 139 ', output)
        config['ping'] = {'enabled': True, 'sources': ['public']}
        with self.assertRaises(ValueError):
            self.planner.render(config)


if __name__ == '__main__':
    unittest.main()

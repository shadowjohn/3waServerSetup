"""Compiler contract tests; native nft/SSH behavior is validated separately."""
import copy
import importlib.util
import unittest

if importlib.util.find_spec('server_setting.firewall_rules'):
    from server_setting import firewall_rules as rules
else:
    rules = None


class FirewallRulesTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(rules, 'Dual-stack firewall compiler is not implemented')
        self.rescue = {'port': 2222, 'sources': ['192.0.2.10/32', '2001:db8::10/128']}

    def config(self):
        return {'schema': 2, 'services': [
            {'id': 'http', 'enabled': True, 'protocol': 'tcp', 'ports': [443, 80], 'sources': ['public']},
            {'id': 'mariadb', 'enabled': True, 'protocol': 'tcp', 'ports': [3306], 'sources': ['198.51.100.0/24']},
            {'id': 'custom_udp', 'enabled': True, 'protocol': 'udp', 'ports': [5353], 'sources': ['2001:db8:1::/64']}]}

    def test_canonical_model_normalizes_ports_sources_and_does_not_mutate_input(self):
        config = self.config()
        config['services'][1]['ports'] = [3307, '3306-3306', 3306]
        config['services'][1]['sources'] = ['198.51.100.5', '198.51.100.0/24']
        before = copy.deepcopy(config)
        normalized = rules.normalize(config)
        self.assertEqual(config, before)
        self.assertEqual(normalized['services'][1]['ports'], [80, 443])
        database = next(service for service in normalized['services'] if service['id'] == 'mariadb')
        self.assertEqual(database['ports'], ['3306-3307'])
        self.assertEqual(database['sources'], ['198.51.100.0/24'])
        self.assertEqual(rules.normalize(normalized), normalized)

    def test_nft_output_is_dual_stack_default_deny_and_does_not_flush_foreign_tables(self):
        output = rules.render(self.config(), self.rescue)
        self.assertIn('table inet threewa_services', output)
        self.assertIn('comment "3waServerSetup firewall schema2"', output)
        self.assertIn('type filter hook input priority 10; policy drop;', output)
        self.assertIn('meta l4proto tcp tcp dport { 80, 443 } counter accept', output)
        self.assertIn('ip saddr { 198.51.100.0/24 } meta l4proto tcp tcp dport 3306 counter accept', output)
        self.assertNotIn('ip6 saddr', next(line for line in output.splitlines() if 'service:mariadb' in line))
        self.assertIn('ip6 saddr { 2001:db8:1::/64 } meta l4proto udp udp dport 5353 counter accept', output)
        for forbidden in ('flush ruleset', 'flush table', 'hook forward', 'hook output', 'hook prerouting', 'masquerade', 'ipset', 'ip6 nexthdr'):
            self.assertNotIn(forbidden, output)
        self.assertNotIn('delete table', output)
        replaced = rules.render(self.config(), self.rescue, table_exists=True)
        self.assertEqual(replaced.count('delete table inet threewa_services'), 1)
        self.assertLess(replaced.index('delete table'), replaced.index('table inet threewa_services {'))

    def test_disabled_service_has_no_new_connection_allow_but_rescue_survives(self):
        config = {'schema': 2, 'services': [
            {'id': 'ssh', 'enabled': False, 'protocol': 'tcp', 'ports': [2222], 'sources': []},
            {'id': 'mariadb', 'enabled': False, 'protocol': 'tcp', 'ports': [3306], 'sources': ['public']}]}
        output = rules.render(config, self.rescue)
        self.assertNotIn('service:ssh', output)
        self.assertNotIn('service:mariadb', output)
        self.assertNotIn('dport 3306 counter accept', output)
        self.assertIn('ip saddr { 192.0.2.10/32 } meta l4proto tcp tcp dport 2222 counter accept', output)
        self.assertIn('ip6 saddr { 2001:db8::10/128 } meta l4proto tcp tcp dport 2222 counter accept', output)
        self.assertLess(output.index('rescue IPv4'), output.index('ct state invalid'))
        self.assertLess(output.index('ct state invalid'), output.index('ct state { established, related }'))

    def test_mandatory_control_includes_ipv6_nd_pmtu_and_dhcp_clients(self):
        output = rules.render({'schema': 2, 'services': []}, self.rescue)
        self.assertIn('iifname "lo" counter accept', output)
        self.assertIn('packet-too-big', output)
        self.assertIn('destination-unreachable', output)
        self.assertIn('nd-neighbor-solicit', output)
        self.assertIn('nd-neighbor-advert', output)
        self.assertIn('ip6 hoplimit 255', output)
        self.assertIn('nd-router-advert', output)
        self.assertIn('mld-listener-query', output)
        self.assertIn('meta nfproto ipv4 meta l4proto udp udp sport 67 udp dport 68 counter accept', output)
        self.assertIn('meta nfproto ipv6 meta l4proto udp udp sport 547 udp dport 546 counter accept', output)
        self.assertNotIn('echo-request', output)

    def test_tcp_udp_ranges_and_both_source_families_render_without_cross_family_public(self):
        config = {'schema': 2, 'services': [{'id': 'ftp_pasv', 'enabled': True, 'protocol': 'tcp',
                    'ports': ['60000-60100'], 'sources': ['198.51.100.2', '2001:db8::2']}]}
        output = rules.render(config, self.rescue)
        self.assertIn('ip saddr { 198.51.100.2/32 } meta l4proto tcp tcp dport 60000-60100', output)
        self.assertIn('ip6 saddr { 2001:db8::2/128 } meta l4proto tcp tcp dport 60000-60100', output)
        self.assertNotIn('dport 20 ', output)

    def test_presets_are_disabled_and_tcp_only_with_explicit_sensitive_sources(self):
        config = rules.default_config(ssh_port=2222, pasv_min=61000, pasv_max=61100)
        self.assertEqual({service['id'] for service in config['services']}, {'http', 'ssh', 'ftp', 'ftp_pasv', 'smb', 'mariadb'})
        self.assertTrue(all(not service['enabled'] and service['protocol'] == 'tcp' for service in config['services']))
        self.assertEqual(next(service['ports'] for service in config['services'] if service['id'] == 'ftp_pasv'), ['61000-61100'])
        self.assertEqual(next(service['ports'] for service in config['services'] if service['id'] == 'ssh'), [2222])
        self.assertTrue(all(service['sources'] == [] for service in config['services'] if service['id'] != 'http'))

    def test_malformed_unknown_injection_and_host_bits_are_rejected(self):
        invalid = [None, {'schema': True, 'services': []}, {'schema': 1, 'services': []},
                   {'schema': 2, 'services': [], 'arbitrary': 'x'}]
        base = self.config()['services'][0]
        for key, value in [('id', 'a"; flush ruleset'), ('enabled', 1), ('protocol', 'icmp'),
                           ('ports', [0]), ('ports', [65536]), ('ports', [True]), ('ports', ['90-80']),
                           ('ports', ['22;flush ruleset']), ('sources', ['192.0.2.9/24']),
                           ('sources', ['2001:db8::1/64']), ('sources', ['fe80::1%eth0']),
                           ('sources', ['public', '192.0.2.1']), ('sources', [])]:
            invalid.append({'schema': 2, 'services': [{**base, key: value}]})
        for config in invalid:
            with self.subTest(config=config), self.assertRaises(rules.FirewallRuleError):
                rules.normalize(config)

    def test_duplicate_ids_and_protocol_port_overlap_do_not_silently_widen_permissions(self):
        config = self.config()
        config['services'].append(copy.deepcopy(config['services'][0]))
        with self.assertRaises(rules.FirewallRuleError):
            rules.normalize(config)
        config = self.config()
        config['services'].append({'id': 'custom_http', 'enabled': True, 'protocol': 'tcp', 'ports': ['79-81'], 'sources': ['198.51.100.1']})
        with self.assertRaisesRegex(rules.FirewallRuleError, 'overlap'):
            rules.normalize(config)
        config['services'][-1]['protocol'] = 'udp'
        self.assertTrue(rules.normalize(config))

    def test_rescue_cannot_be_absent_public_or_silently_broadened(self):
        invalid = [None, {}, {'port': True, 'sources': ['192.0.2.1']}, {'port': 22, 'sources': []},
                   {'port': 22, 'sources': ['public']}, {'port': 22, 'sources': ['0.0.0.0/0']},
                   {'port': 22, 'sources': ['::/0']}, {'port': 22, 'sources': ['192.0.2.1/24']},
                   {'port': 22, 'sources': ['0.0.0.0/1', '128.0.0.0/1']},
                   {'port': 22, 'sources': ['::/1', '8000::/1']},
                   {'port': 22, 'sources': ['192.0.2.1'], 'command': 'x'}]
        for rescue in invalid:
            with self.subTest(rescue=rescue), self.assertRaises(rules.FirewallRuleError):
                rules.render(self.config(), rescue)

    def test_size_limits_and_stable_render(self):
        config = self.config()
        config['services'][0]['ports'] = list(range(1, 130))
        with self.assertRaises(rules.FirewallRuleError):
            rules.normalize(config)
        config = self.config()
        config['services'][1]['sources'] = [f'192.0.2.{index}' for index in range(65)]
        with self.assertRaises(rules.FirewallRuleError):
            rules.normalize(config)
        config = self.config()
        output = rules.render(config, self.rescue)
        config['services'].reverse()
        for service in config['services']:
            service['ports'].reverse()
            service['sources'].reverse()
        self.assertEqual(rules.render(config, self.rescue), output)


if __name__ == '__main__':
    unittest.main()

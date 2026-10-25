"""Pure dual-stack INPUT compiler. No commands, network access or file writes.

Mandatory rescue and network-control exceptions are part of every ruleset.
Disabled services block new connections; established/related traffic survives.
"""
import ipaddress
import re


TABLE = 'threewa_services'
OWNER_COMMENT = '3waServerSetup firewall schema2'


class FirewallRuleError(ValueError):
    """The requested firewall policy cannot be safely compiled."""


def _keys(value, expected):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise FirewallRuleError('Missing or unknown firewall configuration fields.')


def _interval(value):
    if type(value) is int and 1 <= value <= 65535:
        return value, value
    if isinstance(value, str) and re.fullmatch(r'[0-9]{1,5}-[0-9]{1,5}', value):
        first, last = map(int, value.split('-'))
        if 1 <= first <= last <= 65535:
            return first, last
    raise FirewallRuleError('Ports must be integers 1..65535 or ascending start-end ranges.')


def _ports(values):
    if not isinstance(values, list) or not 1 <= len(values) <= 128:
        raise FirewallRuleError('Each service needs 1..128 ports/ranges.')
    merged = []
    for first, last in sorted(_interval(value) for value in values):
        if merged and first <= merged[-1][1] + 1:
            merged[-1] = merged[-1][0], max(last, merged[-1][1])
        else:
            merged.append((first, last))
    return [first if first == last else f'{first}-{last}' for first, last in merged]


def _sources(values, *, required=True, rescue=False):
    if not isinstance(values, list) or len(values) > 64 or required and not values:
        raise FirewallRuleError('Sources must be a bounded list; enabled services need at least one source.')
    if 'public' in values:
        if rescue or values != ['public']:
            raise FirewallRuleError('Public must be the sole source and cannot be used for SSH rescue.')
        return ['public']
    families = {4: [], 6: []}
    for value in values:
        if not isinstance(value, str) or len(value) > 128 or '%' in value:
            raise FirewallRuleError('Sources must be unscoped IPv4/IPv6 addresses or strict CIDRs.')
        try:
            network = ipaddress.ip_network(value, strict=True)
        except ValueError:
            raise FirewallRuleError('Invalid source IP/CIDR or CIDR contains host bits.') from None
        if rescue and network.prefixlen == 0:
            raise FirewallRuleError('SSH rescue must use explicit non-public source IP/CIDR.')
        families[network.version].append(network)
    collapsed = [network for version in (4, 6)
                 for network in ipaddress.collapse_addresses(families[version])]
    if rescue and any(network.prefixlen == 0 for network in collapsed):
        raise FirewallRuleError('Combined SSH rescue networks cannot cover an entire address family.')
    return [str(network) for network in collapsed]


def normalize(config):
    _keys(config, ('schema', 'services'))
    if type(config['schema']) is not int or config['schema'] != 2:
        raise FirewallRuleError('Only firewall schema 2 is supported.')
    if not isinstance(config['services'], list) or len(config['services']) > 256:
        raise FirewallRuleError('Services must be a list of at most 256 entries.')
    services, identifiers, occupied = [], set(), {'tcp': [], 'udp': []}
    for service in config['services']:
        _keys(service, ('id', 'enabled', 'protocol', 'ports', 'sources'))
        identifier = service['id']
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,47}', identifier):
            raise FirewallRuleError('Service id must be a short safe ASCII identifier.')
        if identifier in identifiers:
            raise FirewallRuleError('Duplicate service id.')
        identifiers.add(identifier)
        if type(service['enabled']) is not bool or service['protocol'] not in ('tcp', 'udp'):
            raise FirewallRuleError('Service enabled must be boolean and protocol must be tcp/udp.')
        ports = _ports(service['ports'])
        intervals = [_interval(value) for value in ports]
        for first, last in intervals:
            if any(first <= previous_last and previous_first <= last
                   for previous_first, previous_last in occupied[service['protocol']]):
                raise FirewallRuleError('Service port ranges overlap for the same protocol; edit the existing service instead.')
        occupied[service['protocol']].extend(intervals)
        services.append({'id': identifier, 'enabled': service['enabled'], 'protocol': service['protocol'],
                         'ports': ports, 'sources': _sources(service['sources'], required=service['enabled'])})
    return {'schema': 2, 'services': sorted(services, key=lambda service: service['id'])}


def normalize_rescue(rescue):
    _keys(rescue, ('port', 'sources'))
    if type(rescue['port']) is not int:
        raise FirewallRuleError('SSH rescue requires one actual TCP port.')
    _interval(rescue['port'])
    return {'port': rescue['port'], 'sources': _sources(rescue['sources'], rescue=True)}


def _address_groups(sources):
    return {version: [source for source in sources if ipaddress.ip_network(source).version == version]
            for version in (4, 6)}


def _port_expression(ports):
    text = [str(value) for value in ports]
    return text[0] if len(text) == 1 else '{ ' + ', '.join(text) + ' }'


def render(config, rescue, table_exists=False):
    config, rescue = normalize(config), normalize_rescue(rescue)
    if type(table_exists) is not bool:
        raise FirewallRuleError('Existing-table state must be a boolean.')
    lines = ['# Generated host INPUT policy: unlisted new connections are dropped.',
             '# Exceptions: loopback, SSH rescue, established/related and mandatory network control.',
             '# FORWARD, NAT, Docker-published traffic and external manager policies are not managed.']
    if table_exists:
        lines.append('delete table inet ' + TABLE)
    lines += ['table inet ' + TABLE + ' {', '    comment "' + OWNER_COMMENT + '"', '    chain input {',
              '        type filter hook input priority 10; policy drop;',
              '        iifname "lo" counter accept comment "mandatory loopback"']
    for version, addresses in _address_groups(rescue['sources']).items():
        if addresses:
            family = 'ip' if version == 4 else 'ip6'
            lines.append('        ' + family + ' saddr { ' + ', '.join(addresses) + ' } '
                         + f'meta l4proto tcp tcp dport {rescue["port"]} counter accept comment "mandatory SSH rescue IPv{version}"')
    lines += [
        '        ct state invalid counter drop comment "mandatory invalid drop"',
        '        ct state { established, related } counter accept comment "mandatory established related"',
        '        meta l4proto icmp icmp type { destination-unreachable, time-exceeded, parameter-problem } counter accept comment "mandatory IPv4 errors"',
        '        meta l4proto ipv6-icmp icmpv6 type { destination-unreachable, packet-too-big, time-exceeded, parameter-problem } counter accept comment "mandatory IPv6 errors PMTU"',
        '        meta l4proto ipv6-icmp ip6 hoplimit 255 icmpv6 type { nd-neighbor-solicit, nd-neighbor-advert } counter accept comment "mandatory IPv6 neighbor discovery DAD"',
        '        meta l4proto ipv6-icmp ip6 saddr fe80::/10 ip6 hoplimit 255 icmpv6 type nd-router-advert counter accept comment "mandatory IPv6 router advertisement"',
        '        meta l4proto ipv6-icmp ip6 saddr { ::/128, fe80::/10 } ip6 hoplimit 255 icmpv6 type nd-router-solicit counter accept comment "mandatory IPv6 router solicitation"',
        '        meta l4proto ipv6-icmp ip6 saddr fe80::/10 ip6 hoplimit 1 icmpv6 type mld-listener-query counter accept comment "mandatory IPv6 multicast membership query"',
        '        meta nfproto ipv4 meta l4proto udp udp sport 67 udp dport 68 counter accept comment "mandatory DHCPv4 client"',
        '        meta nfproto ipv6 meta l4proto udp udp sport 547 udp dport 546 counter accept comment "mandatory DHCPv6 client"',
    ]
    for service in config['services']:
        if not service['enabled']:
            continue
        protocol, ports = service['protocol'], _port_expression(service['ports'])
        match = f'meta l4proto {protocol} {protocol} dport {ports} counter accept'
        if service['sources'] == ['public']:
            lines.append(f'        {match} comment "service:{service["id"]} public IPv4 IPv6"')
        else:
            for version, addresses in _address_groups(service['sources']).items():
                if addresses:
                    family = 'ip' if version == 4 else 'ip6'
                    lines.append('        ' + family + ' saddr { ' + ', '.join(addresses) + ' } '
                                 + match + f' comment "service:{service["id"]} IPv{version}"')
    lines += ['    }', '}']
    output = '\n'.join(lines) + '\n'
    if len(output.encode('utf-8')) > 1024 * 1024:
        raise FirewallRuleError('Compiled firewall exceeds the bounded 1 MiB size.')
    return output


def default_config(ssh_port=22, pasv_min=60000, pasv_max=60100):
    if type(ssh_port) is not int or type(pasv_min) is not int or type(pasv_max) is not int:
        raise FirewallRuleError('Preset ports must be integers.')
    config = {'schema': 2, 'services': [
        {'id': 'http', 'enabled': False, 'protocol': 'tcp', 'ports': [80, 443], 'sources': ['public']},
        {'id': 'ssh', 'enabled': False, 'protocol': 'tcp', 'ports': [ssh_port], 'sources': []},
        {'id': 'ftp', 'enabled': False, 'protocol': 'tcp', 'ports': [21], 'sources': []},
        {'id': 'ftp_pasv', 'enabled': False, 'protocol': 'tcp', 'ports': [f'{pasv_min}-{pasv_max}'], 'sources': []},
        {'id': 'smb', 'enabled': False, 'protocol': 'tcp', 'ports': [445], 'sources': []},
        {'id': 'mariadb', 'enabled': False, 'protocol': 'tcp', 'ports': [3306], 'sources': []},
    ]}
    return normalize(config)

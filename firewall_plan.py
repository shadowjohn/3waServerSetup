"""Offline IPv4 INPUT candidate only: no subprocesses, sockets or system writes."""
import argparse
import ipaddress
import json
import re
import sys

CHAIN = 'THREEWA_INPUT4'


def keys(value, expected, optional=()):
    if not isinstance(value, dict) or not set(expected) <= set(value) or not set(value) <= set(expected) | set(optional):
        raise ValueError('Missing or unknown configuration keys')


def port(value):
    if type(value) is int and 1 <= value <= 65535:
        return str(value)
    if isinstance(value, str) and re.fullmatch(r'[0-9]{1,5}-[0-9]{1,5}', value):
        start, end = map(int, value.split('-'))
        if 1 <= start <= end <= 65535:
            return f'{start}:{end}'
    raise ValueError('Port must be 1..65535 or an ascending range')


def sources(values, rescue=False):
    if not isinstance(values, list) or not 1 <= len(values) <= 64:
        raise ValueError('Sources must contain 1..64 entries')
    if 'public' in values and values != ['public']:
        raise ValueError('Public must be the sole source; otherwise specify only IP/CIDR')
    output = []
    for value in values:
        if value == 'public' and not rescue:
            output.append('0.0.0.0/0')
            continue
        if not isinstance(value, str):
            raise ValueError('Source must be IPv4 CIDR')
        network = ipaddress.ip_network(value, strict=True)
        if network.version != 4 or (rescue and network.prefixlen == 0):
            raise ValueError('IPv4 required; SSH rescue cannot be public')
        output.append(str(network))
    return sorted(set(output))


def render(config):
    keys(config, ['schema', 'blacklist_set', 'ssh_rescue', 'rules'])
    if type(config['schema']) is not int or config['schema'] != 1:
        raise ValueError('Unsupported schema')
    set_name = config['blacklist_set']
    if not isinstance(set_name, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,30}', set_name):
        raise ValueError('Invalid IPv4 ipset name')
    rescue = config['ssh_rescue']
    keys(rescue, ['port', 'sources'])
    if type(rescue['port']) is not int:
        raise ValueError('SSH rescue needs one actual TCP port')
    ssh_port = port(rescue['port'])
    rescue_sources = sources(rescue['sources'], rescue=True)
    rules = config['rules']
    if not isinstance(rules, list) or len(rules) > 256:
        raise ValueError('Rules must be a list of at most 256 entries')
    lines = ['# OFFLINE TEXT ONLY: never pipe this directly to iptables-restore.',
             '# Default restore may flush existing filter rules; this is NOT a safe incremental file.',
             '# Not attached to INPUT; generating this text activates no protection.',
             '# IPv4 INPUT only; IPv6 is unsupported and is NOT protected by this candidate.',
             '# Requires a reviewed existing IPv4 ipset; no deployment or rollback provided.',
             '*filter', f':{CHAIN} - [0:0]', f'-A {CHAIN} -i lo -j ACCEPT']
    for source in rescue_sources:
        lines.append(f'-A {CHAIN} -p tcp -s {source} --dport {ssh_port} -j ACCEPT')
    lines.extend([f'-A {CHAIN} -m set --match-set {set_name} src -j DROP',
                  f'-A {CHAIN} -m conntrack --ctstate INVALID -j DROP',
                  f'-A {CHAIN} -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT'])
    allowed = set()
    for rule in rules:
        keys(rule, ['protocol', 'ports', 'sources'], ['enabled'])
        enabled = rule.get('enabled', True)
        if type(enabled) is not bool:
            raise ValueError('Enabled must be a JSON boolean')
        if rule['protocol'] not in ['tcp', 'udp']:
            raise ValueError('Only tcp/udp port rules supported')
        if not isinstance(rule['ports'], list) or not rule['ports'] or len(rule['ports']) > 128:
            raise ValueError('Ports must be a nonempty list of at most 128 values')
        rule_sources = sources(rule['sources'])
        for number in rule['ports']:
            destination = port(number)
            if not enabled:
                continue
            for source in rule_sources:
                allowed.add(f'-A {CHAIN} -p {rule["protocol"]} -s {source} --dport {destination} -j ACCEPT')
                if len(allowed) > 4096:
                    raise ValueError('Expanded allow rules exceed limit of 4096')
    lines.extend(sorted(allowed.difference(lines)))
    lines.extend([f'-A {CHAIN} -j DROP', 'COMMIT'])
    return '\n'.join(lines) + '\n'


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def load_config(path):
    with open(path, encoding='utf-8') as stream:
        data = stream.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ValueError('Configuration too large')
    return json.loads(data, object_pairs_hook=unique_object)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--validate', action='store_true')
    mode.add_argument('--render', action='store_true')
    args = parser.parse_args()
    try:
        output = render(load_config(args.config))
    except (ValueError, OSError, TypeError) as error:
        print(f'Invalid configuration: {error}', file=sys.stderr)
        return 1
    if args.render:
        sys.stdout.write(output)
    else:
        print('Configuration valid; offline only, no system changes.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

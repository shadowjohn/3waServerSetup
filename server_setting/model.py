"""In-memory recipe drafts and offline previews; never writes configuration."""
import difflib
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parents[1]
FIELDS = {
    'apache': {'ServerTokens': 'Prod', 'ServerSignature': 'Off'},
    'php': {'short_open_tag': 'On', 'memory_limit': '128M', 'post_max_size': '128M',
            'upload_max_filesize': '64M', 'date.timezone': 'Asia/Taipei',
            'session.upload_progress.enabled': 'Off', 'expose_php': 'Off'},
    'vsftpd': {'anonymous_enable': 'NO', 'local_enable': 'YES', 'write_enable': 'YES',
               'pasv_min_port': '60000', 'pasv_max_port': '60100'},
    'samba': {'security': 'user', 'map to guest': 'Never'},
    'mariadb': {'slow_query_log': '1', 'long_query_time': '1',
                'log_queries_not_using_indexes': 'OFF'},
    'time': {'Timezone': 'Asia/Taipei'},
    'startup': {}, 'storage': {},
}
LABELS = {'apache': 'Apache', 'php': 'PHP-FPM', 'vsftpd': 'FTP', 'samba': 'Samba',
          'mariadb': 'MariaDB', 'time': 'Time', 'startup': 'Startup', 'storage': 'Disk / Swap',
          'firewall': 'Firewall', 'sqlite': 'SQLite'}


def validate(key, value):
    if not isinstance(value, str) or not value or any(c in value for c in '\n\r\x00'):
        raise ValueError(f'{key}: empty or multiline value')
    if key in {'memory_limit', 'post_max_size', 'upload_max_filesize'}:
        if key == 'memory_limit' and value == '-1':
            return
        if not re.fullmatch(r'[1-9][0-9]*(?:[KMGkmg])?', value):
            raise ValueError(f'{key}: expected positive bytes or K/M/G')
    choices = {'ServerTokens': {'Prod', 'Major', 'Minor', 'Min', 'OS', 'Full'},
               'ServerSignature': {'Off', 'On', 'EMail'}, 'security': {'user'},
               'map to guest': {'Never', 'Bad User', 'Bad Password'}}
    if key in choices and value not in choices[key]:
        raise ValueError(f'{key}: unsupported choice')
    if key in {'short_open_tag', 'session.upload_progress.enabled', 'expose_php'} and value not in {'On', 'Off'}:
        raise ValueError(f'{key}: choose On or Off')
    if key.endswith('_enable') and value not in {'YES', 'NO'}:
        raise ValueError(f'{key}: choose YES or NO')
    if key in {'pasv_min_port', 'pasv_max_port'} and (not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 65535):
        raise ValueError(f'{key}: port must be 1..65535')
    if key in {'date.timezone', 'Timezone'}:
        if value != 'Asia/Taipei':
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError(f'{key}: timezone not available locally') from None
    if key == 'slow_query_log' and value not in {'0', '1'}:
        raise ValueError('slow_query_log: choose 0 or 1')
    if key == 'log_queries_not_using_indexes' and value not in {'ON', 'OFF'}:
        raise ValueError('log_queries_not_using_indexes: choose ON or OFF')
    if key == 'long_query_time' and (not re.fullmatch(r'[0-9]+(?:[.][0-9]+)?', value) or float(value) > 86400):
        raise ValueError('long_query_time: expected seconds from 0 to 86400')


def preview(module, target, configured):
    if module not in FIELDS or set(target) - set(FIELDS[module]):
        raise ValueError('Unknown module or recipe field')
    for key, value in target.items():
        validate(key, value)
    if {'pasv_min_port', 'pasv_max_port'} <= target.keys() and int(target['pasv_min_port']) > int(target['pasv_max_port']):
        raise ValueError('PASV minimum exceeds maximum')
    if module == 'php':
        def size(value):
            return int(value[:-1]) * 1024 ** ('KMG'.index(value[-1].upper()) + 1) if value[-1].upper() in 'KMG' else int(value)
        if 'upload_max_filesize' in target and 'post_max_size' in target and size(target['upload_max_filesize']) > size(target['post_max_size']):
            raise ValueError('upload_max_filesize must not exceed post_max_size')
    before = [f'{key} = {configured.get(key, "UNKNOWN")}\n' for key in target]
    after = [f'{key} = {value}\n' for key, value in target.items()]
    diff = ''.join(difflib.unified_diff(before, after, fromfile='observed-config', tofile='draft-target'))
    return ('PREVIEW ONLY - no files, services or settings changed.\n'
            'Configuration values are not runtime health evidence. UNKNOWN requires manual verification.\n'
            + (diff or 'NO DIFFERENCE in observed selected fields; runtime health not checked.\n'))


def firewall_preview(text=None):
    from firewall_plan import load_config, render, unique_object
    config = json.loads(text, object_pairs_hook=unique_object) if text is not None else load_config(ROOT / 'config/firewall.example.json')
    return render(config)


def apache_candidate():
    return (ROOT / 'config/apache-sensitive-files.example.conf').read_text(encoding='utf-8')

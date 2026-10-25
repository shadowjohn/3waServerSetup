"""Bounded local FastCGI request with a transient, fixed nonsecret PHP probe."""
import json
import os
from pathlib import Path
import socket
import struct
import tempfile


_SOURCE = b'''<?php
header('Content-Type: application/json');
$names = array('short_open_tag', 'memory_limit', 'post_max_size', 'upload_max_filesize',
    'date.timezone', 'session.upload_progress.enabled', 'expose_php');
$values = array();
foreach ($names as $name) { $values[$name] = ini_get($name); }
echo json_encode(array('sapi' => PHP_SAPI, 'values' => $values,
    'scanned' => php_ini_scanned_files() ?: '',
    'extensions' => array('pdo_sqlite' => extension_loaded('pdo_sqlite'),
                          'sqlite3' => extension_loaded('sqlite3'))));
'''
_MAX_OUTPUT = 65536


def _record(kind, content=b''):
    return struct.pack('!BBHHBB', 1, kind, 1, len(content), 0, 0) + content


def _length(value):
    return bytes([value]) if value < 128 else struct.pack('!I', value | 0x80000000)


def _params(script_path):
    values = {'GATEWAY_INTERFACE': 'CGI/1.1', 'SERVER_PROTOCOL': 'HTTP/1.1',
              'REQUEST_METHOD': 'GET', 'SCRIPT_FILENAME': script_path,
              'SCRIPT_NAME': '/3wa-fpm-probe.php', 'REQUEST_URI': '/3wa-fpm-probe.php',
              'QUERY_STRING': '', 'CONTENT_LENGTH': '0', 'REMOTE_ADDR': '127.0.0.1',
              'SERVER_ADDR': '127.0.0.1', 'SERVER_NAME': 'localhost', 'SERVER_PORT': '0'}
    content = b''
    for key, value in values.items():
        name, encoded = key.encode('ascii'), value.encode('utf-8')
        content += _length(len(name)) + _length(len(encoded)) + name + encoded
    return content


def _receive(sock, count):
    content = b''
    while len(content) < count:
        piece = sock.recv(count - len(content))
        if not piece:
            raise ValueError('FPM FastCGI connection closed before response completion.')
        content += piece
    return content


def _exchange(sock, script_path):
    begin = struct.pack('!HB5s', 1, 0, b'\0' * 5)
    sock.sendall(_record(1, begin) + _record(4, _params(script_path)) + _record(4) + _record(5))
    output = bytearray()
    total, records = 0, 0
    while True:
        version, kind, request_id, size, padding, _ = struct.unpack('!BBHHBB', _receive(sock, 8))
        total += size + padding
        records += 1
        if version != 1 or request_id != 1 or total > _MAX_OUTPUT or records > 1024:
            raise ValueError('Invalid or excessive FPM FastCGI response.')
        content = _receive(sock, size)
        if padding:
            _receive(sock, padding)
        if kind == 6:
            output.extend(content)
        elif kind == 7 and content:
            # Do not include stderr: it can reveal application paths or secrets.
            raise ValueError('FPM FastCGI request reported a runtime diagnostic.')
        elif kind == 3:
            if size != 8 or struct.unpack('!IB3s', content)[:2] != (0, 0):
                raise ValueError('FPM FastCGI request failed.')
            break
        elif kind != 7:
            raise ValueError('Unexpected FPM FastCGI record.')
    headers, separator, body = bytes(output).partition(b'\r\n\r\n')
    if not separator:
        headers, separator, body = bytes(output).partition(b'\n\n')
    if not separator or b'Status:' in headers and not any(line.lower() == b'status: 200 ok' for line in headers.splitlines()):
        raise ValueError('FPM probe returned an unsuccessful response.')
    try:
        evidence = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise ValueError('FPM probe did not return valid evidence JSON.') from None
    if not isinstance(evidence, dict) or not isinstance(evidence.get('values'), dict):
        raise ValueError('FPM probe returned malformed evidence.')
    return evidence


def _request(socket_path, script_path):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(8)
        connection.connect(socket_path)
        return _exchange(connection, script_path)


def probe_values(host, socket_path):
    """The manager has already enforced root/OS trust before this explicit check."""
    run = host.path('/run')
    directory = Path(tempfile.mkdtemp(prefix='3wa-server-setup-fpm-', dir=run))
    script = directory / 'probe.php'
    try:
        os.chmod(directory, 0o711)
        descriptor = os.open(script, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(_SOURCE)
        os.chmod(script, 0o644)
        virtual = '/run/' + directory.name + '/probe.php'
        return _request(str(host.path(socket_path)), virtual)
    finally:
        if script.exists():
            script.unlink()
        directory.rmdir()

"""Authenticated source/patch/artifact tests with actual temporary files."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

try:
    from server_setting.php_feather_build import (FeatherBuilder, FeatherHost, patch_sources,
        source_profile, verify_source_downloads, validate_artifact, unit_command, MODULES, STATE)
except ImportError:
    FeatherBuilder = FeatherHost = None
from server_setting.root_actions import RootActionError, _fingerprint


MODULE = b'''#include "php_ini.h"
static zend_class_entry *pdosqlite_ce;
PHP_MINIT_FUNCTION(pdo_sqlite)
{
    return SUCCESS;
}
PHP_MSHUTDOWN_FUNCTION(pdo_sqlite)
{
    return SUCCESS;
}
'''
DRIVER = b'''#include "php_ini.h"
static int pdo_sqlite_handle_factory(pdo_dbh_t *dbh, zval *driver_options) {
    i = sqlite3_open_v2(filename, &H->db, flags, NULL);
    if (i != SQLITE_OK) { goto cleanup; }
\tsqlite3_set_authorizer(H->db, authorizer, H);
    return 1;
}
'''


class FeatherBuildTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(FeatherBuilder, 'Feather compiler is not implemented')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for directory in ('etc/apt/sources.list.d', 'usr/share/keyrings', 'var/lib/dpkg'):
            (self.root / directory).mkdir(parents=True)
        (self.root / 'etc/os-release').write_text('ID=ubuntu\nVERSION_ID="26.04"\n')
        (self.root / 'usr/share/keyrings/ubuntu-archive-keyring.gpg').write_bytes(b'fixture native archive keyring')
        (self.root / 'etc/apt/sources.list.d/ubuntu.sources').write_text(
            'Types: deb\nURIs: http://tw.archive.ubuntu.com/ubuntu/\nSuites: resolute resolute-updates\n'
            'Components: main universe\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n')
        (self.root / 'var/lib/dpkg/status').write_bytes(b'fixture dpkg state\n')
        outer = self
        class FakeHost(FeatherHost):
            def inspect(self):
                return {'php_version': '8.5.4', 'package_version': '8.5.4-0ubuntu1.5', 'abi': '20250925',
                        'arch': 'amd64', 'nts': True, 'debug': False, 'source_package': 'php8.5',
                        'sqlite_package_version': '3.46.1-9ubuntu0.3', 'sqlite_version': '3.46.1',
                        'extension_dir': '/usr/lib/php/20250925', 'original_module': '/usr/lib/php/20250925/pdo_sqlite.so',
                        'original_sha256': 'a' * 64, 'pdo_module': '/usr/lib/php/20250925/pdo.so'}
        self.host = FakeHost(self.root)
        self.builder = FeatherBuilder(self.host)
        self.addCleanup(patch.stopall)
        patch('server_setting.root_actions.os.geteuid', return_value=0, create=True).start()
        @contextmanager
        def unlocked():
            yield
        patch.object(self.builder.core, '_lock', unlocked).start()
        class Packages:
            def plan(self, names):
                result = {'schema': 1, 'module': 'packages', 'names': sorted(names),
                          'observed': [{'name': name, 'installed': True, 'current': ('8.5.4-0ubuntu1.5' if name == 'php8.5-dev' else '3.46.1-9ubuntu0.3' if name == 'libsqlite3-dev' else '1'), 'candidate': '1'} for name in sorted(names)],
                          'changes': [], 'command': ['apt-get', '--no-upgrade', '--no-remove', '--simulate', 'install', '--', *sorted(names)],
                          'download_bytes': 0, 'disk_bytes': 0, 'apt_state_sha256': hashlib.sha256((outer.root / 'var/lib/dpkg/status').read_bytes()).hexdigest()}
                result['fingerprint'] = _fingerprint(result)
                return result
        patch.object(self.builder, '_packages', return_value=Packages()).start()

    def test_patch_is_system_only_default_off_constructor_gate_and_no_new_driver_api(self):
        inputs = {'pdo_sqlite.c': MODULE, 'sqlite_driver.c': DRIVER}
        result = patch_sources(inputs)
        self.assertEqual(inputs['pdo_sqlite.c'], MODULE)
        self.assertIn(b'PHP_INI_SYSTEM', result['pdo_sqlite.c'])
        self.assertIn(b'"pdo_sqlite.feather_sql_load_extension", "0"', result['pdo_sqlite.c'])
        self.assertIn(b'REGISTER_INI_ENTRIES();', result['pdo_sqlite.c'])
        self.assertIn(b'UNREGISTER_INI_ENTRIES();', result['pdo_sqlite.c'])
        self.assertIn(b'INI_BOOL("pdo_sqlite.feather_sql_load_extension")', result['sqlite_driver.c'])
        self.assertEqual(result['sqlite_driver.c'].count(b'sqlite3_enable_load_extension(H->db, 1)'), 1)
        self.assertNotIn(b'pdo_attr_', result['sqlite_driver.c'])

    def test_unknown_duplicate_or_already_patched_sources_refuse_without_writing(self):
        for files in ({'pdo_sqlite.c': MODULE, 'sqlite_driver.c': b'unknown source'},
                      {'pdo_sqlite.c': MODULE + MODULE, 'sqlite_driver.c': DRIVER},
                      patch_sources({'pdo_sqlite.c': MODULE, 'sqlite_driver.c': DRIVER})):
            with self.subTest(files=tuple(files)), self.assertRaises(RootActionError):
                patch_sources(files)

    def test_private_deb_src_uses_existing_signed_ubuntu_suites_without_changing_system_file(self):
        native = self.root / 'etc/apt/sources.list.d/ubuntu.sources'
        before = native.read_bytes()
        profile = source_profile(self.host)
        self.assertIn('Types: deb-src', profile['deb_src'])
        self.assertIn('resolute resolute-updates', profile['deb_src'])
        self.assertEqual(profile['keyring_sha256'], hashlib.sha256((self.root / 'usr/share/keyrings/ubuntu-archive-keyring.gpg').read_bytes()).hexdigest())
        self.assertEqual(native.read_bytes(), before)
        self.assertFalse((self.root / 'var/lib/3wa-server-setup').exists())

    def test_unauthenticated_foreign_suite_or_embedded_key_sources_refuse(self):
        path = self.root / 'etc/apt/sources.list.d/ubuntu.sources'
        original = path.read_text()
        for data in (original + 'Trusted: yes\n', original.replace('resolute', 'jammy'),
                     original.replace('/usr/share/keyrings/ubuntu-archive-keyring.gpg', '/tmp/keyring.gpg')):
            path.write_text(data)
            with self.subTest(data=data[-40:]), self.assertRaises(RootActionError):
                source_profile(self.host)

    def source_download(self):
        directory = self.root / 'source'
        directory.mkdir()
        archive = directory / 'php8.5_8.5.4.orig.tar.xz'
        archive.write_bytes(b'authenticated source archive fixture')
        debian = directory / 'php8.5_8.5.4-0ubuntu1.5.debian.tar.xz'
        debian.write_bytes(b'authenticated Ubuntu patch archive fixture')
        checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
        dsc = directory / 'php8.5_8.5.4-0ubuntu1.5.dsc'
        dsc.write_text('-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA256\n\nFormat: 3.0 (quilt)\n'
                       'Source: php8.5\nVersion: 8.5.4-0ubuntu1.5\nChecksums-Sha256:\n '
                       + checksum + ' ' + str(archive.stat().st_size) + ' ' + archive.name + '\n '
                       + hashlib.sha256(debian.read_bytes()).hexdigest() + ' ' + str(debian.stat().st_size) + ' ' + debian.name + '\n'
                       '-----BEGIN PGP SIGNATURE-----\nfixture\n-----END PGP SIGNATURE-----\n')
        return directory, archive

    def test_dsc_exact_version_and_every_archive_hash_are_checked(self):
        directory, archive = self.source_download()
        record = verify_source_downloads(directory, '8.5.4-0ubuntu1.5')
        self.assertEqual(record['source_package'], 'php8.5')
        self.assertEqual(record['package_version'], '8.5.4-0ubuntu1.5')
        self.assertEqual(record['files'][0]['sha256'], hashlib.sha256(archive.read_bytes()).hexdigest())
        with self.assertRaises(RootActionError):
            verify_source_downloads(directory, '8.5.4-0ubuntu1')
        archive.write_bytes(b'tampered source')
        with self.assertRaises(RootActionError):
            verify_source_downloads(directory, '8.5.4-0ubuntu1.5')

    def test_build_plan_names_exact_dependencies_and_is_observational(self):
        plan = self.builder.plan()
        self.assertEqual(plan['module'], 'php-feather-build')
        self.assertEqual(plan['native']['abi'], '20250925')
        self.assertIn('php8.5-dev', plan['dependencies']['names'])
        self.assertIn('libsqlite3-dev', plan['dependencies']['names'])
        self.assertIn('--no-install-recommends', plan['dependencies']['command'])
        self.assertIn('--with-php-config=/usr/bin/php-config8.5', str(plan['build']))
        self.assertIn('threewa-php-build', str(plan))
        self.assertEqual(plan['build']['make'], ['make', '-j2'])
        self.assertFalse(plan['build']['make_install'])
        self.assertFalse((self.root / 'var/lib/3wa-server-setup').exists())

    def test_tampered_or_stale_plan_blocks_before_source_fetch_or_deps(self):
        plan = self.builder.plan()
        plan['build']['configure'].append('--unsafe')
        with self.assertRaises(RootActionError):
            self.builder.build(plan)
        plan = self.builder.plan()
        (self.root / 'var/lib/dpkg/status').write_bytes(b'external package changes')
        with self.assertRaises(RootActionError) as error:
            self.builder.build(plan)
        self.assertEqual(error.exception.code, 'STALE_PLAN')
        self.assertFalse((self.root / 'var/lib/3wa-server-setup').exists())

    def test_build_unit_is_fixed_unprivileged_bounded_and_kills_all_descendants(self):
        command = unit_command('/var/tmp/threewa-feather-abcdef', '/var/tmp/threewa-feather-abcdef/job.json', 'abcdef123456')
        for value in ('User=threewa-php-build', 'Group=threewa-php-build', 'KillMode=control-group',
                      'NoNewPrivileges=yes', 'ProtectSystem=strict', 'ProtectHome=yes',
                      'RestrictAddressFamilies=AF_UNIX', 'PrivateNetwork=yes',
                      'ReadWritePaths=/var/tmp/threewa-feather-abcdef', 'RuntimeMaxSec=900'):
            self.assertIn(value, ' '.join(command))
        self.assertNotIn('runuser', command)
        self.assertNotIn('make install', ' '.join(command))

    def test_artifact_validator_reads_real_files_and_detects_tampering(self):
        directory = self.host.path(MODULES + '/' + 'b' * 64)
        directory.mkdir(parents=True)
        elf = bytearray(64)
        elf[:6] = b'\x7fELF\x02\x01'
        struct.pack_into('<H', elf, 16, 3)
        struct.pack_into('<H', elf, 18, 62)
        module = directory / 'pdo_sqlite.so'
        probe = directory / 'featherproof.so'
        for file in (module, probe):
            file.write_bytes(elf)
            file.chmod(0o644)
        artifact = {'module_path': MODULES + '/' + 'b' * 64 + '/pdo_sqlite.so', 'sha256': hashlib.sha256(elf).hexdigest(),
                    'probe_so_path': MODULES + '/' + 'b' * 64 + '/featherproof.so', 'probe_sha256': hashlib.sha256(elf).hexdigest(),
                    'php_version': '8.5.4', 'package_version': '8.5.4-0ubuntu1.5', 'abi': '20250925', 'arch': 'amd64', 'nts': True,
                    'source_sha256': 'c' * 64, 'patch_sha256': 'd' * 64, 'probe_function': 'threewa_abcdef123456',
                    'probe_value': 'feather-abcdef123456', 'buildmanifest': STATE + '/manifests/' + 'b' * 64 + '.json'}
        manifest = self.host.path(artifact['buildmanifest'])
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({'artifact': artifact, 'verified': True}))
        manifest.chmod(0o600)
        self.assertEqual(validate_artifact(artifact, self.host)['sha256'], artifact['sha256'])
        module.write_bytes(b'tampered artifact')
        with self.assertRaises(RootActionError):
            validate_artifact(artifact, self.host)

    def test_source_descriptor_requires_the_ubuntu_quilt_patch_archive(self):
        directory, archive = self.source_download()
        descriptor = next(directory.glob('*.dsc'))
        text = descriptor.read_text()
        descriptor.write_text('\n'.join(line for line in text.splitlines() if '.debian.tar.' not in line) + '\n')
        with self.assertRaises(RootActionError):
            verify_source_downloads(directory, '8.5.4-0ubuntu1.5')

    def test_native_ubuntu_orig_tar_signature_is_hash_checked_but_not_a_source_tar(self):
        directory, archive = self.source_download()
        signature = directory / (archive.name + '.asc')
        signature.write_bytes(b'authenticated upstream signature fixture')
        descriptor = next(directory.glob('*.dsc'))
        extra = (' ' + hashlib.sha256(signature.read_bytes()).hexdigest() + ' '
                 + str(signature.stat().st_size) + ' ' + signature.name + '\n')
        text = descriptor.read_text().replace('-----BEGIN PGP SIGNATURE-----', extra + '-----BEGIN PGP SIGNATURE-----')
        descriptor.write_text(text)
        proof = verify_source_downloads(directory, '8.5.4-0ubuntu1.5')
        self.assertEqual(len(proof['files']), 3)
        self.assertEqual(next(row['sha256'] for row in proof['files'] if row['name'].endswith('.asc')),
                         hashlib.sha256(signature.read_bytes()).hexdigest())
        signature.write_bytes(b'tampered signature bytes')
        with self.assertRaises(RootActionError) as error:
            verify_source_downloads(directory, '8.5.4-0ubuntu1.5')
        self.assertEqual(error.exception.code, 'FEATHER_SOURCE_INTEGRITY')
        signature.write_bytes(b'authenticated upstream signature fixture')
        descriptor.write_text('\n'.join(line for line in text.splitlines() if not line.rstrip().endswith(' ' + archive.name)) + '\n')
        with self.assertRaises(RootActionError):
            verify_source_downloads(directory, '8.5.4-0ubuntu1.5')

    def test_public_artifact_directories_are_exact_0755_even_with_private_umask(self):
        previous = os.umask(0o077)
        try:
            path = self.builder._public_dir(MODULES + '/' + 'a' * 64)
        finally:
            os.umask(previous)
        self.assertTrue(path.is_dir())
        if os.name == 'posix':
            self.assertEqual(path.stat().st_mode & 0o777, 0o755)


if __name__ == '__main__':
    unittest.main()

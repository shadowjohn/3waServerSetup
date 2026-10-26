"""Installer previews use real APT state files and a command-only fake."""
import copy
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

try:
    from server_setting.root_packages import PackageManager, PackageHost
except ImportError:
    PackageManager = PackageHost = None
from server_setting.root_actions import RootActionError, _fingerprint


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(PackageManager, 'Independent package installer is not implemented')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for directory in ('etc/apt/sources.list.d', 'var/lib/dpkg', 'var/lib/apt/lists'):
            (self.root / directory).mkdir(parents=True)
        (self.root / 'etc/os-release').write_text('ID=ubuntu\nVERSION_ID="26.04"\n')
        (self.root / 'var/lib/dpkg/status').write_bytes(b'original dpkg status\n')
        (self.root / 'var/lib/apt/lists/example_Packages').write_bytes(b'configured package index\n')
        self.calls = []
        self.installed = {'curl': '8.0', 'php8.5-common': '8.5', 'php8.5-cli': '8.5', 'php8.5-fpm': '8.5'}
        self.candidates = {'gdal-bin': '3.12', 'libgdal38': '3.12', 'curl': '8.1',
                           'php-fpm': '2:8.5+99', 'php8.5-common': '8.5', 'php8.5-cli': '8.5', 'php8.5-fpm': '8.5'}
        self.simulation = 'Inst libgdal38 (3.12 Ubuntu:26.04 [amd64])\nInst gdal-bin (3.12 Ubuntu:26.04 [amd64])\nConf libgdal38 (3.12 Ubuntu:26.04 [amd64])\nConf gdal-bin (3.12 Ubuntu:26.04 [amd64])\n'
        self.fail_install = False
        self.fail_verification = False
        self.search_output = '\n'.join(f'pkg-{index:03} - package description' for index in range(75))
        outer = self

        class FakeHost(PackageHost):
            def run(self, argv, timeout=30):
                outer.calls.append(list(argv))
                if argv[:2] == ['apt-cache', 'policy']:
                    rows = []
                    for name in argv[3:]:
                        rows.append(name + ':\n  Installed: ' + outer.installed.get(name, '(none)')
                                    + '\n  Candidate: ' + outer.candidates.get(name, '(none)'))
                    return subprocess.CompletedProcess(argv, 0, '\n'.join(rows) + '\n', '')
                if argv[:2] == ['apt-cache', 'search']:
                    return subprocess.CompletedProcess(argv, 0, outer.search_output, '')
                if argv[:2] == ['apt-cache', 'show']:
                    names = argv[4:]
                    rows = []
                    for spec in names:
                        name, _, version = spec.partition('=')
                        version = version or outer.candidates.get(name, '1')
                        extra = 'Depends: php8.5-fpm\n' if name == 'php-fpm' else ''
                        rows.append(f'Package: {name}\nVersion: {version}\nArchitecture: amd64\nSize: 100\nInstalled-Size: 2\n{extra}\n')
                    return subprocess.CompletedProcess(argv, 0, ''.join(rows), '')
                if argv[0] == 'apt-get' and '--simulate' in argv:
                    names = argv[argv.index('--') + 1:]
                    return subprocess.CompletedProcess(argv, 0, outer.simulation if names else '', '')
                if argv[0] == 'apt-get':
                    if not outer.fail_install and not outer.fail_verification:
                        outer.installed.update({'gdal-bin': '3.12', 'libgdal38': '3.12'})
                    (outer.root / 'var/lib/dpkg/status').write_bytes(b'APT may already have changed package state\n')
                    return subprocess.CompletedProcess(argv, int(outer.fail_install), 'https://credential@private-source.invalid/never-log', '')
                if argv[0] == 'dpkg-query':
                    rows = [name + '\tii \t' + outer.installed[name] + '\t2' for name in argv[argv.index('--') + 1:] if name in outer.installed]
                    return subprocess.CompletedProcess(argv, 0, '\n'.join(rows) + ('\n' if rows else ''), '')
                raise AssertionError(argv)

        self.host = FakeHost(self.root)
        self.manager = PackageManager(self.host)
        self.addCleanup(patch.stopall)
        patch('server_setting.root_actions.os.geteuid', return_value=0, create=True).start()
        @contextmanager
        def unlocked():
            yield
        patch.object(self.manager.core, '_lock', unlocked).start()

    def real_installs(self):
        return [argv for argv in self.calls if argv[0] == 'apt-get' and '--simulate' not in argv]

    def test_invalid_names_never_reach_apt(self):
        for names in ([], ['--yes'], ['/tmp/a.deb'], ['https://example/a'], ['gdal-bin=1'],
                      ['gdal-bin:amd64'], ['Apt'], ['a.deb'], ['a\nreboot'], ['pkg'] * 33, 'gdal-bin'):
            with self.subTest(names=names), self.assertRaises(RootActionError):
                self.manager.plan(names)
        self.assertFalse(self.calls)

    def test_preview_includes_new_dependencies_versions_sizes_and_preserves_state(self):
        preview = self.manager.plan(['gdal-bin', 'gdal-bin'])
        self.assertEqual(preview['names'], ['gdal-bin'])
        self.assertEqual([row['name'] for row in preview['changes']], ['gdal-bin', 'libgdal38'])
        self.assertTrue(all(row['action'] == 'install' and row['candidate'] == '3.12' for row in preview['changes']))
        self.assertEqual(preview['download_bytes'], 200)
        self.assertEqual(preview['disk_bytes'], 4096)
        self.assertFalse(self.real_installs())
        self.assertFalse((self.root / 'var/lib/3wa-server-setup').exists())
        simulation = next(argv for argv in self.calls if argv[0] == 'apt-get')
        self.assertIn('--no-upgrade', simulation)
        self.assertIn('--no-remove', simulation)
        self.assertIn('Dpkg::Options::=--force-confold', simulation)

    def test_already_installed_is_noop_even_if_new_candidate_exists(self):
        preview = self.manager.plan(['curl'])
        self.assertEqual(preview['changes'], [])
        self.assertEqual(preview['observed'][0]['current'], '8.0')
        self.assertEqual(preview['observed'][0]['candidate'], '8.1')
        result = self.manager.install(preview)
        self.assertEqual(result['status'], 'NO_CHANGE')
        self.assertFalse(self.real_installs())
        self.assertFalse((self.root / 'var/lib/3wa-server-setup').exists())

    def test_dependency_upgrade_removal_and_unpreviewed_configuration_refuse(self):
        for simulation in ('Inst curl [8.0] (8.1 Ubuntu [amd64])\n', 'Remv curl [8.0]\n',
                           'Conf broken (1.0 Ubuntu [amd64])\n'):
            self.simulation = simulation
            with self.subTest(simulation=simulation), self.assertRaises(RootActionError):
                self.manager.plan(['gdal-bin'])
        self.assertFalse(self.real_installs())

    def test_missing_candidate_refuses_without_apt_regex_fallback(self):
        with self.assertRaises(RootActionError) as error:
            self.manager.plan(['nonexistent.pkg'])
        self.assertEqual(error.exception.code, 'PACKAGES_NOT_AVAILABLE')
        self.assertFalse(any(argv[0] == 'apt-get' for argv in self.calls))

    def test_stale_index_and_rehashed_tampering_never_install(self):
        preview = self.manager.plan(['gdal-bin'])
        (self.root / 'var/lib/apt/lists/example_Packages').write_bytes(b'new index')
        with self.assertRaises(RootActionError) as error:
            self.manager.install(preview)
        self.assertEqual(error.exception.code, 'STALE_PLAN')
        preview = self.manager.plan(['gdal-bin'])
        preview['command'] = ['apt-get', 'full-upgrade']
        preview['fingerprint'] = _fingerprint(preview)
        with self.assertRaises(RootActionError):
            self.manager.install(preview)
        self.assertFalse(self.real_installs())

    def test_install_rebuilds_command_and_verifies_dpkg_before_success(self):
        preview = self.manager.plan(['gdal-bin'])
        result = self.manager.install(preview)
        self.assertEqual(result['status'], 'PACKAGES_INSTALLED')
        argv = self.real_installs()[0]
        self.assertEqual(argv, [arg for arg in next(call for call in self.calls if call[0] == 'apt-get' and '--simulate' in call) if arg != '--simulate'][:])
        self.assertIn('--yes', argv)
        self.assertFalse(any(item in argv for item in ('update', 'upgrade', 'autoremove', 'full-upgrade', 'dist-upgrade')))
        journal = self.manager.core._journals()[-1]
        self.assertEqual(journal['status'], 'PACKAGES_INSTALLED')
        self.assertNotIn('private-source', str(result) + json.dumps(journal))

    def test_failure_or_failed_verification_is_truthful_and_never_claims_rollback(self):
        for mode in ('command', 'verification'):
            with self.subTest(mode=mode):
                self.fail_install = mode == 'command'
                self.fail_verification = mode == 'verification'
                result = self.manager.install(self.manager.plan(['gdal-bin']))
                self.assertEqual(result['status'], 'PACKAGES_FAILED')
                self.assertTrue(result['package_state_may_have_changed'])
                self.assertEqual(self.manager.core._journals()[-1]['status'], 'PACKAGES_FAILED')
                self.assertNotIn('private-source', str(result))
                self.assertNotIn('rolled back', result['message'].lower())

    def test_pending_transaction_blocks_install(self):
        preview = self.manager.plan(['gdal-bin'])
        self.manager.core._journal({'transaction_id': self.manager.core._id(), 'kind': 'swap', 'module': 'swap', 'status': 'SWAP_RECOVERY_REQUIRED'})
        with self.assertRaises(RootActionError) as error:
            self.manager.install(preview)
        self.assertEqual(error.exception.code, 'RECOVERY_REQUIRED')
        self.assertFalse(self.real_installs())

    def test_search_is_literal_bounded_and_catalog_has_provider_components(self):
        result = self.manager.search('gdal.lib')
        self.assertEqual(len(result), 50)
        self.assertIn(r'gdal\.lib', self.calls[-1])
        with self.assertRaises(RootActionError):
            self.manager.search('gdal; reboot')
        groups = self.manager.catalog()
        self.assertTrue({'gis_cli', 'mapserver', 'python_gis', 'gis_headers', 'php', 'base'} <= {group['id'] for group in groups})
        items = [item for group in groups for item in group['items']]
        exif = next(item for item in items if item['id'] == 'php-exif')
        self.assertEqual(exif['packages'], ['php8.5-common'])
        self.assertTrue(exif['installed'])
        builtin = next(item for item in items if item['id'] == 'php-json')
        self.assertEqual(builtin['packages'], [])
        self.assertEqual(builtin['component_status'], 'builtin')
        self.assertTrue(builtin['installed'])
        custom = next(item for item in items if item['id'] == 'php-pdo-sqlite-custom')
        self.assertEqual(custom['packages'], [])
        self.assertEqual(custom['component_status'], 'external build')
        self.assertFalse(self.real_installs())

    def test_spatialite_loadable_module_catalog_item_has_an_install_only_plan(self):
        name = 'libsqlite3-mod-spatialite'
        self.candidates[name] = '5.1.0-1'
        groups = self.manager.catalog()
        gis = next(group for group in groups if group['id'] == 'gis_cli')
        matches = [item for item in gis['items'] if item['id'] == name]
        self.assertEqual(len(matches), 1)
        item = matches[0]
        self.assertEqual(item['packages'], [name])
        self.assertIn(name, gis['packages'])
        self.assertIn('SpatiaLite loadable module', item['label'])
        for component in ('mod_spatialite.so', 'PHP PDO/SQLite3', 'runtime support'):
            self.assertIn(component, item['detail'])
        self.assertFalse(item['installed'])
        self.assertIsNone(item['current'])
        self.assertEqual(item['candidate'], '5.1.0-1')
        base = next(group for group in groups if group['id'] == 'base')
        self.assertEqual(base['items'][0]['id'], 'ntpdate')
        self.assertEqual(base['items'][0]['packages'], ['ntpsec-ntpdate'])

        self.simulation = f'Inst {name} (5.1.0-1 Ubuntu:26.04 [amd64])\nConf {name} (5.1.0-1 Ubuntu:26.04 [amd64])\n'
        preview = self.manager.plan(item['packages'])
        self.assertEqual(preview['names'], [name])
        self.assertEqual(preview['install_names'], [name])
        self.assertEqual([(row['name'], row['candidate'], row['action']) for row in preview['changes']], [(name, '5.1.0-1', 'install')])
        self.assertEqual((preview['download_bytes'], preview['disk_bytes']), (100, 2048))
        self.assertIn('--no-upgrade', preview['command'])
        self.assertIn('--no-remove', preview['command'])
        self.assertFalse(self.real_installs())
        self.assertFalse((self.root/'var/lib/3wa-server-setup').exists())

    def test_spatialite_catalog_and_plan_keep_the_ubuntu26_write_gate(self):
        for version in ('22.04', '24.04'):
            (self.root/'etc/os-release').write_text('ID=ubuntu\nVERSION_ID="' + version + '"\n')
            for operation in (self.manager.catalog, lambda: self.manager.plan(['libsqlite3-mod-spatialite'])):
                with self.subTest(version=version, operation=operation), self.assertRaises(RootActionError) as error:
                    operation()
                self.assertEqual(error.exception.code, 'UNSUPPORTED_OS')
        self.assertFalse(self.calls)

    def test_readonly_search_status_can_cover_all_fifty_results_without_expanding_install_limit(self):
        names = [row['name'] for row in self.manager.search('package')]
        self.assertEqual(len(self.manager.status(names)['items']), 50)
        with self.assertRaises(RootActionError):
            self.manager.plan(names)
        self.assertFalse(self.real_installs())


if __name__ == '__main__':
    unittest.main()

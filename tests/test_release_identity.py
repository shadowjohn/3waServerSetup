"""Release identity does not depend on a Git checkout or execute tools."""
import json
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class ReleaseIdentityTests(unittest.TestCase):
    def test_calendar_and_daily_sequence_boundaries(self):
        from server_setting import valid_release_tag
        for value in ('v2026.10.25.001','v2026.10.25.999','v2028.02.29.001-rc.2'):
            with self.subTest(value=value):self.assertTrue(valid_release_tag(value))
        for value in ('v2026.02.29.001','v2026.04.31.001','v2026.10.25.000','v2026.10.25.1000',
                      'v2026.1.25.001','v2026.10.25.001-rc.0','v2026.10.25.001-rc.01',None):
            with self.subTest(value=value):self.assertFalse(valid_release_tag(value))

    def test_running_identity_is_pinned_when_compatibility_entry_changes(self):
        import server_setting
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, version, commit in [('old','2026.10.25.001','a'*40),('new','2026.10.25.002','b'*40)]:
                release = root/name
                (release/'server_setting').mkdir(parents=True)
                (release/'server_setting/__init__.py').write_bytes(Path(server_setting.__file__).read_bytes())
                (release/'RELEASE.json').write_text(json.dumps({'version':version,'commit':commit,
                    'data_schema':1,'validated_platforms':[]}),encoding='utf-8')
            spec = importlib.util.spec_from_file_location('release_identity_fixture',root/'old/server_setting/__init__.py')
            module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
            # An imported module keeps its code even if the compatibility entry later resolves elsewhere.
            module.__file__ = str(root/'new/server_setting/__init__.py')
            self.assertEqual((module.VERSION,module.version_info()['version'],module.revision()),('2026.10.25.001','2026.10.25.001','a'*12))

    def test_manifest_identity_is_used_without_resolving_git(self):
        from server_setting import version_info
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'RELEASE.json').write_text(json.dumps({
                'schema': 1, 'product': '3waServerSetting', 'repository': 'shadowjohn/3waServerSetup',
                'version': '2026.10.25.001-rc.2', 'tag': 'v2026.10.25.001-rc.2', 'commit': 'a'*40, 'data_schema': 1,
                'validated_platforms': ['ubuntu26.04']}), encoding='utf-8')
            with patch('server_setting.trusted_commands.resolve') as resolve, patch('subprocess.run') as run:
                info = version_info(root)
            self.assertEqual((info['version'], info['commit'], info['channel']), ('2026.10.25.001-rc.2', 'a'*40, 'release'))
            self.assertEqual(info['validated_platforms'], ['ubuntu26.04'])
            resolve.assert_not_called(); run.assert_not_called()

    def test_ubuntu22_release_identity_and_independent_program_gate(self):
        import server_setting
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'RELEASE.json').write_text(json.dumps({'version':'2026.10.26.002','commit':'a'*40,
                'data_schema':1,'python_requires':'>=3.10','validated_platforms':['ubuntu22.04']}),encoding='utf-8')
            self.assertEqual(server_setting.version_info(root)['channel'],'release')
        for release,expected in (('22.04',True),('24.04',True),('26.04',True),('20.04',False)):
            with self.subTest(release=release),patch('server_setting.platform.system',return_value='Linux'), \
                    patch('server_setting.os.geteuid',return_value=0,create=True), \
                    patch.object(Path,'read_text',return_value='ID=ubuntu\nVERSION_ID="'+release+'"\n'):
                self.assertEqual(server_setting.update_management_available(),expected)

    def test_invalid_or_foreign_release_is_not_reported_as_verified_or_git_source(self):
        from server_setting import version_info
        good = {'version': '2026.10.25.001', 'commit': 'b'*40, 'data_schema': 1, 'validated_platforms': ['ubuntu26.04']}
        cases = [dict(good, version='../bad'), dict(good, version='2026.02.30.001'),
                 dict(good, version='2026.10.25.000'), dict(good, version='2026.1.25.001'),
                 dict(good, version='2026.10.25.1000'), dict(good, commit='bad'), dict(good, data_schema=2),
                 dict(good, repository='other/repo'), dict(good, tag='v9.0.0'),
                 dict(good, validated_platforms='ubuntu26.04')]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for value in cases:
                with self.subTest(value=value):
                    (root/'RELEASE.json').write_text(json.dumps(value), encoding='utf-8')
                    with patch('subprocess.run') as run:
                        info = version_info(root)
                    self.assertEqual(info['channel'], 'invalid-release')
                    self.assertEqual(info['commit'], 'unknown')
                    run.assert_not_called()

    def test_release_revision_uses_full_manifest_identity_shortened_only_for_display(self):
        import server_setting
        with patch.object(server_setting, '_release_identity', return_value={
                'version':'2026.10.25.001', 'commit':'c'*40, 'validated_platforms':[]}):
            with patch('subprocess.run') as run:
                self.assertEqual(server_setting.revision(), 'c'*12)
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()

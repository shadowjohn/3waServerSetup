"""Release artifacts contain only reviewed payload and bootstrap validates before use."""
import base64
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def load(name):
    path = ROOT / 'scripts' / (name + '.py')
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BuildFixture(unittest.TestCase):
    def setUp(self):
        self.builder = load('build_release')
        self.assertIsNotNone(self.builder, 'Release builder is not implemented')
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source, self.wheels, self.output = [self.root / n for n in ('source', 'wheels', 'dist')]
        self.source.mkdir(); self.wheels.mkdir()
        for name in ('README.md', 'LICENSE', 'run.sh', 'nat.sh', 'firewall_plan.py', 'requirements-tui.txt',
                     'server_setting/__init__.py', 'server_setting/release_update.py',
                     'config/firewall.example.json', 'tests/sqlite-smoke.php', 'docs/apt-install.md'):
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# public fixture\n' if not name.endswith('.json') else '{}\n', encoding='utf-8')
        (self.source/'release').mkdir()
        public = base64.b64encode(b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20' + b'0'*32).decode()
        (self.source/'release/allowed_signers').write_text('3waServerSetup-release ssh-ed25519 '+public+'\n', encoding='utf-8')
        (self.source/'scripts').mkdir()
        for name in ('install.sh.in', 'bootstrap_verify.py'):
            path = ROOT/'scripts'/name
            self.assertTrue(path.is_file(), name+' is not implemented')
            shutil.copyfile(path, self.source/'scripts'/name)
        self.wheel = self.wheels/'fixture_pkg-1.2.3-py3-none-any.whl'
        with zipfile.ZipFile(self.wheel, 'w') as wheel:
            wheel.writestr('fixture_pkg-1.2.3.dist-info/METADATA', 'Metadata-Version: 2.1\nName: fixture-pkg\nVersion: 1.2.3\n')
            wheel.writestr('fixture_pkg-1.2.3.dist-info/WHEEL', 'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
        self.digest = hashlib.sha256(self.wheel.read_bytes()).hexdigest()
        (self.source/'requirements-tui.lock').write_text('fixture-pkg==1.2.3 --hash=sha256:'+self.digest+'\n', encoding='utf-8')

    def build(self, **changes):
        args = dict(source=self.source, wheelhouse=self.wheels, output=self.output,
                    version='v2026.10.25.001', commit='a'*40, validated_platforms=['ubuntu26.04'])
        args.update(changes)
        return self.builder.build_release(**args)


class ReleaseBuildTests(BuildFixture):
    def test_exact_whitelist_hashes_modes_and_identity_with_no_runtime_leak(self):
        forbidden = ('config/password.json', 'backups/db.conf', 'modules/private.so', '.root-dev/private.key',
                     '.venv/bin/python', 'server_setting/__pycache__/compiled.pyc', 'tests/test_private.py')
        for name in forbidden:
            path = self.source/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'PRIVATE_MUST_NOT_SHIP')
        image = self.source/'docs/previews/binary.png'; image.parent.mkdir(parents=True)
        image.write_bytes(b'\x89PNG\r\n\x1a\nopaque\r\n\x00')
        result = self.build()
        manifest = json.loads((self.output/'release-manifest.json').read_text())
        self.assertEqual((manifest['version'], manifest['tag'], manifest['commit']), ('2026.10.25.001', 'v2026.10.25.001', 'a'*40))
        self.assertEqual(manifest['archive']['sha256'], hashlib.sha256((self.output/manifest['archive']['name']).read_bytes()).hexdigest())
        with tarfile.open(self.output/manifest['archive']['name']) as archive:
            contents = {p.name.removeprefix('3waServerSetting/'): archive.extractfile(p).read() for p in archive.getmembers()}
            self.assertEqual(set(contents), set(manifest['files']))
            self.assertTrue(all(p.isfile() and p.uid == 0 and p.gid == 0 for p in archive.getmembers()))
            self.assertEqual({p.mode for p in archive.getmembers() if p.name.endswith('/run.sh')}, {0o755})
        self.assertFalse(set(forbidden) & set(contents))
        self.assertFalse(any(b'PRIVATE_MUST_NOT_SHIP' in value for value in contents.values()))
        self.assertEqual(contents['docs/previews/binary.png'], image.read_bytes())
        for name, data in contents.items():
            self.assertEqual(hashlib.sha256(data).hexdigest(), manifest['files'][name]['sha256'])
            self.assertEqual(len(data), manifest['files'][name]['size'])
        identity = json.loads(contents['RELEASE.json'])
        self.assertEqual(identity['data_schema'], 1)
        self.assertEqual(identity['python_requires'], '>=3.10')
        self.assertEqual(identity['validated_platforms'], ['ubuntu26.04'])
        installer = (self.output/'install.sh').read_bytes()
        self.assertNotIn(b'@ALLOWED_SIGNERS@', installer)
        self.assertNotIn(b'@BOOTSTRAP_PYTHON@', installer)
        self.assertIn(b'3waServerSetup-release', installer)
        self.assertEqual(hashlib.sha256(installer).hexdigest(), manifest['installer']['sha256'])
        self.assertEqual(result['archive'], str(self.output/manifest['archive']['name']))

    def test_missing_changed_unlocked_or_native_wheel_is_rejected(self):
        original = self.wheel.read_bytes()
        self.wheel.write_bytes(original+b'tamper')
        with self.assertRaises(ValueError): self.build()
        self.wheel.write_bytes(original)

        extra = self.wheels/'unlocked-1-py3-none-any.whl'; extra.write_bytes(original)
        with self.assertRaises(ValueError): self.build()
        extra.unlink(); self.wheel.unlink()
        with self.assertRaises(ValueError): self.build()
        self.wheel.write_bytes(original)
        native = self.wheels/'fixture_pkg-1.2.3-cp312-cp312-linux_x86_64.whl'
        self.wheel.rename(native)
        with self.assertRaises(ValueError): self.build()

    def test_ubuntu22_validation_uses_native_python310_metadata(self):
        self.build(validated_platforms=['ubuntu22.04','ubuntu24.04','ubuntu26.04'])
        manifest=json.loads((self.output/'release-manifest.json').read_text())
        self.assertEqual(manifest['python_requires'],'>=3.10')
        self.assertEqual(manifest['validated_platforms'],['ubuntu22.04','ubuntu24.04','ubuntu26.04'])

    def test_unsafe_version_commit_signers_or_source_symlink_refused(self):
        for value in ('v2026.2.25.001', 'main', 'v2026.10.25.001;reboot', '../v2026.10.25.001',
                      'v2026.02.29.001', 'v2026.10.25.000', 'v2026.10.25.01', 'v2026.10.25.1000'):
            with self.subTest(value=value), self.assertRaises(ValueError): self.build(version=value)
        with self.assertRaises(ValueError): self.build(commit='short')
        with self.assertRaises(ValueError): self.build(validated_platforms=[])
        self.assertEqual(self.builder.release_version('v2024.02.29.999-rc.1'), '2024.02.29.999-rc.1')
        signer = self.source/'release/allowed_signers'; original = signer.read_bytes()
        signer.write_bytes(b'unknown ssh-ed25519 AA==\n')
        with self.assertRaises(ValueError): self.build()
        signer.write_bytes(original)
        target = self.source/'server_setting/untrusted.py'
        try: target.symlink_to(self.source/'README.md')
        except OSError: return
        with self.assertRaises(ValueError): self.build()

    def test_same_source_produces_deterministic_archive(self):
        self.build()
        initial = (self.output/'3waServerSetting-v2026.10.25.001.tar.gz').read_bytes()
        # A Windows checkout and the canonical Git LF tree must publish identical bytes.
        for name in ('run.sh','requirements-tui.lock','server_setting/__init__.py'):
            path = self.source/name
            path.write_bytes(path.read_bytes().replace(b'\r\n',b'\n'))
        self.build(output=self.root/'second')
        self.assertEqual(initial, (self.root/'second/3waServerSetting-v2026.10.25.001.tar.gz').read_bytes())

    def test_output_with_unrelated_files_is_not_published_or_deleted(self):
        self.output.mkdir()
        private = self.output/'unrelated.key'; private.write_bytes(b'private fixture')
        with self.assertRaises(ValueError): self.build()
        self.assertEqual(private.read_bytes(), b'private fixture')
        self.assertEqual(list(self.output.iterdir()), [private])


@unittest.skipUnless(shutil.which('ssh-keygen'), 'OpenSSH unavailable')
class BootstrapVerificationTests(BuildFixture):
    def setUp(self):
        super().setUp()
        self.verify = load('bootstrap_verify')
        self.assertIsNotNone(self.verify)
        self.key = self.root/'signing-key'
        subprocess.run([shutil.which('ssh-keygen'), '-q', '-t', 'ed25519', '-N', '', '-f', str(self.key)], check=True, capture_output=True)
        public = self.key.with_suffix('.pub').read_text().strip()
        (self.source/'release/allowed_signers').write_text('3waServerSetup-release '+public+'\n', encoding='utf-8')
        self.build()
        subprocess.run([shutil.which('ssh-keygen'), '-Y', 'sign', '-f', str(self.key), '-n', '3wa-release@3wa.tw',
                        str(self.output/'release-manifest.json')], check=True, capture_output=True)
        self.signers = self.source/'release/allowed_signers'

    def verified(self):
        return self.verify.verify_manifest(self.output, self.output/'install.sh', self.signers, 'v2026.10.25.001')

    def test_real_signature_archive_and_payload_verify_before_execution(self):
        manifest = self.verified()
        destination = self.root/'extracted'
        entry = self.verify.extract_payload(self.output/manifest['archive']['name'], manifest, destination, self.signers)
        self.assertEqual(entry, destination/'3waServerSetting/server_setting/release_update.py')
        self.assertTrue(entry.is_file())

    def test_signed_manifest_floor_is_checked_against_bootstrap_python(self):
        path=self.output/'release-manifest.json'
        original=json.loads(path.read_text())
        for requirement,version,allowed in (('>=3.10',(3,10),True),('>=3.12',(3,10),False),
                                            ('>=3.10',(3,12),True),('>=3.12',(3,12),True)):
            value=dict(original,python_requires=requirement,
                       validated_platforms=['ubuntu22.04','ubuntu24.04','ubuntu26.04'])
            path.write_text(json.dumps(value),encoding='utf-8')
            path.with_suffix('.json.sig').unlink()
            subprocess.run([shutil.which('ssh-keygen'),'-Y','sign','-f',str(self.key),'-n','3wa-release@3wa.tw',str(path)],
                           check=True,capture_output=True)
            with self.subTest(requirement=requirement,version=version),patch.object(self.verify.sys,'version_info',version):
                if allowed:self.assertEqual(self.verified()['python_requires'],requirement)
                else:
                    with self.assertRaisesRegex(ValueError,'Python 3.12'):self.verified()

    def test_bad_signature_manifest_installer_or_archive_prevents_extraction(self):
        path = self.output/'release-manifest.json'; original = path.read_bytes()
        path.write_bytes(original+b' ')
        with self.assertRaises(ValueError): self.verified()
        path.write_bytes(original)
        script = self.output/'install.sh'; raw = script.read_bytes(); script.write_bytes(raw+b'\n')
        with self.assertRaises(ValueError): self.verified()
        script.write_bytes(raw)
        manifest = self.verified(); archive = self.output/manifest['archive']['name']; archive.write_bytes(archive.read_bytes()+b'x')
        destination = self.root/'must-not-exist'
        with self.assertRaises(ValueError): self.verify.extract_payload(archive, manifest, destination, self.signers)
        self.assertFalse(destination.exists())

    def test_wrong_namespace_or_publisher_key_is_rejected(self):
        signature = self.output/'release-manifest.json.sig'; original = signature.read_bytes()
        signature.unlink()
        subprocess.run([shutil.which('ssh-keygen'), '-Y', 'sign', '-f', str(self.key), '-n', 'wrong-namespace',
                        str(self.output/'release-manifest.json')], check=True, capture_output=True)
        with self.assertRaises(ValueError): self.verified()
        signature.write_bytes(original)
        other = self.root/'other-key'
        subprocess.run([shutil.which('ssh-keygen'), '-q', '-t', 'ed25519', '-N', '', '-f', str(other)], check=True, capture_output=True)
        self.signers.write_text('3waServerSetup-release '+other.with_suffix('.pub').read_text().strip()+'\n', encoding='utf-8')
        with self.assertRaises(ValueError): self.verified()

    def test_authenticated_invalid_metadata_types_are_safely_refused(self):
        path = self.output/'release-manifest.json'
        original = json.loads(path.read_text())
        for changes in ({'schema':True}, {'commit':[]}, {'validated_platforms':[{}]}, {'tag':True},
                        {'python_requires':'>=3.9'},{'validated_platforms':['ubuntu20.04']}):
            value = dict(original, **changes)
            path.write_text(json.dumps(value),encoding='utf-8')
            (self.output/'release-manifest.json.sig').unlink()
            subprocess.run([shutil.which('ssh-keygen'), '-Y', 'sign', '-f', str(self.key), '-n', '3wa-release@3wa.tw',str(path)],
                           check=True,capture_output=True)
            with self.subTest(changes=changes), self.assertRaises(ValueError): self.verified()

    def test_authenticated_archive_still_rejects_traversal_symlink_and_extra_member(self):
        for kind in ('traversal', 'symlink', 'extra'):
            with self.subTest(kind=kind):
                manifest = self.verified()
                archive = self.root/(kind+'.tar.gz')
                with tarfile.open(archive, 'w:gz') as output:
                    p = tarfile.TarInfo('../escape' if kind=='traversal' else '3waServerSetting/extra')
                    if kind=='symlink': p.type=tarfile.SYMTYPE; p.linkname='/etc/passwd'; output.addfile(p)
                    else: p.size=1; output.addfile(p, io.BytesIO(b'x'))
                manifest['archive'].update(sha256=hashlib.sha256(archive.read_bytes()).hexdigest(), size=archive.stat().st_size)
                with self.assertRaises(ValueError): self.verify.extract_payload(archive, manifest, self.root/(kind+'-out'), self.signers)


if __name__ == '__main__':
    unittest.main()

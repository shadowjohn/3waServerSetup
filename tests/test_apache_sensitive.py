"""Synthetic matching tests only, NOT Apache configuration/runtime validation."""
from pathlib import Path
import re
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class SensitiveCandidateTests(unittest.TestCase):
    def test_candidate_exists_and_contains_three_explicit_deny_scopes(self):
        path = ROOT / 'config/apache-sensitive-files.example.conf'
        self.assertTrue(path.is_file(), 'Missing Apache sensitive-path candidate')
        text = path.read_text(encoding='utf-8')
        self.assertEqual(text.count('Require all denied'), 3)
        self.assertNotIn('<IfModule', text)

    def patterns(self):
        text = (ROOT / 'config/apache-sensitive-files.example.conf').read_text(encoding='utf-8')
        return {kind: re.compile(pattern) for kind, pattern in
                re.findall(r'<(FilesMatch|DirectoryMatch|LocationMatch) "([^"]+)">', text)}

    def test_synthetic_existing_fixtures_and_nested_paths(self):
        patterns = self.patterns()
        denied = ['.env', '.env.production', 'nested/.env.local', '.git/config',
                  'nested/.git/objects/synthetic', 'nested/.git', '.ENV', '.env.backup']
        allowed = ['.well-known/acme-challenge/synthetic', 'nested/.well-known/security.txt',
                   'index.html', 'nested/environment.txt', '.gitignore', '.envoy', 'env']
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for relative in denied + allowed:
                path = base / relative
                # .git is also a valid file (e.g. worktree pointer); use a separate fixture root.
                if relative == 'nested/.git':
                    path = base / 'pointer' / '.git'
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('SYNTHETIC PUBLIC TEST MARKER\n', encoding='utf-8')
                self.assertTrue(path.is_file())
                uri = '/' + relative
                matches = bool(patterns['LocationMatch'].search(uri))
                self.assertEqual(matches, relative in denied, relative)
                if '/.git/' in uri:
                    self.assertIsNotNone(patterns['DirectoryMatch'].search('/fixture' + uri))
                if path.name.startswith('.env') and relative in denied:
                    self.assertIsNotNone(patterns['FilesMatch'].search(path.name))


if __name__ == '__main__':
    unittest.main()

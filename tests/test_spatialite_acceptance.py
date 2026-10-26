"""Fixture entry-point contracts only; native 30,000-row proof is a separate VM run."""
import base64
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


FIXTURE = Path(__file__).resolve().with_name('spatialite-acceptance.php')
PACKAGED = FIXTURE.with_name('sqlite-smoke.php')
ENTRIES = ((FIXTURE, ()), (PACKAGED, ('--acceptance',)))
PHP = shutil.which('php')
if PHP and '.sbx-denybin' in PHP.lower():
    PHP = None


def php_string(value):
    return "base64_decode('" + base64.b64encode(str(value).encode('utf-8')).decode('ascii') + "')"


def acceptance_source():
    source = PACKAGED.read_text(encoding='utf-8')
    start = source.find('function spatialite_acceptance(')
    end = source.find('\nfunction spatialite_acceptance_cli(', start)
    if start < 0 or end < 0:
        raise AssertionError('The packaged probe must own the shared acceptance library')
    return source[start:end]


class SpatialiteFixtureSourceTests(unittest.TestCase):
    def test_plain_pdo_sql_and_official_sqlite3_loaders_remain_distinct(self):
        source = acceptance_source()
        self.assertIn("new PDO('sqlite::memory:')", source)
        self.assertIn("new SQLite3(':memory:')", source)
        self.assertIn('SELECT load_extension(', source)
        self.assertIn('->loadExtension($basename)', source)
        for forbidden in ('Pdo\\Sqlite', 'PDO::connect', 'ini_set(', 'SQLITE_ATTR_LOAD_EXTENSION',
                          '$_GET', '$_POST', '$_REQUEST'):
            with self.subTest(forbidden=forbidden):self.assertNotIn(forbidden, source)

    def test_workload_has_full_group_assertions_instead_of_bare_aggregate_columns(self):
        source = acceptance_source()
        for required in ('10000', 'COUNT(geom)', 'GROUP BY GeometryType(geom)', 'CoordDimension(geom)',
                         'ST_NDims(geom)', "'POINT'", "'LINESTRING'", "'POLYGON'", '$geometryFor($type, $id)'):
            with self.subTest(required=required):self.assertIn(required, source)


@unittest.skipUnless(PHP, 'PHP CLI unavailable; native SpatiaLite workload is not simulated')
class SpatialiteFixtureCliTests(unittest.TestCase):
    def invoke(self, *arguments, entry=FIXTURE, prefix=()):
        return subprocess.run([PHP, '-n', str(entry), *prefix, *arguments], capture_output=True,
                              text=True, encoding='utf-8', errors='replace', timeout=15)

    def values(self, result):
        self.assertEqual(result.stderr, '')
        return json.loads(result.stdout)['values']

    def test_php_syntax(self):
        for entry, _ in ENTRIES:
            with self.subTest(entry=entry.name):
                result = subprocess.run([PHP, '-n', '-l', str(entry)], capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_original_geometry_formulas_without_simulating_a_database(self):
        source = acceptance_source()
        generator = re.search(r'    (\$geometryFor = static function.*?\n    \};)', source, re.S)
        self.assertIsNotNone(generator, 'The original coordinate generator must be independently testable')
        program = generator[1] + ('echo json_encode(['
            '"point_first"=>$geometryFor("POINT",1),"point_last"=>$geometryFor("POINT",10000),'
            '"line_even"=>$geometryFor("LINESTRING",1),"line_odd"=>$geometryFor("LINESTRING",2),'
            '"line_last"=>$geometryFor("LINESTRING",10000),"polygon_last"=>$geometryFor("POLYGON",10000)]);')
        result = subprocess.run([PHP, '-n', '-r', program], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        wkts = json.loads(result.stdout)
        expected = {
            'point_first': ('POINT', [0, 0]), 'point_last': ('POINT', [9.999, 9.999]),
            'line_even': ('LINESTRING', [-10, -10, 10, 10]),
            'line_odd': ('LINESTRING', [-180, -90, -10.001, -10.001, -10.001, 10.001, 10.001, 10.001, 180, 90]),
            'line_last': ('LINESTRING', [-180, -90, -19.999, -19.999, -19.999, 19.999, 19.999, 19.999, 180, 90]),
            'polygon_last': ('POLYGON', [-19.999, -19.999, 19.999, -19.999, 19.999, 19.999, -19.999, 19.999, -19.999, -19.999]),
        }
        for label, (kind, numbers) in expected.items():
            with self.subTest(label=label):
                self.assertTrue(wkts[label].startswith(kind + '('))
                actual = list(map(float, re.findall(r'-?\d+(?:\.\d+)?', wkts[label])))
                self.assertEqual(len(actual), len(numbers))
                for observed, wanted in zip(actual, numbers):self.assertAlmostEqual(observed, wanted, places=12)

    def test_help_does_not_load_a_module(self):
        for entry, prefix in ENTRIES:
            with self.subTest(entry=entry.name):
                result = self.invoke('--help', entry=entry, prefix=prefix)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('--module', result.stdout)
                self.assertIn('pdo|sqlite3|all', result.stdout)

    def test_no_module_is_unverified_for_every_driver(self):
        for entry, prefix in ENTRIES:
            for arguments in ((), ('--driver=pdo',), ('--driver=sqlite3',), ('--driver=all',)):
                with self.subTest(entry=entry.name, arguments=arguments):
                    result = self.invoke(*arguments, entry=entry, prefix=prefix)
                    self.assertEqual(result.returncode, 2)
                    values = self.values(result)
                    self.assertEqual(values['status'], 'UNVERIFIED')
                    self.assertEqual(values['results'], [])

    def test_nonexistent_absolute_module_is_failure_not_missing_capability(self):
        missing = str(FIXTURE.parent / 'missing-spatialite-acceptance-module.so')
        self.assertFalse(Path(missing).exists())
        for entry, prefix in ENTRIES:
            for driver in ('pdo', 'sqlite3', 'all'):
                with self.subTest(entry=entry.name, driver=driver):
                    result = self.invoke('--driver=' + driver, '--module=' + missing, entry=entry, prefix=prefix)
                    self.assertEqual(result.returncode, 1)
                    values = self.values(result)
                    self.assertEqual(values['status'], 'FAIL')
                    self.assertTrue(values['results'])
                    self.assertTrue(all(r['status'] == 'FAIL' and r['stage'] == 'module' for r in values['results']))

    def test_unknown_empty_duplicate_or_missing_options_fail_before_workload(self):
        cases = [('--driver=auto',), ('--loader=native',), ('--module=',), ('--module',),
                 ('--driver',), ('--driver=pdo', '--driver=sqlite3'), ('unexpected',)]
        for entry, prefix in ENTRIES:
            for arguments in cases:
                with self.subTest(entry=entry.name, arguments=arguments):
                    result = self.invoke(*arguments, entry=entry, prefix=prefix)
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(self.values(result)['status'], 'FAIL')

    def test_packaged_selector_rejects_duplicates_even_with_help(self):
        for arguments in (('--acceptance', '--acceptance'), ('--help', '--acceptance', '--acceptance')):
            with self.subTest(arguments=arguments):
                result = self.invoke(*arguments, entry=PACKAGED)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(self.values(result)['status'], 'FAIL')

    def test_quick_probe_without_selector_keeps_its_text_output_and_loader_options(self):
        for arguments in ((), ('--loader=legacy-sql',)):
            with self.subTest(arguments=arguments):
                result = self.invoke(*arguments, entry=PACKAGED)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stderr, '')
                for label in ('SQLite3 RTree', 'PDO SQLite', 'RTree', 'SpatiaLite'):
                    self.assertIn('[UNVERIFIED] ' + label + ':', result.stdout)
                self.assertNotIn('"values"', result.stdout)
        help_result = self.invoke('--help', entry=PACKAGED)
        self.assertEqual(help_result.returncode, 0)
        self.assertIn('--loader=auto|native|legacy-sql', help_result.stdout)
        invalid = self.invoke('--loader=invalid', entry=PACKAGED)
        self.assertEqual(invalid.returncode, 1)
        self.assertIn('Invalid options', invalid.stderr)

    def test_include_only_defines_the_library_even_without_sqlite_extensions(self):
        for entry, _ in ENTRIES:
            with self.subTest(entry=entry.name):
                program = ('define("THREEWA_SQLITE_LIBRARY_ONLY",true);'
                           '$before=ini_get("sqlite3.extension_dir");require ' + php_string(entry) + ';'
                           'echo json_encode(["available"=>function_exists("spatialite_acceptance"),'
                           '"same_dir"=>$before===ini_get("sqlite3.extension_dir")]);')
                result = subprocess.run([PHP, '-n', '-r', program], capture_output=True, text=True,
                                        encoding='utf-8', errors='replace', timeout=15)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stderr, '')
                self.assertEqual(json.loads(result.stdout), {'available': True, 'same_dir': True})

    def test_library_cli_returns_instead_of_exiting_the_calling_script(self):
        program = ('define("THREEWA_SQLITE_LIBRARY_ONLY",true);require ' + php_string(PACKAGED) + ';'
                   'ob_start();$code=spatialite_acceptance_cli(["--driver=pdo"]);$output=ob_get_clean();'
                   'echo json_encode(["code"=>$code,"values"=>json_decode($output,true)["values"],"after"=>true]);')
        result = subprocess.run([PHP, '-n', '-r', program], capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=15)
        self.assertEqual((result.returncode, result.stderr), (0, ''))
        self.assertEqual(json.loads(result.stdout), {'code': 2, 'values': {'status': 'UNVERIFIED', 'results': [],
            'message': 'Supply --module with a trusted local native library to run this workload.'}, 'after': True})

    def test_library_include_supports_unicode_spaces_and_dollar_in_path(self):
        with tempfile.TemporaryDirectory(prefix='3wa 空間 $ ') as directory:
            copy = Path(directory) / 'fixture $ 羽山.php'
            copy.write_bytes(FIXTURE.read_bytes())
            (Path(directory) / PACKAGED.name).write_bytes(PACKAGED.read_bytes())
            program = 'require ' + php_string(copy) + ';echo function_exists("spatialite_acceptance") ? "INCLUDED" : "MISSING";'
            result = subprocess.run([PHP, '-n', '-r', program], capture_output=True, text=True,
                                    encoding='utf-8', errors='replace', timeout=15)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, 'INCLUDED', ''))

    def test_library_invalid_input_reports_failure_without_changing_directory(self):
        missing = str(FIXTURE.parent / 'missing-spatialite-acceptance-module.so')
        program = ('require ' + php_string(FIXTURE) + ';'
                   '$before=ini_get("sqlite3.extension_dir");'
                   '$report=spatialite_acceptance("pdo",' + php_string(missing) + ');'
                   'echo json_encode(["report"=>$report,"same_dir"=>$before===ini_get("sqlite3.extension_dir")]);')
        result = subprocess.run([PHP, '-n', '-r', program], capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, '')
        value = json.loads(result.stdout)
        self.assertTrue(value['same_dir'])
        self.assertEqual((value['report']['status'], value['report']['stage']), ('FAIL', 'module'))
        self.assertEqual(value['report']['total_rows'], 0)


if __name__ == '__main__':unittest.main()

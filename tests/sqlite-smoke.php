<?php
declare(strict_types=1);

// Include the shared workload without running either CLI probe.
if (defined('THREEWA_SQLITE_LIBRARY_ONLY') && THREEWA_SQLITE_LIBRARY_ONLY) {
    return;
}

// Standalone CLI probe. It never enables extension loading or changes PHP settings.
if (PHP_SAPI !== 'cli') {
    http_response_code(403);
    exit("CLI only.\n");
}

$acceptanceArguments = array_slice($argv, 1);
$acceptanceIndex = array_search('--acceptance', $acceptanceArguments, true);
if ($acceptanceIndex !== false) {
    array_splice($acceptanceArguments, $acceptanceIndex, 1);
    exit(spatialite_acceptance_cli($acceptanceArguments));
}

$options = [];
for ($index = 1; $index < $argc; $index++) {
    $argument = $argv[$index];
    if ($argument === '--help') {
        $options['help'] = true;
        continue;
    }
    if (preg_match('/^--(module|loader)(?:=(.*))?$/D', $argument, $match) !== 1) {
        fwrite(STDERR, "Unknown argument; use --help.\n");
        exit(1);
    }
    $key = $match[1];
    $value = $match[2] ?? null;
    if ($value === null && isset($argv[$index + 1]) && !str_starts_with($argv[$index + 1], '--')) {
        $value = $argv[++$index];
    }
    if ($value === null || $value === '' || isset($options[$key])) {
        fwrite(STDERR, "Missing value or duplicate option; use --help.\n");
        exit(1);
    }
    $options[$key] = $value;
}
if (isset($options['help'])) {
    echo "Usage: php sqlite-smoke.php [--module=/absolute/path/mod_spatialite.so] [--loader=auto|native|legacy-sql]\n";
    echo "auto: PHP 8.4+ Pdo\\Sqlite API only; legacy-sql explicitly tests an already patched PDO.\n";
    echo "Exit: 0 all passed, 1 failure, 2 incomplete/cannot verify. Native modules execute trusted code.\n";
    exit(0);
}
$loader = $options['loader'] ?? 'auto';
if (!is_string($loader) || !in_array($loader, ['auto', 'native', 'legacy-sql'], true)
    || (isset($options['module']) && !is_string($options['module']))) {
    fwrite(STDERR, "Invalid options; use --help.\n");
    exit(1);
}

$results = [];
function recordResult(string $name, string $status, string $detail): void
{
    global $results;
    $results[] = $status;
    echo "[$status] $name: $detail\n";
}
function requireTrue(bool $condition, string $message): void
{
    if (!$condition) {
        throw new RuntimeException($message);
    }
}
function database(): PDO
{
    $db = class_exists('Pdo\\Sqlite')
        ? new \Pdo\Sqlite('sqlite::memory:')
        : new PDO('sqlite::memory:');
    $db->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
    return $db;
}

echo 'PHP ' . PHP_VERSION . ' / ' . PHP_SAPI . " / loader=$loader\n";
$pdoAvailable = class_exists('PDO') && in_array('sqlite', PDO::getAvailableDrivers(), true);
// SQLite3 is a separate PHP extension and may use a different SQLite build.
if (!extension_loaded('sqlite3') || !class_exists('SQLite3')) {
    recordResult('SQLite3 RTree', 'UNVERIFIED', 'Independent SQLite3 extension unavailable');
} else {
    try {
        $sqlite3 = new SQLite3(':memory:');
        $sqlite3->enableExceptions(true);
        requireTrue($sqlite3->exec('CREATE VIRTUAL TABLE boxes USING rtree(id,minX,maxX,minY,maxY)'), 'SQLite3 RTree create failed');
        requireTrue($sqlite3->exec('INSERT INTO boxes VALUES (1,0,2,0,2),(2,10,12,10,12),(3,2,4,2,4)'), 'SQLite3 RTree insert failed');
        $rows = $sqlite3->query('SELECT id FROM boxes WHERE minX<=3 AND maxX>=1 AND minY<=3 AND maxY>=1 ORDER BY id');
        requireTrue($rows !== false, 'SQLite3 overlap query failed');
        $ids = [];
        while (($row = $rows->fetchArray(SQLITE3_NUM)) !== false) {
            $ids[] = (int) $row[0];
        }
        requireTrue($ids === [1, 3], 'SQLite3 overlap positive/negative result mismatch');
        requireTrue((int) $sqlite3->querySingle('SELECT COUNT(*) FROM boxes WHERE minX<=21 AND maxX>=20 AND minY<=21 AND maxY>=20') === 0, 'SQLite3 disjoint rectangle unexpectedly matched');
        $sqlite3->close();
        recordResult('SQLite3 RTree', 'PASS', 'SQLite ' . SQLite3::version()['versionString'] . '; create / insert / overlap positives and disjoint negatives');
    } catch (Throwable $error) {
        recordResult('SQLite3 RTree', 'FAIL', $error->getMessage());
    }
}

if (!$pdoAvailable) {
    recordResult('PDO SQLite', 'UNVERIFIED', 'PDO SQLite driver unavailable');
} else {
    try {
        $db = database();
        $version = $db->query('SELECT sqlite_version()')->fetchColumn();
        requireTrue(is_string($version) && $version !== '', 'Missing SQLite version');
        $db->exec('CREATE TABLE probe (id INTEGER PRIMARY KEY, value TEXT NOT NULL)');
        $stmt = $db->prepare('INSERT INTO probe (id, value) VALUES (?, ?)');
        $stmt->execute([1, '羽山']);
        requireTrue($db->query('SELECT value FROM probe WHERE id=1')->fetchColumn() === '羽山', 'PDO round trip mismatch');
        recordResult('PDO SQLite', 'PASS', 'SQLite ' . $version . '; prepared statement / Unicode round trip');
    } catch (Throwable $error) {
        recordResult('PDO SQLite', 'FAIL', $error->getMessage());
    }
}

// A separate connection ensures SpatiaLite cannot supply or change the RTree capability.
if (!$pdoAvailable) {
    recordResult('RTree', 'UNVERIFIED', 'PDO SQLite unavailable');
} else {
    try {
        $rtree = database();
        $rtree->exec('CREATE VIRTUAL TABLE boxes USING rtree(id, minX, maxX, minY, maxY)');
        $rtree->exec('INSERT INTO boxes VALUES (1,0,2,0,2),(2,10,12,10,12),(3,2,4,2,4)');
        $query = $rtree->prepare('SELECT id FROM boxes WHERE minX <= :maxX AND maxX >= :minX AND minY <= :maxY AND maxY >= :minY ORDER BY id');
        $query->execute([':minX' => 1, ':maxX' => 3, ':minY' => 1, ':maxY' => 3]);
        requireTrue(array_map('intval', $query->fetchAll(PDO::FETCH_COLUMN)) === [1, 3], 'Overlap positive/negative result mismatch');
        $query->execute([':minX' => 20, ':maxX' => 21, ':minY' => 20, ':maxY' => 21]);
        requireTrue($query->fetchAll(PDO::FETCH_COLUMN) === [], 'Disjoint rectangle unexpectedly matched');
        recordResult('RTree', 'PASS', 'Virtual table / insert / overlap positives and disjoint negatives');
    } catch (Throwable $error) {
        recordResult('RTree', 'FAIL', $error->getMessage());
    }
}

if (!$pdoAvailable) {
    recordResult('SpatiaLite', 'UNVERIFIED', 'PDO SQLite unavailable');
} elseif (!isset($options['module']) || $options['module'] === '') {
    recordResult('SpatiaLite', 'UNVERIFIED', 'No module supplied; use --module with a trusted local native library');
} else {
    try {
        $module = $options['module'];
        requireTrue(!str_starts_with($module, '\\\\') && !str_starts_with($module, '//'), 'UNC module paths are not accepted');
        requireTrue(preg_match('~^(?:/|[A-Za-z]:[\\\\/])~', $module) === 1, 'Module path must be absolute');
        $resolved = realpath($module);
        requireTrue($resolved !== false && is_file($resolved) && is_readable($resolved), 'Module is not a readable local file');
        $spatial = database();
        if ($loader === 'legacy-sql') {
            $spatial->query('SELECT load_extension(' . $spatial->quote($resolved) . ')')->fetchColumn();
        } elseif (method_exists($spatial, 'loadExtension')) {
            $spatial->loadExtension($resolved);
        } else {
            recordResult('SpatiaLite', 'UNVERIFIED', 'Native PDO API unavailable; explicit --loader=legacy-sql required for an already patched PDO');
            $spatial = null;
        }
        if ($spatial !== null) {
            $version = $spatial->query('SELECT spatialite_version()')->fetchColumn();
            requireTrue(is_string($version) && trim($version) !== '', 'Missing SpatiaLite version');
            $area = $spatial->query("SELECT ST_Area(GeomFromText('POLYGON((0 0,4 0,4 3,0 3,0 0))',4326))")->fetchColumn();
            requireTrue(is_numeric($area) && abs((float) $area - 12.0) < 0.000001, 'Spatial polygon area mismatch');
            recordResult('SpatiaLite', 'PASS', 'SpatiaLite ' . $version . '; planar polygon area = 12');
        }
    } catch (Throwable $error) {
        recordResult('SpatiaLite', 'FAIL', $error->getMessage());
    }
}

exit(in_array('FAIL', $results, true) ? 1 : (in_array('UNVERIFIED', $results, true) ? 2 : 0));

/** Execute the same memory workload using one explicitly selected driver. */
function spatialite_acceptance(string $driver, string $module): array
{
    $started = microtime(true);
    $report = [
        'status' => 'FAIL', 'stage' => 'driver', 'driver' => $driver, 'sapi' => PHP_SAPI,
        'php_version' => PHP_VERSION, 'sqlite_version' => null, 'spatialite_version' => null,
        'class_loaded' => null, 'loader' => null, 'module' => $module, 'module_realpath' => null,
        'module_sha256' => null, 'rows_per_type' => 10000, 'total_rows' => 0, 'tables' => [],
        'transactions_committed' => 0,
        'loaded_extensions' => ['pdo_sqlite' => extension_loaded('pdo_sqlite'), 'sqlite3' => extension_loaded('sqlite3')],
    ];
    $db = null;
    $statement = null;
    $transactionOpen = false;
    $require = static function (bool $condition, string $message): void {
        if (!$condition) {
            throw new RuntimeException($message);
        }
    };
    $geometryFor = static function (string $type, int $id): string {
        $i = $id - 1;
        if ($type === 'POINT') {
            $coordinate = $i / 1000.0;
            return "POINT($coordinate $coordinate)";
        }
        $low = -10.0 - ($i / 1000.0);
        $high = 10.0 + ($i / 1000.0);
        if ($type === 'LINESTRING') {
            return ($i % 2) === 1
                ? "LINESTRING(-180.0 -90.0,$low $low,$low $high,$high $high,180.0 90.0)"
                : "LINESTRING($low $low,$high $high)";
        }
        return "POLYGON(($low $low,$high $low,$high $high,$low $high,$low $low))";
    };
    try {
        $require(in_array($driver, ['pdo', 'sqlite3'], true), 'Driver must be pdo or sqlite3.');
        $report['stage'] = 'module';
        $require(!str_starts_with($module, '\\\\') && !str_starts_with($module, '//'), 'UNC module paths are not accepted.');
        $require(preg_match('~^(?:/|[A-Za-z]:[\\\\/])~D', $module) === 1, 'Module path must be absolute.');
        $resolved = realpath($module);
        $require($resolved !== false && is_file($resolved) && is_readable($resolved), 'Module is not a readable local file.');
        $report['module_realpath'] = $resolved;
        $report['module_sha256'] = hash_file('sha256', $resolved);
        $report['stage'] = 'capability';
        if (($driver === 'pdo' && (!class_exists('PDO', false) || !in_array('sqlite', PDO::getAvailableDrivers(), true)))
            || ($driver === 'sqlite3' && !class_exists('SQLite3', false))) {
            $report['status'] = 'UNVERIFIED';
            throw new RuntimeException('The selected SQLite PHP driver is unavailable.');
        }
        $report['stage'] = 'connect';
        if ($driver === 'pdo') {
            $db = new PDO('sqlite::memory:');
            $db->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
            $db->exec("PRAGMA foreign_keys = ON");
            $db->exec("PRAGMA trusted_schema = ON");
            $db->exec("PRAGMA database_list");
            $report['loader'] = 'sql_load_extension';
            $report['stage'] = 'load';
            $db->query('SELECT load_extension(' . $db->quote($module) . ')')->fetchColumn();
        } else {
            $report['stage'] = 'sqlite3_extension_dir';
            $directory = (string) ini_get('sqlite3.extension_dir');
            $report['sqlite3_extension_dir'] = $directory;
            $require($directory !== '', 'sqlite3.extension_dir is not configured; no override was attempted.');
            $basename = basename($module);
            $configuredModule = realpath(rtrim($directory, '/\\') . DIRECTORY_SEPARATOR . $basename);
            $require($configuredModule !== false && $configuredModule === $resolved,
                     'Configured sqlite3.extension_dir does not resolve this module basename.');
            $db = new SQLite3(':memory:');
            $db->enableExceptions(true);
            $report['loader'] = 'sqlite3_load_extension';
            $report['stage'] = 'load';
            $require($db->loadExtension($basename), 'SQLite3 could not load the module from its configured directory.');
        }
        $report['class_loaded'] = get_class($db);
        $exec = static function (string $sql) use (&$db, $require): void {
            $require($db->exec($sql) !== false, 'SQL execution failed.');
        };
        $scalar = static function (string $sql) use (&$db, $driver) {
            return $driver === 'pdo' ? $db->query($sql)->fetchColumn() : $db->querySingle($sql);
        };
        $rows = static function (string $sql) use (&$db, $driver, $require): array {
            if ($driver === 'pdo') {
                return $db->query($sql)->fetchAll(PDO::FETCH_ASSOC);
            }
            $result = $db->query($sql);
            $require($result !== false, 'SQLite3 result is unavailable.');
            $values = [];
            while (($row = $result->fetchArray(SQLITE3_ASSOC)) !== false) {
                $values[] = $row;
            }
            $result->finalize();
            return $values;
        };
        $report['sqlite_version'] = $scalar('SELECT sqlite_version()');
        $report['spatialite_version'] = $scalar('SELECT spatialite_version()');
        $require(is_string($report['sqlite_version']) && $report['sqlite_version'] !== '', 'SQLite version missing.');
        $require(is_string($report['spatialite_version']) && $report['spatialite_version'] !== '', 'SpatiaLite version missing.');
        $report['stage'] = 'metadata';
        // InitSpatialMetadata(1) manages its own transaction before data BEGINs.
        $require((int) $scalar('SELECT InitSpatialMetadata(1)') === 1, 'Spatial metadata initialization failed.');
        $definitions = ['POINT' => ['test_pt', 1], 'LINESTRING' => ['test_ln', 2], 'POLYGON' => ['test_pg', 3]];
        foreach ($definitions as $type => [$table, $typeCode]) {
            $exec("CREATE TABLE $table (id INTEGER NOT NULL PRIMARY KEY, name TEXT NOT NULL)");
            $require((int) $scalar("SELECT AddGeometryColumn('$table','geom',4326,'$type','XY')") === 1,
                     'AddGeometryColumn failed: ' . $type);
        }
        foreach ($definitions as $type => [$table, $typeCode]) {
            $report['stage'] = 'insert_' . strtolower($type);
            $exec('BEGIN');
            $transactionOpen = true;
            if ($type !== 'POINT') {
                $statement = $db->prepare("INSERT INTO $table (id,name,geom) VALUES (:id,:name,GeomFromText(:geom,4326))");
                $require($statement !== false, 'Prepare failed: ' . $type);
            }
            for ($id = 1; $id <= 10000; $id++) {
                $geometry = $geometryFor($type, $id);
                if ($type === 'POINT') {
                    // Keep the supplied POINT workload as raw SQL, not a prepared replacement.
                    $exec("INSERT INTO test_pt (id,name,geom) VALUES ($id,'test POINT #$id',GeomFromText('$geometry',4326))");
                    continue;
                }
                $name = 'test ' . $type . ' #' . $id;
                if ($driver === 'pdo') {
                    $require($statement->execute([':id' => $id, ':name' => $name, ':geom' => $geometry]), 'Prepared insert failed.');
                } else {
                    $statement->reset();
                    $statement->clear();
                    $require($statement->bindValue(':id', $id, SQLITE3_INTEGER), 'Integer bind failed.');
                    $require($statement->bindValue(':name', $name, SQLITE3_TEXT), 'Name bind failed.');
                    $require($statement->bindValue(':geom', $geometry, SQLITE3_TEXT), 'Geometry bind failed.');
                    $result = $statement->execute();
                    $require($result !== false, 'Prepared insert failed.');
                    $result->finalize();
                }
            }
            if ($driver === 'sqlite3' && $statement !== null) {
                $statement->close();
            }
            $statement = null;
            $exec('COMMIT');
            $transactionOpen = false;
            $report['transactions_committed']++;
        }
        $report['stage'] = 'verify';
        foreach ($definitions as $type => [$table, $typeCode]) {
            $groups = $rows("SELECT COUNT(*) AS rows_count, COUNT(geom) AS nonnull_count,
                GeometryType(geom) AS geometry_type, ST_GeometryType(geom) AS st_geometry_type,
                Srid(geom) AS srid, CoordDimension(geom) AS coordinate_dimension, ST_NDims(geom) AS dimensions
                FROM $table
                GROUP BY GeometryType(geom), ST_GeometryType(geom), Srid(geom), CoordDimension(geom), ST_NDims(geom)");
            $require(count($groups) === 1, 'Expected one complete geometry group: ' . $type);
            $group = $groups[0];
            $require((int) $group['rows_count'] === 10000 && (int) $group['nonnull_count'] === 10000,
                     'Expected 10000 non-NULL geometries: ' . $type);
            $require($group['geometry_type'] === $type && $group['st_geometry_type'] === $type,
                     'Geometry type mismatch: ' . $type);
            $require((int) $group['srid'] === 4326 && $group['coordinate_dimension'] === 'XY'
                     && (int) $group['dimensions'] === 2, 'Expected SRID4326 / XY / 2 dimensions: ' . $type);
            $metadata = $rows("SELECT geometry_type,coord_dimension,srid FROM geometry_columns
                WHERE f_table_name='$table' AND f_geometry_column='geom'");
            $require(count($metadata) === 1 && (int) $metadata[0]['geometry_type'] === $typeCode
                     && (int) $metadata[0]['coord_dimension'] === 2 && (int) $metadata[0]['srid'] === 4326,
                     'Spatial metadata mismatch: ' . $type);
            $report['tables'][$type] = ['table' => $table, 'groups' => $groups, 'metadata' => $metadata,
                                       'rows' => 10000, 'nonnull' => 10000];
            foreach ([1, 2, 10000] as $sampleId) {
                $expected = $geometryFor($type, $sampleId);
                $require((int) $scalar("SELECT hex(geom)=hex(GeomFromText('$expected',4326)) FROM $table WHERE id=$sampleId") === 1,
                         'Original workload coordinate mismatch: ' . $type . ' / ' . $sampleId);
                $report['tables'][$type]['samples'][$sampleId] = $expected;
            }
            if ($type === 'LINESTRING') {
                $vertices = $rows('SELECT NumPoints(geom) AS vertices,COUNT(*) AS rows_count FROM test_ln GROUP BY NumPoints(geom) ORDER BY vertices');
                $require(count($vertices) === 2 && (int) $vertices[0]['vertices'] === 2 && (int) $vertices[0]['rows_count'] === 5000
                         && (int) $vertices[1]['vertices'] === 5 && (int) $vertices[1]['rows_count'] === 5000,
                         'Expected 5000 two-point and 5000 five-point LINESTRINGs.');
                $report['tables'][$type]['vertices'] = $vertices;
            }
            $report['total_rows'] += (int) $group['rows_count'];
        }
        $require($report['total_rows'] === 30000 && $report['transactions_committed'] === 3, 'Incomplete workload.');
        $require(hash_file('sha256', $resolved) === $report['module_sha256'], 'Module changed during the workload.');
        $report['stage'] = 'complete';
        $report['status'] = 'PASS';
    } catch (Throwable $error) {
        $report['error'] = $error->getMessage();
    } finally {
        try {
            if ($driver === 'sqlite3' && $statement !== null) {
                $statement->close();
            }
            $statement = null;
            if ($transactionOpen && $db !== null) {
                $db->exec('ROLLBACK');
            }
            if ($driver === 'sqlite3' && $db !== null) {
                $db->close();
            }
            $db = null;
        } catch (Throwable $error) {
            $report['status'] = 'FAIL';
            $report['error'] = ($report['error'] ?? '') . ' Close/rollback: ' . $error->getMessage();
        }
        $report['elapsed_seconds'] = round(microtime(true) - $started, 6);
    }
    return $report;
}

function spatialite_acceptance_cli(array $arguments): int
{
    $render = static function (string $status, array $results, ?string $message = null): void {
        $values = ['status' => $status, 'results' => $results];
        if ($message !== null) {
            $values['message'] = $message;
        }
        echo json_encode(['sapi' => PHP_SAPI, 'values' => $values], JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR) . "\n";
    };
    $options = [];
    try {
        if (in_array('--acceptance', $arguments, true)) {
            throw new RuntimeException('Duplicate or unexpected acceptance selector; use --help.');
        }
        for ($index = 0; $index < count($arguments); $index++) {
            $argument = $arguments[$index];
            if ($argument === '--help') {
                echo "Usage: php sqlite-smoke.php --acceptance --module=/absolute/path/mod_spatialite.so [--driver=pdo|sqlite3|all]\n";
                echo "Native code is loaded only from an explicit local module. Each driver inserts 10000 rows per geometry type.\n";
                echo "Exit: 0 PASS, 1 FAIL, 2 UNVERIFIED. No PHP gate or extension directory is changed.\n";
                return 0;
            }
            if (preg_match('/^--(module|driver)(?:=(.*))?$/D', $argument, $match) !== 1) {
                throw new RuntimeException('Unknown option; use --help.');
            }
            $key = $match[1];
            $value = $match[2] ?? null;
            if ($value === null && isset($arguments[$index + 1]) && !str_starts_with($arguments[$index + 1], '--')) {
                $value = $arguments[++$index];
            }
            if ($value === null || $value === '' || isset($options[$key])) {
                throw new RuntimeException('Missing value or duplicate option; use --help.');
            }
            $options[$key] = $value;
        }
        $driver = $options['driver'] ?? 'all';
        if (!in_array($driver, ['pdo', 'sqlite3', 'all'], true)) {
            throw new RuntimeException('Driver must be pdo, sqlite3 or all.');
        }
    } catch (Throwable $error) {
        $render('FAIL', [], $error->getMessage());
        return 1;
    }
    if (!isset($options['module'])) {
        $render('UNVERIFIED', [], 'Supply --module with a trusted local native library to run this workload.');
        return 2;
    }
    $reports = [];
    foreach ($driver === 'all' ? ['pdo', 'sqlite3'] : [$driver] as $selected) {
        $reports[] = spatialite_acceptance($selected, $options['module']);
    }
    $statuses = array_column($reports, 'status');
    $status = in_array('FAIL', $statuses, true) ? 'FAIL' : (in_array('UNVERIFIED', $statuses, true) ? 'UNVERIFIED' : 'PASS');
    $render($status, $reports);
    return $status === 'PASS' ? 0 : ($status === 'FAIL' ? 1 : 2);
}

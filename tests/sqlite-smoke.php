<?php
declare(strict_types=1);

// Standalone CLI probe. It never enables extension loading or changes PHP settings.
if (PHP_SAPI !== 'cli') {
    http_response_code(403);
    exit("CLI only.\n");
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

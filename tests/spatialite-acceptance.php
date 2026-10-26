<?php
declare(strict_types=1);

// Source-tree alias; releases use sqlite-smoke.php --acceptance.
if (!defined('THREEWA_SQLITE_LIBRARY_ONLY')) {
    define('THREEWA_SQLITE_LIBRARY_ONLY', true);
}
require_once __DIR__ . '/sqlite-smoke.php';

// A private FPM wrapper may include the function, but this file is not a web endpoint.
if (!isset($_SERVER['SCRIPT_FILENAME']) || realpath((string) $_SERVER['SCRIPT_FILENAME']) !== __FILE__) {
    return;
}
if (PHP_SAPI !== 'cli') {
    http_response_code(403);
    exit("CLI entry point only.\n");
}
exit(spatialite_acceptance_cli(array_slice($argv, 1)));

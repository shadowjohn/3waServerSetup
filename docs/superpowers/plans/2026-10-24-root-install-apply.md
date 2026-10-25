# Root installation and configuration implementation plan

> **For agentic workers:** Execute the independent engine, recipes and UI tasks in parallel, with TDD and an integration review before VM deployment.

**Goal:** Root can install a selected service, load recommendations, save/apply its settings, verify the defined scope and restore a configuration transaction; fix Ctrl+Right/Left focus bounce.

**Architecture:** Keep the existing Textual UI and pixel mascot. Add a standard-library transaction manager and explicit Ubuntu recipes. Root operations remain opt-in per page; startup stays observational. Product source starts from the reviewed latest prototype snapshot, isolated from both existing checkouts.

**Tech Stack:** Python 3.10+, Textual 8.2.8, Ubuntu native packages/systemd, unittest. No new Python dependencies.

**Spec:** `docs/design.md` plus the user's explicit instruction to implement root installation/recommendation/save/apply and test on 192.168.137.26.

## Global constraints

- Source worktree: D:\mytools\3waServerSetup-root-apply, branch 3waAI/root-install-apply. Do not modify the prototype checkout or main checkout. No push/release.
- First writable capability target is Ubuntu 26.04. Other versions remain observational until verified.
- Implement existing nonempty recipe pages: apache, php, vsftpd, samba, mariadb, time. Startup/storage stay explicitly unsupported for writes; firewall remains an offline candidate until its separate guarded deployment workflow is implemented.
- No SSH/firewall/network/account/password/schema/storage changes. No native SQLite extension loading.
- Service installation never triggers a system-wide upgrade. User subsequently added explicit apt update/upgrade: a separate reviewed operation simulates dependency-aware no-remove upgrades and reports reboot requirements. No autoremove, source changes, automatic reboot or silent mod_php/prefork migration.
- Default runner uses fixed trusted executable directories, argv, bounded timeouts and safe diagnostics, never shell interpolation.
- Only ordinary source/code assets enter the new package; no key, real config, backup, venv or validation output.
- All production root code deployed to the VM must be root-owned and non-user-writable.

## Review focus

- Nonroot/unsupported OS cannot mutate state; unsupported pages cannot pretend to apply.
- Stale preview, symlink escape, malformed values and modified saved plans are rejected before writes.
- Partial validation/service failures restore reversible files and prior service states; packages are not claimed reversible.
- No-op does not reload/restart services; restore refuses to overwrite external edits.
- PHP CLI is not FPM evidence; verify FPM-selected values independently. FTP/Samba/DB health labels state their exact scope.

## Shared interfaces

`server_setting/root_actions.py` owns `Host`, `RootManager`, `RootActionError`.

Host interface: `root: Path` (filesystem prefix, `/` in production), `path(absolute_path)->Path`, `read(absolute_path)->str|None`, `run(argv, timeout=30)->subprocess.CompletedProcess[str]`. `path`/`read` reject unsafe traversal/symlinks. Production commands use a fixed allowlist/trusted system PATH, LC_ALL=C, noninteractive apt and NEEDRESTART_MODE=l. Test doubles replace only the OS/command boundary; file edits use real temporary files.

RootManager methods:

- `RootManager(host=None)` creates no state on startup.
- `capabilities(module)->dict`: packages, installed, service unit/active/enabled, supported/detail.
- `recommended(module)->dict`, `load_saved(module)->dict|None` (saved values/optional service targets).
- `plan(module, values=None, service_active=None, service_enabled=None)->dict` (None values means FIELDS recommendation).
- `install_plan(module)->dict`, `install(module)->dict` (explicit selected package action).
- `apply(plan)->dict`, `restore_last(module)->dict`.
- `apt_plan(operation)->dict`, `apt_run(preview)->dict`: explicit update/upgrade, strict index errors, dpkg/index fingerprint guard, modified conffile preservation and no package rollback claim.

Plan dictionaries contain schema/module/values/service_active/service_enabled, files (`path`, `before_sha256`, `content`, `mode`), validation command argv lists (`validate`), application argv lists (`apply_commands`), observations, service snapshot, notes/health_scope, and a stable `fingerprint`. Core computes the fingerprint, rebuilds the plan from trusted recipes before execution and rejects changes. Only complete allowed FIELDS targets are accepted; no arbitrary file/command supplied by a caller is executed.

`server_setting/root_recipes.py` consumes the Host interface without importing the manager and provides:

- `packages(module)->list[str]`
- `capabilities(host,module)->dict`
- `build_plan(host,module,values,service_active=None,service_enabled=None)->dict` (fingerprint added by manager)
- `post_install(host,module)->dict` for narrowly scoped distro Apache/FPM integration when both exist and no legacy mod_php migration is needed.
- `verify_plan(host,plan)->dict`: exact health scope, verified boolean/detail; raises on a checked value mismatch. Inactive services are explicitly saved-but-unverified, not claimed healthy.

Root manager result keys: `status`, `module`, `message`, optional `transaction_id`, `verification`. Statuses include APPLIED, NO_CHANGE, RESTORED, INSTALLED; scope is reported separately.

Persistence: root-only transaction journals under `/var/lib/3wa-server-setup/transactions`; backups under `/opt/3waServerSetting/backups`; successful nonsecret recipes under `/opt/3waServerSetting/config/recipes`. Preserve original bytes/mode/owner and applicable xattrs; backup mode never wider than original/0600. Atomic writes, flock, before/after hashes, rollback on checked failure, and restore external-edit guard.

## Task 1: Root transaction engine

Files: root_actions.py, tests/test_root_actions.py.

- [ ] Write and run failing tests for root/OS gate, atomic save/backup/restore, stale/tampered plan, symlink rejection, rollback and no-op.
- [ ] Implement Host and RootManager against the recipes contract.
- [ ] Persist successful target recipes and safe transaction metadata; never log raw configuration/secret output.
- [ ] Verify real temporary filesystem behavior and the full test suite.

## Task 2: Ubuntu service recipes

Files: root_recipes.py, tests/test_root_recipes.py.

- [ ] Write and run failing tests for preserving unrelated config, all six module targets, conflicting/multiple FPM targets, no credentials reads and truthful health scopes.
- [ ] Apache: tool-owned included conf; PHP: matched FPM version, tool-owned late ini, FPM syntax/effective-value check; vsftpd: preserve unrelated config and honest structural/daemon scope; Samba: managed global block and testparm; MariaDB: managed server drop-in, native validation and socket-only selected variable verification; Time: timedatectl with repeated properties and validated timezone.
- [ ] Use distro package names (apache2; php-fpm/php-cli/php-sqlite3; vsftpd; samba; mariadb-server), exact service detection, no root credentials file reads.
- [ ] Verify recipe tests and full suite; explain unsupported existing layouts instead of guessing.

## Task 3: Root TUI and CLI

Files: app.py, __main__.py, probes.py, tests/test_tui_pilot.py, tests/test_root_cli.py (and focused UI tests if useful).

- [ ] Add Ctrl+Right/Left regressions with old-pane Button focus; observe RED, focus page tabs before changing active, verify GREEN.
- [ ] Keep nonroot view and pixel mascot. Root pages expose install, recommendations, custom save/apply and restore-last with clear review/confirmation; service active and boot-enabled targets are separate.
- [ ] Long operations run off the UI thread, show progress/result and prevent quitting mid-transaction. Refresh current observations afterwards.
- [ ] CLI: `--root-plan MODULE [--values JSON] [--start|--stop] [--enable|--disable]`, `--root-apply MODULE ... --confirm`, `--root-install MODULE --confirm`, `--root-restore MODULE --confirm`; mutations require explicit confirm and root. Keep existing status/text/smoke interfaces.
- [ ] Fix timedatectl probe property selection; run full suite.

## Task 4: Integration and VM acceptance

- [ ] Review combined diff, root-path trust and journals, run all tests/compile/pip check/diff check.
- [ ] Package reviewed source with hashes; stage root-owned `/opt/3waServerSetting` on .26 with a root-owned venv from the existing lock (never run root code from john-writable directories).
- [ ] On the VM install selected services through the new manager, apply recommendations/custom values, validate real services/FPM values, no-op, stale plan rejection and restore. Preserve original VM backup.
- [ ] Real root TTY: install/apply review, save/restore and Ctrl+Right from a focused form button. Record evidence and exact health limitations.
- [ ] Write concise usage/deployment/validation documentation and final status. No commit/push unless separately requested.

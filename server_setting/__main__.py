"""Launcher with explicit root commands; never bootstraps dependencies."""
import argparse
import asyncio
import getpass
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import warnings
from . import VERSION, revision, version_info, update_management_available, release_public, valid_release_tag, network_management_available, network_public
from .probes import collect, root_management_available
from .model import FIELDS

ROOT_MODULES = tuple(module for module, fields in FIELDS.items() if fields)
MARIADB_SECURITY_ERROR_CODES = {'MARIADB_NOT_READY', 'MARIADB_UNSUPPORTED', 'MARIADB_AUDIT_ACTIVE',
                               'MARIADB_PASSWORD_REQUIRED', 'MARIADB_INVALID_PASSWORD', 'MARIADB_SQL_FAILED',
                               'MARIADB_PASSWORD_VERIFY_FAILED', 'MARIADB_ROLLED_BACK',
                               'TAMPERED_PLAN', 'STALE_PLAN', 'RECOVERY_REQUIRED'}


class CommandParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def json_response(value, code=0):
    print(json.dumps(value, ensure_ascii=True, indent=2, default=str))
    return code


def root_command(args, manager=None):
    operation, module = next((name, getattr(args, 'root_' + name))
                             for name in ('plan', 'apply', 'install', 'restore')
                             if getattr(args, 'root_' + name) is not None)
    if not root_management_available():
        return json_response({'status': 'DENIED', 'code': 'ROOT_REQUIRED', 'module': module,
                              'message': 'Root management requires root on Ubuntu 26.04.'}, 1)
    if operation != 'plan' and not args.confirm:
        return json_response({'status': 'ERROR', 'code': 'CONFIRMATION_REQUIRED', 'module': module,
                              'message': 'Mutations require --confirm.'}, 2)
    try:
        values = None
        if args.values is not None:
            from firewall_plan import unique_object
            values = json.loads(args.values, object_pairs_hook=unique_object)
            if not isinstance(values, dict):
                raise ValueError('--values must be a JSON object')
        if operation in {'install', 'restore'} and (values is not None or
                args.service_active is not None or args.service_enabled is not None):
            raise ValueError('--values and service targets apply only to --root-plan / --root-apply')
        if manager is None:
            from .root_actions import RootManager
            manager = RootManager()
        if operation in {'plan', 'apply'}:
            plan = manager.plan(module, values, service_active=args.service_active,
                                service_enabled=args.service_enabled)
            if operation == 'plan':
                return json_response({'status': 'PLANNED', 'module': module, 'plan': plan})
            result = manager.apply(plan)
        elif operation == 'install':
            result = manager.install(module)
        else:
            result = manager.restore_last(module)
        return json_response(result)
    except ValueError as error:
        return json_response({'status': 'ERROR', 'code': 'INVALID_ARGUMENT', 'module': module,
                              'message': str(error)}, 2)
    except Exception as error:
        return json_response({'status': 'FAILED', 'code': getattr(error, 'code', 'ROOT_OPERATION_FAILED'),
                              'module': module, 'message': str(error)}, 1)


def apt_command(args, manager=None):
    operation = args.apt_plan or ('update' if args.apt_update else 'upgrade')
    if not root_management_available():
        return json_response({'status': 'DENIED', 'code': 'ROOT_REQUIRED', 'module': 'apt',
                              'message': 'APT maintenance requires root on Ubuntu 26.04.'}, 1)
    if args.values is not None or args.service_active is not None or args.service_enabled is not None:
        return json_response({'status': 'ERROR', 'code': 'INVALID_ARGUMENT',
                              'message': 'APT operations do not accept values or service targets.'}, 2)
    if args.apt_plan is None and not args.confirm:
        return json_response({'status': 'ERROR', 'code': 'CONFIRMATION_REQUIRED',
                              'message': 'APT mutations require --confirm.'}, 2)
    try:
        if manager is None:
            from .root_actions import RootManager
            manager = RootManager()
        plan = manager.apt_plan(operation)
        return json_response({'status': 'PLANNED', 'module': 'apt', 'plan': plan}
                             if args.apt_plan else manager.apt_run(plan))
    except Exception as error:
        return json_response({'status': 'FAILED', 'code': getattr(error, 'code', 'APT_FAILED'),
                              'module': 'apt', 'message': str(error)}, 1)


class PasswordInputError(ValueError):
    """Only fixed, non-secret validation messages may cross this boundary."""


def read_root_password(from_stdin=False):
    if from_stdin:
        if sys.stdin.isatty():
            raise PasswordInputError('--password-stdin requires redirected input; use the masked TTY prompt interactively.')
        stream = getattr(sys.stdin, 'buffer', sys.stdin)
        raw = stream.readline(4098)
        try:
            password = raw.decode('utf-8') if isinstance(raw, bytes) else raw
        except UnicodeError:
            raise PasswordInputError('Password input must be UTF-8.') from None
        if password.endswith('\n'):
            password = password[:-1]
            if password.endswith('\r'):
                password = password[:-1]
    else:
        if not sys.stdin.isatty():
            raise PasswordInputError('Password changes require a real TTY or explicit --password-stdin.')
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            password = getpass.getpass('New MariaDB root password: ')
            confirmation = getpass.getpass('Confirm new password: ')
        if password != confirmation:
            raise PasswordInputError('Password confirmation does not match.')
        confirmation = None
    if not password or len(password.encode('utf-8')) > 4096 or any(char in password for char in '\r\n\x00'):
        raise PasswordInputError('Password must be one nonempty line of at most 4096 UTF-8 bytes without NUL.')
    return password


def mariadb_security_command(args, manager=None):
    if not root_management_available():
        return json_response({'status': 'DENIED', 'code': 'ROOT_REQUIRED',
                              'message': 'MariaDB root access requires root on Ubuntu 26.04.'}, 1)
    if args.values is not None or args.service_active is not None or args.service_enabled is not None:
        return json_response({'status': 'ERROR', 'code': 'INVALID_ARGUMENT',
                              'message': 'Use the separate root recipe for values and service targets.'}, 2)
    if args.password_stdin and (args.mariadb_security_plan or not args.change_root_password):
        return json_response({'status': 'ERROR', 'code': 'INVALID_ARGUMENT',
                              'message': '--password-stdin requires --mariadb-security-apply --change-root-password.'}, 2)
    if args.mariadb_security_apply and not args.confirm:
        return json_response({'status': 'ERROR', 'code': 'CONFIRMATION_REQUIRED',
                              'message': 'MariaDB root access changes require --confirm.'}, 2)
    password = None
    try:
        if manager is None:
            from .root_actions import RootManager
            manager = RootManager()
        remote = None if args.remote in (None, 'keep') else args.remote == 'on'
        plan = manager.mariadb_security_plan(remote_enabled=remote, change_password=args.change_root_password)
        if args.mariadb_security_plan:
            return json_response({'status': 'PLANNED', 'module': 'mariadb-security', 'plan': plan})
        if args.change_root_password:
            password = read_root_password(args.password_stdin)
        return json_response(manager.mariadb_security_apply(plan, password=password))
    except PasswordInputError as error:
        return json_response({'status': 'ERROR', 'code': 'INVALID_PASSWORD_INPUT', 'message': str(error)}, 2)
    except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        return json_response({'status': 'CANCELLED', 'code': 'PASSWORD_INPUT_CANCELLED',
                              'message': 'Secure password input was cancelled or unavailable.'}, 2)
    except Exception as error:
        code = getattr(error, 'code', None)
        return json_response({'status': 'FAILED', 'code': code if code in MARIADB_SECURITY_ERROR_CODES else 'MARIADB_SECURITY_FAILED',
                              'message': 'MariaDB root access operation failed. Inspect its sanitized transaction status.'}, 1)
    finally:
        password = None


def read_firewall_config(source):
    """Treat recipe JSON as bounded data; never follow a file symlink or FIFO."""
    limit = 1024 * 1024
    if source == '-':
        stream = getattr(sys.stdin, 'buffer', sys.stdin)
        data = stream.read(limit + 1)
        data = data.encode('utf-8') if isinstance(data, str) else data
    else:
        path = Path(source)
        if path.is_symlink():
            raise ValueError('Firewall configuration must not be a symlink.')
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_size > limit or
                    (os.name == 'posix' and (info.st_uid != 0 or info.st_mode & 0o022))):
                raise ValueError('Firewall config must be a bounded root-owned regular file without group/world write access.')
            data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError('Firewall configuration exceeds 1 MiB.')
    from firewall_plan import unique_object
    return json.loads(data.decode('utf-8'), object_pairs_hook=unique_object)


def firewall_command(args, manager=None):
    operation = next(name for name in ('status', 'plan', 'save', 'apply', 'confirm', 'rollback')
                     if getattr(args, 'firewall_' + name))
    if not root_management_available():
        return json_response({'status': 'DENIED', 'code': 'ROOT_REQUIRED',
                              'message': 'Live firewall management requires root on Ubuntu 26.04.'}, 1)
    if operation not in {'status', 'plan'} and not args.confirm:
        return json_response({'status': 'ERROR', 'code': 'CONFIRMATION_REQUIRED',
                              'message': 'Firewall mutations require --confirm.'}, 2)
    if ((operation in {'plan', 'save', 'apply'}) != (args.config is not None) or
            args.values is not None or args.service_active is not None or args.service_enabled is not None):
        return json_response({'status': 'ERROR', 'code': 'INVALID_ARGUMENT',
                              'message': 'Only firewall plan/save/apply accept and require --config FILE (or - for bounded stdin).'}, 2)
    try:
        config = read_firewall_config(args.config) if args.config is not None else None
    except (OSError, ValueError, UnicodeError, TypeError):
        return json_response({'status': 'ERROR', 'code': 'INVALID_CONFIG',
                              'message': 'Invalid firewall config: use bounded UTF-8 schema2 JSON in a trusted regular file or explicit stdin.'}, 2)
    try:
        if manager is None:
            from .root_firewall import FirewallManager
            manager = FirewallManager()
        if operation == 'status':
            result = manager.status()
        elif operation == 'save':
            result = manager.save(config)
        elif operation in {'plan', 'apply'}:
            plan = manager.plan(config)
            result = {'status': 'PLANNED', 'module': 'firewall', 'plan': plan} if operation == 'plan' else manager.apply(plan)
        else:
            result = getattr(manager, operation)(getattr(args, 'firewall_' + operation))
        return json_response(result)
    except Exception as error:
        return json_response({'status': 'FAILED', 'code': getattr(error, 'code', 'FIREWALL_FAILED'),
                              'message': str(error)}, 1)


def lvm_command(args, manager=None):
    operation = next(name for name in ('status', 'plan', 'check', 'apply', 'recover') if getattr(args, 'lvm_' + name))
    if not root_management_available():
        return json_response({'status': 'DENIED', 'code': 'ROOT_REQUIRED', 'message': 'Root LV management requires root on Ubuntu 26.04.'}, 1)
    if operation in {'check', 'apply', 'recover'} and not args.confirm:
        return json_response({'status': 'ERROR', 'code': 'CONFIRMATION_REQUIRED', 'message': 'Explicit LVM test/growth/recovery requires --confirm.'}, 2)
    mode = args.grow_mode or 'all'
    if (args.values is not None or args.service_active is not None or args.service_enabled is not None or
            (operation in {'status', 'recover'} and (args.grow_mode is not None or args.add_gib is not None)) or
            (mode == 'all' and args.add_gib is not None) or
            (mode == 'add' and (args.add_gib is None or not 1 <= args.add_gib <= 65536))):
        return json_response({'status': 'ERROR', 'code': 'INVALID_ARGUMENT', 'message': 'Use all free space, or add with an integer --add-gib from 1 to 65536; status/recover do not accept growth targets.'}, 2)
    try:
        if manager is None:
            from .root_lvm import LVMManager
            manager = LVMManager()
        if operation == 'status':
            result = manager.status()
        elif operation == 'recover':
            result = manager.recover(args.lvm_recover)
        else:
            plan = manager.plan(mode=mode, add_gib=args.add_gib)
            result = {'status': 'PLANNED', 'module': 'lvm', 'plan': plan} if operation == 'plan' else getattr(manager, operation)(plan)
        return json_response(result)
    except Exception as error:
        return json_response({'status': 'FAILED', 'code': getattr(error, 'code', 'LVM_FAILED'), 'message': str(error)}, 1)


def swap_command(args, manager=None):
    operation = next(name for name in ('status', 'plan', 'save', 'apply', 'recover') if getattr(args, 'swap_' + name))
    if not root_management_available():
        return json_response({'status': 'DENIED', 'code': 'ROOT_REQUIRED', 'message': 'Swap management requires root on Ubuntu 26.04.'}, 1)
    if operation in {'save', 'apply', 'recover'} and not args.confirm:
        return json_response({'status': 'ERROR', 'code': 'CONFIRMATION_REQUIRED', 'message': 'Swap mutations require --confirm.'}, 2)
    targets = (args.swap_path, args.size_mib, args.swap_active, args.swap_persistent)
    if (args.values is not None or args.service_active is not None or args.service_enabled is not None or
            (operation in {'plan', 'save', 'apply'} and (any(value is None for value in targets) or not 16 <= args.size_mib <= 1048576)) or
            (operation == 'status' and any(value is not None for value in targets[1:])) or
            (operation == 'recover' and any(value is not None for value in targets))):
        return json_response({'status': 'ERROR', 'code': 'INVALID_ARGUMENT', 'message': 'Swap plan/save/apply require explicit path, size MiB (16..1048576), active and persistent targets. Status accepts only a path; recovery accepts only its transaction ID.'}, 2)
    try:
        if manager is None:
            from .root_swap import SwapManager
            manager = SwapManager()
        if operation == 'status':
            result = manager.status(args.swap_path)
        elif operation == 'recover':
            result = manager.recover(args.swap_recover)
        else:
            config = {'schema': 1, 'path': args.swap_path, 'size_mib': args.size_mib,
                      'active': args.swap_active == 'on', 'persistent': args.swap_persistent == 'on'}
            if operation == 'save':
                result = manager.save(config)
            else:
                plan = manager.plan(config)
                result = {'status': 'PLANNED', 'module': 'swap', 'plan': plan} if operation == 'plan' else manager.apply(plan)
        return json_response(result)
    except Exception as error:
        return json_response({'status': 'FAILED', 'code': getattr(error, 'code', 'SWAP_FAILED'), 'message': str(error)}, 1)


def banip_command(args, manager=None):
    operation = next(name for name in ('status', 'plan', 'update', 'confirm', 'rollback')
                     if getattr(args, 'banip_' + name))
    if not root_management_available():
        return json_response({'status': 'DENIED', 'code': 'ROOT_REQUIRED',
                              'message': 'BanIP management requires root on Ubuntu 26.04.'}, 1)
    if operation not in {'status', 'plan'} and not args.confirm:
        return json_response({'status': 'ERROR', 'code': 'CONFIRMATION_REQUIRED',
                              'message': 'BanIP mutations require --confirm.'}, 2)
    if args.values is not None or args.service_active is not None or args.service_enabled is not None:
        return json_response({'status': 'ERROR', 'code': 'INVALID_ARGUMENT',
                              'message': 'BanIP uses its fixed reviewed source and accepts no service targets.'}, 2)
    try:
        if manager is None:
            from .root_banip import BanIPManager
            manager = BanIPManager()
        if operation in {'plan', 'update'}:
            plan = manager.plan()
            result = {'status': 'PLANNED', 'module': 'banip', 'plan': plan} if operation == 'plan' else manager.update(plan)
        elif operation == 'status':
            result = manager.status()
        else:
            result = getattr(manager, operation)(getattr(args, 'banip_' + operation))
        return json_response(result)
    except Exception as error:
        return json_response({'status': 'FAILED', 'code': getattr(error, 'code', 'BANIP_FAILED'),
                              'message': str(error)}, 1)


def packages_command(args, manager=None):
    operation = ('catalog' if args.packages_catalog else 'search' if args.packages_search is not None else
                 'plan' if args.packages_plan is not None else 'install')
    if not root_management_available():
        return json_response({'status': 'DENIED', 'code': 'ROOT_REQUIRED',
                              'message': 'Package installation requires root on Ubuntu 26.04.'}, 1)
    if operation == 'install' and not args.confirm:
        return json_response({'status': 'ERROR', 'code': 'CONFIRMATION_REQUIRED',
                              'message': 'Package installation requires --confirm.'}, 2)
    if args.values is not None or args.service_active is not None or args.service_enabled is not None:
        return json_response({'status': 'ERROR', 'code': 'INVALID_ARGUMENT',
                              'message': 'Package operations do not accept recipe or service targets.'}, 2)
    names = getattr(args, 'packages_' + operation)
    if operation in {'plan', 'install'}:
        if not 1 <= len(names) <= 32 or any(not re.fullmatch(r'[a-z0-9][a-z0-9+.-]{1,127}', name) or name.endswith('.deb') for name in names):
            return json_response({'status': 'ERROR', 'code': 'INVALID_ARGUMENT',
                                  'message': 'Select 1..32 canonical package names; versions, paths and options are not accepted.'}, 2)
        names = sorted(set(names))
    if operation == 'search' and (not 1 <= len(names.strip()) <= 80 or len(names.split()) > 4 or
                                  not re.fullmatch(r'[A-Za-z0-9+. -]+', names)):
        return json_response({'status': 'ERROR', 'code': 'INVALID_ARGUMENT', 'message': 'Use up to four plain package search words, at most 80 characters.'}, 2)
    try:
        if manager is None:
            from .root_packages import PackageManager
            manager = PackageManager()
        if operation == 'catalog':
            result = {'status': 'OBSERVED', 'module': 'packages', 'catalog': manager.catalog()}
        elif operation == 'search':
            result = {'status': 'OBSERVED', 'module': 'packages', 'items': manager.search(names)}
        else:
            plan = manager.plan(names)
            result = {'status': 'PLANNED', 'module': 'packages', 'plan': plan} if operation == 'plan' else manager.install(plan)
        return json_response(result)
    except Exception as error:
        return json_response({'status': 'FAILED', 'code': getattr(error, 'code', 'PACKAGES_FAILED'),
                              'message': str(error)}, 1)


def apache_fpm_command(args, manager=None, root_manager=None):
    operation = next(name for name in ('status', 'plan', 'apply', 'restore')
                     if getattr(args, 'apache_fpm_' + name))
    if not root_management_available():
        return json_response({'status': 'DENIED', 'code': 'ROOT_REQUIRED', 'module': 'apache-fpm',
                              'message': 'Apache event/FPM management requires root on Ubuntu 26.04.'}, 1)
    if operation in {'apply', 'restore'} and not args.confirm:
        return json_response({'status': 'ERROR', 'code': 'CONFIRMATION_REQUIRED', 'module': 'apache-fpm',
                              'message': 'Apache event/FPM integration and restore require --confirm.'}, 2)
    try:
        if manager is None:
            from .root_apache import ApacheFPMManager
            manager = ApacheFPMManager(root_manager=root_manager)
        if operation == 'status':
            result = manager.status()
            return json_response(result, 0 if result.get('supported') is True else 1)
        if operation == 'plan':
            return json_response({'status': 'PLANNED', 'module': 'apache-fpm', 'plan': manager.plan()})
        if operation == 'restore':
            result = manager.restore(manager.restore_plan())
        else:
            result = manager.apply(manager.plan())
        expected = 'APACHE_FPM_RESTORED' if operation == 'restore' else 'APACHE_FPM_APPLIED'
        return json_response(result, 0 if result.get('status') in {expected, 'NO_CHANGE'} else 1)
    except Exception as error:
        from .root_actions import RootActionError
        code = getattr(error, 'code', 'APACHE_FPM_FAILED')
        if not isinstance(code, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', code):
            code = 'APACHE_FPM_FAILED'
        message = str(error) if isinstance(error, RootActionError) else 'Apache event/FPM operation failed; inspect its transaction and native service state.'
        return json_response({'status': 'FAILED', 'code': code, 'module': 'apache-fpm', 'message': message}, 1)


def php_feather_command(args, manager=None):
    operation = next(name for name in ('status', 'plan', 'apply', 'restore') if getattr(args, 'php_feather_' + name))
    if not root_management_available():
        return json_response({'status': 'DENIED', 'code': 'ROOT_REQUIRED',
                              'message': 'FeatherMountain SQLite management requires root on Ubuntu 26.04.'}, 1)
    if operation in {'apply', 'restore'} and not args.confirm:
        return json_response({'status': 'ERROR', 'code': 'CONFIRMATION_REQUIRED',
                              'message': 'Custom module build/activation and restore require --confirm.'}, 2)
    if (args.values is not None or args.service_active is not None or args.service_enabled is not None or
            (operation in {'status', 'restore'} and (args.feather_cli is not None or args.feather_fpm is not None))):
        return json_response({'status': 'ERROR', 'code': 'INVALID_ARGUMENT',
                              'message': 'CLI/FPM targets apply only to Feather plan/apply; recipe and service targets are separate.'}, 2)
    cli, fpm = args.feather_cli != 'off', args.feather_fpm != 'off'
    if operation in {'plan', 'apply'} and not (cli or fpm):
        return json_response({'status': 'ERROR', 'code': 'INVALID_ARGUMENT',
                              'message': 'Select at least one activation scope: CLI or FPM.'}, 2)
    try:
        if manager is None:
            from .php_feather import FeatherSQLiteManager
            manager = FeatherSQLiteManager()
        if operation == 'status':
            result = manager.status()
        elif operation == 'restore':
            result = manager.restore(manager.restore_plan())
        else:
            plan = manager.plan(cli=cli, fpm=fpm)
            result = {'status': 'PLANNED', 'module': 'php-feather', 'plan': plan} if operation == 'plan' else manager.apply(plan)
        return json_response(result)
    except Exception as error:
        return json_response({'status': 'FAILED', 'code': getattr(error, 'code', 'PHP_FEATHER_FAILED'),
                              'message': str(error)}, 1)


def startup_public(value):
    """Script text is explicit load/review data, never ordinary status/result data."""
    allowed = {'module', 'status', 'code', 'message', 'supported', 'detail', 'managed', 'enabled', 'wait_network',
               'timeout_seconds', 'script_sha256', 'unit', 'runtime', 'rc_local', 'pending', 'transaction_id',
               'verification', 'notes', 'recovery', 'warning', 'warnings', 'health_scope'}
    def clean(item):
        if isinstance(item, dict):
            return {key: clean(child) for key, child in item.items()
                    if key not in {'script', 'body', 'content', 'config', 'before', 'after', 'stdout', 'stderr'}}
        if isinstance(item, (list, tuple)):
            return [clean(child) for child in item]
        return item
    return {key: clean(item) for key, item in value.items() if key in allowed}


def startup_command(args, manager=None):
    operation = next(name for name in ('status', 'load', 'plan', 'save', 'apply', 'run', 'restore')
                     if getattr(args, 'startup_' + name))
    if not root_management_available():
        return json_response({'status': 'DENIED', 'code': 'ROOT_REQUIRED', 'module': 'startup',
                              'message': 'Startup management requires root on Ubuntu 26.04.'}, 1)
    if operation in {'save', 'apply', 'run', 'restore'} and not args.confirm:
        return json_response({'status': 'ERROR', 'code': 'CONFIRMATION_REQUIRED',
                              'message': 'Startup mutations and explicit execution require --confirm.'}, 2)
    if ((operation in {'plan', 'save', 'apply'}) != (args.startup_config is not None) or
            args.values is not None or args.service_active is not None or args.service_enabled is not None):
        return json_response({'status': 'ERROR', 'code': 'INVALID_ARGUMENT',
                              'message': 'Only startup plan/save/apply accept and require --startup-config FILE (or - for bounded stdin).'}, 2)
    try:
        config = read_firewall_config(args.startup_config) if args.startup_config is not None else None
    except (OSError, ValueError, UnicodeError, TypeError):
        return json_response({'status': 'ERROR', 'code': 'INVALID_CONFIG',
                              'message': 'Use bounded UTF-8 Startup JSON in a trusted regular file or explicit stdin.'}, 2)
    try:
        if manager is None:
            from .root_startup import StartupManager
            manager = StartupManager()
        if operation == 'status':
            result = startup_public(manager.status())
        elif operation == 'load':
            saved = manager.load_saved()
            return json_response({'status': 'LOADED' if saved else 'NO_SAVED_CONFIG', 'module': 'startup', **({'config': saved} if saved else {})})
        elif operation == 'save':
            result = manager.save(config)
        elif operation in {'plan', 'apply'}:
            plan = manager.plan(config)
            if operation == 'plan':
                selected = plan['config']
                summary = {'schema': plan['schema'], 'module': 'startup', 'operation': 'apply',
                           'config': {key: selected[key] for key in ('schema', 'enabled', 'wait_network', 'timeout_seconds')},
                           'script_sha256': hashlib.sha256(selected['script'].encode('utf-8')).hexdigest(),
                           'script_bytes': len(selected['script'].encode('utf-8')), 'fingerprint': plan['fingerprint'],
                           'notes': plan.get('notes', [])}
                return json_response({'status': 'PLANNED', 'module': 'startup', 'plan': summary})
            result = manager.apply(plan)
        elif operation == 'run':
            result = manager.run_once(manager.run_plan())
        else:
            result = manager.restore(manager.restore_plan())
        return json_response(startup_public(result))
    except Exception as error:
        return json_response({'status': 'FAILED', 'module': 'startup', 'code': getattr(error, 'code', 'STARTUP_FAILED'),
                              'message': 'Startup operation failed; inspect its sanitized status. Script output is not included.'}, 1)


def update_command(args, manager=None):
    operation = next(name for name in ('status', 'check', 'plan', 'apply', 'rollback') if getattr(args, 'update_'+name))
    if operation in {'apply', 'rollback'}:
        if not update_management_available():
            return json_response({'status':'DENIED', 'code':'ROOT_REQUIRED', 'module':'update',
                                  'message':'Program updates require root on Ubuntu 22.04, 24.04 or 26.04.'}, 1)
        if not args.confirm:
            return json_response({'status':'ERROR', 'code':'CONFIRMATION_REQUIRED', 'module':'update',
                                  'message':'Review the update plan, then use --confirm for a program switch.'}, 2)
    try:
        if manager is None:
            from .release_update import ReleaseManager
            manager = ReleaseManager()
        if operation == 'status':
            result = manager.status()
        elif operation == 'check':
            result = manager.check(args.release_tag)
        elif operation == 'plan':
            result = manager.plan(args.release_tag)
        elif operation == 'apply':
            result = manager.apply(manager.plan(args.release_tag))
        else:
            result = manager.rollback(manager.rollback_plan())
        return json_response(release_public(result))
    except Exception as error:
        code = getattr(error, 'code', 'UPDATE_FAILED')
        if not isinstance(code, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', code):
            code = 'UPDATE_FAILED'
        return json_response({'status':'FAILED', 'code':code, 'module':'update',
                              'message':'Release operation failed; the previous program is retained unless recovery is required. Inspect local update status.'}, 1)


def network_command(args, manager=None):
    operation = next(name for name in ('status', 'load', 'save', 'plan', 'apply', 'confirm', 'recover')
                     if (getattr(args, 'network_'+name) is not None if name in {'confirm', 'recover'} else getattr(args, 'network_'+name)))
    if operation != 'status' and not network_management_available():
        return json_response({'status':'DENIED', 'module':'network', 'code':'ROOT_REQUIRED',
                              'message':'Network management requires root on Ubuntu 22.04, 24.04 or 26.04.'}, 1)
    if operation in {'save', 'apply', 'confirm', 'recover'} and not args.confirm:
        return json_response({'status':'ERROR', 'module':'network', 'code':'CONFIRMATION_REQUIRED',
                              'message':'Network mutations require --confirm. Confirm the target only from a new SSH connection.'}, 2)
    if (operation in {'save', 'plan', 'apply'}) != (args.network_config is not None):
        raise ValueError('Only --network-save/plan/apply require and accept --network-config FILE (or - for stdin)')
    transaction_id = (args.network_confirm if operation == 'confirm' else args.network_recover) or None
    if ((operation == 'confirm' and transaction_id is None) or
            (transaction_id is not None and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', transaction_id))):
        raise ValueError('Use the transaction ID returned by the network operation')
    try:
        config = read_firewall_config(args.network_config) if args.network_config is not None else None
    except (OSError, ValueError, UnicodeError, TypeError):
        return json_response({'status':'ERROR', 'module':'network', 'code':'INVALID_CONFIG',
                              'message':'Use bounded UTF-8 network JSON in a trusted regular file or explicit stdin.'}, 2)
    try:
        if manager is None:
            from .root_network import NetworkManager
            manager = NetworkManager()
        if operation == 'status':
            result = manager.status()
        elif operation == 'load':
            config = manager.load_saved()
            result = {'status':'LOADED' if config else 'NO_SAVED_CONFIG', 'module':'network', **({'config':config} if config else {})}
        elif operation == 'save':
            result = manager.save(config)
        elif operation in {'plan', 'apply'}:
            plan = manager.plan(config)
            if operation == 'plan':
                return json_response({'status':'PLANNED', 'module':'network', 'plan':network_public(plan)})
            result = manager.apply(plan)
        elif operation == 'confirm':
            result = manager.confirm(transaction_id)
        else:
            result = manager.recover(transaction_id)
        return json_response(network_public(result))
    except Exception as error:
        code = getattr(error, 'code', 'NETWORK_FAILED')
        if not isinstance(code, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', code):
            code = 'NETWORK_FAILED'
        return json_response({'status':'FAILED', 'module':'network', 'code':code,
                              'message':'Network operation failed. Read network status for pending recovery instructions.'}, 1)


def main(argv=None, root_manager=None, firewall_manager=None, lvm_manager=None, swap_manager=None, banip_manager=None, package_manager=None, php_feather_manager=None, startup_manager=None, release_manager=None, network_manager=None, apache_fpm_manager=None):
    parser = CommandParser(description='3waServerSetting local console and explicit root operations')
    identity = version_info()
    parser.add_argument('--version', action='version', version=f'{identity["version"]} / commit {identity["commit"]} / {identity["channel"]}')
    parser.add_argument('--status-json', action='store_true', help='Read local status without Textual')
    parser.add_argument('--text', action='store_true', help='Plain ASCII status fallback, no interactive TUI')
    parser.add_argument('--ascii', action='store_true', help='ASCII labels/mascot (Textual may still draw Unicode borders)')
    parser.add_argument('--plain', action='store_true', help='Plain text fallback for terminals without color/Unicode')
    parser.add_argument('--smoke', action='store_true', help='Headless Textual startup/keyboard/resize check')
    root_modes = parser.add_mutually_exclusive_group()
    for operation in ('status', 'load', 'save', 'plan', 'apply'):
        root_modes.add_argument('--network-'+operation, action='store_true')
    root_modes.add_argument('--network-confirm', metavar='TRANSACTION_ID', help='Confirm only from a fresh SSH connection to the target IP')
    root_modes.add_argument('--network-recover', nargs='?', const='', metavar='TRANSACTION_ID', help='Recover the selected or unique pending network transaction')
    parser.add_argument('--network-config', metavar='FILE', help='Ethernet DHCP/static IPv4 and optional DNS JSON; - reads bounded stdin')
    for operation in ('status', 'check', 'plan', 'apply', 'rollback'):
        root_modes.add_argument('--update-' + operation, action='store_true')
    parser.add_argument('--release-tag', metavar='TAG', help='Select vYYYY.MM.DD.NNN (001..999; optional -rc.N) for an explicit release check/plan/apply')
    for operation in ('plan', 'apply', 'install', 'restore'):
        root_modes.add_argument('--root-' + operation, metavar='MODULE', choices=ROOT_MODULES)
    root_modes.add_argument('--apt-plan', choices=('update', 'upgrade'))
    root_modes.add_argument('--apt-update', action='store_true')
    root_modes.add_argument('--apt-upgrade', action='store_true')
    root_modes.add_argument('--mariadb-security-plan', action='store_true')
    root_modes.add_argument('--mariadb-security-apply', action='store_true')
    for operation in ('status', 'plan', 'save', 'apply'):
        root_modes.add_argument('--firewall-' + operation, action='store_true')
    for operation in ('confirm', 'rollback'):
        root_modes.add_argument('--firewall-' + operation, metavar='TRANSACTION_ID')
    for operation in ('status', 'plan', 'update'):
        root_modes.add_argument('--banip-' + operation, action='store_true')
    for operation in ('confirm', 'rollback'):
        root_modes.add_argument('--banip-' + operation, metavar='TRANSACTION_ID')
    root_modes.add_argument('--packages-catalog', action='store_true')
    root_modes.add_argument('--packages-search', metavar='QUERY')
    for operation in ('plan', 'install'):
        root_modes.add_argument('--packages-' + operation, nargs='+', metavar='PACKAGE')
    for operation in ('status', 'plan', 'apply', 'restore'):
        root_modes.add_argument('--php-feather-' + operation, action='store_true')
    for operation in ('status', 'plan', 'apply', 'restore'):
        root_modes.add_argument('--apache-fpm-' + operation, action='store_true')
    for operation in ('status', 'load', 'plan', 'save', 'apply', 'run', 'restore'):
        root_modes.add_argument('--startup-' + operation, action='store_true')
    parser.add_argument('--startup-config', metavar='FILE', help='Startup schema1 JSON; - reads at most 1 MiB from stdin')
    parser.add_argument('--feather-cli', choices=('on', 'off'), help='Select PHP CLI for custom activation; off preserves its current activation')
    parser.add_argument('--feather-fpm', choices=('on', 'off'), help='Select PHP FPM for custom activation; off preserves its current activation')
    parser.add_argument('--config', metavar='FILE', help='Firewall schema2 recipe; - reads at most 1 MiB from stdin')
    for operation in ('status', 'plan', 'check', 'apply'):
        root_modes.add_argument('--lvm-' + operation, action='store_true')
    root_modes.add_argument('--lvm-recover', metavar='TRANSACTION_ID')
    parser.add_argument('--grow-mode', choices=('all', 'add'), help='Detected root LV: all free VG extents, or a GiB increment')
    parser.add_argument('--add-gib', type=int, metavar='N', help='Positive integer GiB increment; requires --grow-mode add')
    for operation in ('status', 'plan', 'save', 'apply'):
        root_modes.add_argument('--swap-' + operation, action='store_true')
    root_modes.add_argument('--swap-recover', metavar='TRANSACTION_ID')
    parser.add_argument('--swap-path', choices=('/swapfile', '/swap.img'))
    parser.add_argument('--size-mib', type=int, metavar='N')
    parser.add_argument('--swap-active', choices=('on', 'off'), help='Swap use now; independent of boot persistence')
    parser.add_argument('--swap-persistent', choices=('on', 'off'), help='Swap entry at boot; independent of active state')
    parser.add_argument('--remote', choices=('keep', 'on', 'off'), help='MariaDB root@%% remote access target')
    parser.add_argument('--change-root-password', action='store_true', help='Request a separate, securely entered MariaDB root password')
    parser.add_argument('--password-stdin', action='store_true', help='Read one bounded UTF-8 password line from stdin; never argv/environment')
    parser.add_argument('--values', metavar='JSON', help='Complete managed field values for root plan/apply')
    active = parser.add_mutually_exclusive_group()
    active.add_argument('--start', dest='service_active', action='store_const', const=True)
    active.add_argument('--stop', dest='service_active', action='store_const', const=False)
    enabled = parser.add_mutually_exclusive_group()
    enabled.add_argument('--enable', dest='service_enabled', action='store_const', const=True)
    enabled.add_argument('--disable', dest='service_enabled', action='store_const', const=False)
    parser.add_argument('--confirm', action='store_true', help='Confirm one explicit root mutation')
    try:
        args = parser.parse_args(argv)
        is_root_command = any(getattr(args, 'root_' + name) is not None
                              for name in ('plan', 'apply', 'install', 'restore'))
        is_apt_command = args.apt_plan is not None or args.apt_update or args.apt_upgrade
        is_security_command = args.mariadb_security_plan or args.mariadb_security_apply
        is_firewall_command = any(getattr(args, 'firewall_' + operation)
                                  for operation in ('status', 'plan', 'save', 'apply', 'confirm', 'rollback'))
        is_lvm_command = any(getattr(args, 'lvm_' + operation) for operation in ('status', 'plan', 'check', 'apply', 'recover'))
        is_swap_command = any(getattr(args, 'swap_' + operation) for operation in ('status', 'plan', 'save', 'apply', 'recover'))
        is_banip_command = any(getattr(args, 'banip_' + operation) for operation in ('status', 'plan', 'update', 'confirm', 'rollback'))
        is_packages_command = args.packages_catalog or any(getattr(args, 'packages_' + operation) is not None
                                                          for operation in ('search', 'plan', 'install'))
        is_feather_command = any(getattr(args, 'php_feather_' + operation) for operation in ('status', 'plan', 'apply', 'restore'))
        is_apache_fpm_command = any(getattr(args, 'apache_fpm_' + operation) for operation in ('status', 'plan', 'apply', 'restore'))
        is_startup_command = any(getattr(args, 'startup_' + operation) for operation in ('status', 'load', 'plan', 'save', 'apply', 'run', 'restore'))
        is_update_command = any(getattr(args, 'update_' + operation) for operation in ('status', 'check', 'plan', 'apply', 'rollback'))
        is_network_command = any(getattr(args, 'network_'+operation) for operation in ('status', 'load', 'save', 'plan', 'apply')) or args.network_confirm is not None or args.network_recover is not None
        if not is_network_command and args.network_config is not None:
            raise ValueError('--network-config requires --network-save/plan/apply')
        if args.release_tag is not None:
            if not (args.update_check or args.update_plan or args.update_apply) or not valid_release_tag(args.release_tag):
                raise ValueError('--release-tag requires an explicit update check/plan/apply and a valid release tag')
        if not is_startup_command and args.startup_config is not None:
            raise ValueError('--startup-config requires --startup-plan/save/apply')
        if not is_feather_command and (args.feather_cli is not None or args.feather_fpm is not None):
            raise ValueError('Feather CLI/FPM scope flags require --php-feather-plan/apply')
        if not is_swap_command and any(value is not None for value in (args.swap_path, args.size_mib, args.swap_active, args.swap_persistent)):
            raise ValueError('Swap targets require an explicit --swap-status/plan/save/apply operation')
        if not is_lvm_command and (args.grow_mode is not None or args.add_gib is not None):
            raise ValueError('Growth targets require an explicit --lvm-plan/check/apply operation')
        if not is_firewall_command and args.config is not None:
            raise ValueError('--config requires a firewall plan/save/apply operation')
        if not is_security_command and (args.remote is not None or args.change_root_password or args.password_stdin):
            raise ValueError('MariaDB security options require --mariadb-security-plan/apply')
        if is_apache_fpm_command:
            if (args.status_json or args.text or args.plain or args.smoke or args.ascii
                    or args.values is not None or args.service_active is not None or args.service_enabled is not None
                    or (args.apache_fpm_status or args.apache_fpm_plan) and args.confirm):
                raise ValueError('Apache event/FPM operations accept only their explicit mode and --confirm for apply/restore')
            return apache_fpm_command(args, apache_fpm_manager, root_manager)
        if is_network_command:
            if (args.status_json or args.text or args.plain or args.smoke or args.values is not None
                    or args.service_active is not None or args.service_enabled is not None):
                raise ValueError('Network operations cannot be combined with text/status/smoke or service targets')
            return network_command(args, network_manager)
        if is_update_command:
            if (args.status_json or args.text or args.plain or args.smoke or args.values is not None
                    or args.service_active is not None or args.service_enabled is not None):
                raise ValueError('Program updates cannot be combined with status/text/smoke or service configuration options')
            return update_command(args, release_manager)
        if is_root_command or is_apt_command or is_security_command or is_firewall_command or is_lvm_command or is_swap_command or is_banip_command or is_packages_command or is_feather_command or is_startup_command:
            if args.status_json or args.text or args.plain or args.smoke:
                raise ValueError('Root operations cannot be combined with status/text/smoke modes')
            if is_security_command:
                return mariadb_security_command(args, root_manager)
            if is_firewall_command:
                return firewall_command(args, firewall_manager)
            if is_banip_command:
                return banip_command(args, banip_manager)
            if is_packages_command:
                return packages_command(args, package_manager)
            if is_feather_command:
                return php_feather_command(args, php_feather_manager)
            if is_startup_command:
                return startup_command(args, startup_manager)
            if is_lvm_command:
                return lvm_command(args, lvm_manager)
            if is_swap_command:
                return swap_command(args, swap_manager)
            return apt_command(args, root_manager) if is_apt_command else root_command(args, root_manager)
        if args.values is not None or args.service_active is not None or args.service_enabled is not None or args.confirm:
            raise ValueError('Root operation options require --root-plan/apply/install/restore')
    except ValueError as error:
        return json_response({'status': 'ERROR', 'code': 'INVALID_ARGUMENT', 'message': str(error)}, 2)
    if args.status_json or args.text or args.plain or (os.environ.get('TERM') == 'dumb' and not args.smoke):
        snapshot = collect()
        if args.status_json:
            print(json.dumps(snapshot, ensure_ascii=True, indent=2))
        else:
            print(f'3waServerSetting {VERSION} | READ ONLY / text fallback')
            print('No interactive form in text fallback. No changes applied.')
            for name, item in snapshot['modules'].items():
                print(f'{name}: [{item["status"]}] {item["detail"]}'.encode('ascii', 'backslashreplace').decode())
        return 0
    if importlib.util.find_spec('textual') is None:
        print('UNAVAILABLE: Textual dependency is missing. Nothing installed or changed.\n'
              'Use python -m server_setting --text for local status.\n'
              'See docs/tui-readonly.md for the reviewed dependency requirement.', file=sys.stderr)
        return 2
    from .app import SettingApp
    app = SettingApp(ascii_mode=args.ascii)
    if args.smoke:
        async def smoke():
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                await pilot.press('tab', 'ctrl+right')
                await pilot.resize_terminal(80, 24)
                await pilot.pause()
                assert not app.query_one('#mascot').display
                await pilot.press('ctrl+q')
            print('Textual headless startup / keyboard / resize completed. No system apply.')
        asyncio.run(smoke())
    else:
        app.run()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

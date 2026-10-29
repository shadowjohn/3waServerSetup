"""Apache event/FPM CLI requires one explicit operation and reviewed mutation."""
import contextlib
import copy
import io
import json
import unittest
from unittest.mock import patch

from server_setting.root_actions import RootActionError


class ApacheFPMFixture:
    def __init__(self):
        self.calls = []
        self.supported = True
        self.failure = None
        self.apply_status = 'APACHE_FPM_APPLIED'

    def status(self):
        self.calls.append(('status',))
        return {'module': 'apache-fpm', 'supported': self.supported,
                'detail': 'Native Apache MPM and matching PHP-FPM integration.'}

    def plan(self):
        self.calls.append(('plan',))
        return {'module': 'apache-fpm', 'fingerprint': 'reviewed-event-fpm',
                'mpm': 'event', 'php': '8.5'}

    def apply(self, plan):
        self.calls.append(('apply', copy.deepcopy(plan)))
        if self.failure:
            raise self.failure
        return {'module': 'apache-fpm', 'status': self.apply_status}

    def restore_plan(self):
        self.calls.append(('restore_plan',))
        return {'module': 'apache-fpm', 'fingerprint': 'reviewed-event-restore'}

    def restore(self, plan):
        self.calls.append(('restore', copy.deepcopy(plan)))
        return {'module': 'apache-fpm', 'status': 'APACHE_FPM_RESTORED'}


class ApacheFPMCliTests(unittest.TestCase):
    def invoke(self, arguments, manager=None, root=True):
        from server_setting.__main__ import main
        manager = manager if manager is not None else ApacheFPMFixture()
        stream = io.StringIO()
        with patch('server_setting.__main__.root_management_available', return_value=root), \
                contextlib.redirect_stdout(stream):
            code = main(arguments, apache_fpm_manager=manager)
        return code, json.loads(stream.getvalue())

    def test_every_operation_checks_root_before_calling_backend(self):
        for operation in ('status', 'plan', 'apply', 'restore'):
            manager = ApacheFPMFixture()
            arguments = ['--apache-fpm-' + operation] + (['--confirm'] if operation in {'apply', 'restore'} else [])
            code, result = self.invoke(arguments, manager, root=False)
            self.assertEqual((code, result['code']), (1, 'ROOT_REQUIRED'))
            self.assertEqual(manager.calls, [])

    def test_apply_and_restore_require_confirmation_before_even_planning(self):
        for operation in ('apply', 'restore'):
            manager = ApacheFPMFixture()
            code, result = self.invoke(['--apache-fpm-' + operation], manager)
            self.assertEqual((code, result['code']), (2, 'CONFIRMATION_REQUIRED'))
            self.assertEqual(manager.calls, [])

    def test_status_and_plan_never_apply(self):
        manager = ApacheFPMFixture()
        code, result = self.invoke(['--apache-fpm-status'], manager)
        self.assertEqual((code, result['supported']), (0, True))
        code, result = self.invoke(['--apache-fpm-plan'], manager)
        self.assertEqual((code, result['status'], result['plan']['fingerprint']),
                         (0, 'PLANNED', 'reviewed-event-fpm'))
        self.assertEqual(manager.calls, [('status',), ('plan',)])

    def test_apply_and_restore_pass_the_generated_preview_unchanged(self):
        manager = ApacheFPMFixture()
        code, result = self.invoke(['--apache-fpm-apply', '--confirm'], manager)
        self.assertEqual((code, result['status']), (0, 'APACHE_FPM_APPLIED'))
        self.assertEqual(manager.calls[-1], ('apply', {'module': 'apache-fpm',
            'fingerprint': 'reviewed-event-fpm', 'mpm': 'event', 'php': '8.5'}))
        code, result = self.invoke(['--apache-fpm-restore', '--confirm'], manager)
        self.assertEqual((code, result['status']), (0, 'APACHE_FPM_RESTORED'))
        self.assertEqual(manager.calls[-1], ('restore', {'module': 'apache-fpm',
            'fingerprint': 'reviewed-event-restore'}))
        self.assertEqual([call[0] for call in manager.calls], ['plan', 'apply', 'restore_plan', 'restore'])

    def test_other_modes_and_unrelated_options_cannot_reach_backend(self):
        extras = [
            ['--root-plan', 'php'], ['--apt-plan', 'upgrade'], ['--firewall-status'],
            ['--network-status'], ['--update-status'], ['--packages-catalog'], ['--php-feather-status'],
            ['--apache-fpm-apply', '--confirm'], ['--text'], ['--status-json'], ['--plain'],
            ['--smoke'], ['--ascii'], ['--values', '{}'], ['--start'], ['--stop'],
            ['--enable'], ['--disable'], ['--config', '-'], ['--network-config', '-'],
            ['--startup-config', '-'], ['--feather-cli', 'on'], ['--feather-fpm', 'off'],
            ['--remote', 'on'], ['--change-root-password'], ['--password-stdin'],
            ['--grow-mode', 'all'], ['--add-gib', '1'], ['--swap-path', '/swap.img'],
            ['--size-mib', '16384'], ['--swap-active', 'on'], ['--swap-persistent', 'on'],
            ['--release-tag', 'v2026.10.27.001'], ['--confirm'],
        ]
        for extra in extras:
            with self.subTest(extra=extra):
                manager = ApacheFPMFixture()
                code, result = self.invoke(['--apache-fpm-status', *extra], manager)
                self.assertEqual((code, result['code']), (2, 'INVALID_ARGUMENT'))
                self.assertEqual(manager.calls, [])

    def test_native_rollback_failure_is_nonzero_with_its_sanitized_code(self):
        manager = ApacheFPMFixture()
        manager.failure = RootActionError('Previous Apache/FPM state was restored.', 'APACHE_FPM_ROLLED_BACK')
        code, result = self.invoke(['--apache-fpm-apply', '--confirm'], manager)
        self.assertEqual((code, result['code']), (1, 'APACHE_FPM_ROLLED_BACK'))
        self.assertEqual(result['status'], 'FAILED')

    def test_unsupported_status_and_returned_failed_mutation_are_nonzero(self):
        manager = ApacheFPMFixture()
        manager.supported = False
        self.assertEqual(self.invoke(['--apache-fpm-status'], manager)[0], 1)
        manager.apply_status = 'APACHE_FPM_RECOVERY_REQUIRED'
        self.assertEqual(self.invoke(['--apache-fpm-apply', '--confirm'], manager)[0], 1)

    def test_unexpected_exception_never_exposes_native_diagnostics(self):
        manager = ApacheFPMFixture()
        manager.failure = RuntimeError('https://user:secret@private.invalid/native-diagnostic')
        code, result = self.invoke(['--apache-fpm-apply', '--confirm'], manager)
        self.assertEqual((code, result['code']), (1, 'APACHE_FPM_FAILED'))
        self.assertNotIn('private.invalid', str(result))
        self.assertNotIn('secret', str(result))


if __name__ == '__main__':
    unittest.main()

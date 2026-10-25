"""Reviewed growth of the detected, ordinary ext4 root logical volume."""
import json
import os
from pathlib import Path
import re
import stat
import subprocess

from .root_actions import Host, RootActionError, RootManager, _fingerprint
from .trusted_commands import environment, resolve

GIB = 1024**3
_NAMES = re.compile(r'[A-Za-z0-9_+][A-Za-z0-9_.+-]{0,126}')
_IDENTIFIER = re.compile(r'[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}')


class LVMHost(Host):
    def run(self, argv, timeout=30):
        names = {'findmnt', 'vgs', 'lvs', 'pvs', 'dumpe2fs', 'lvextend', 'resize2fs'}
        if (not isinstance(argv, (list, tuple)) or not argv or argv[0] not in names
                or any(not isinstance(arg, str) or '\x00' in arg for arg in argv)
                or not isinstance(timeout, (int, float)) or not 0 < timeout <= 900):
            raise RootActionError('Invalid bounded LVM command.', 'UNSAFE_COMMAND')
        if self.root != Path('/'):
            raise RootActionError('Prefixed LVM hosts require an explicit native test boundary.')
        executable = resolve(argv[0])
        if not executable:
            raise RootActionError('Required trusted LVM command unavailable: ' + argv[0], 'MISSING_COMMAND')
        try:
            return subprocess.run([executable, *argv[1:]], stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, errors='replace', timeout=timeout,
                                  env=environment(), shell=False, check=False)
        except subprocess.TimeoutExpired:
            raise RootActionError('LVM command timed out; inspect actual LV and filesystem state.', 'TIMEOUT') from None
        except OSError:
            raise RootActionError('LVM command could not execute.', 'COMMAND_FAILED') from None

    def block_identity(self, path):
        if self.root != Path('/') or not path.startswith('/dev/'):
            raise RootActionError('A native block device is required.', 'LVM_UNSUPPORTED')
        try:
            info = os.stat(path)
            if not stat.S_ISBLK(info.st_mode):
                raise OSError()
            return os.major(info.st_rdev), os.minor(info.st_rdev)
        except OSError:
            raise RootActionError('Detected LVM path is not a block device.', 'LVM_UNSUPPORTED') from None

    def plain_pv(self, path):
        major, minor = self.block_identity(path)
        location = Path(f'/sys/dev/block/{major}:{minor}').resolve(strict=True)
        # Encrypted/stacked device-mapper and MD layouts need a separate adapter.
        return not (location/'dm').exists() and not (location/'md').exists()


class LVMManager:
    def __init__(self, host=None):
        self.host = host or LVMHost()
        self.manager = RootManager(self.host)

    def _gate(self):
        if getattr(os, 'geteuid', lambda: -1)() != 0:
            raise RootActionError('Root privileges are required.', 'ROOT_REQUIRED')
        if not self.manager._platform():
            raise RootActionError('Root LV changes require Ubuntu 26.04.', 'UNSUPPORTED_OS')

    def _checked(self, argv, timeout=30):
        result = self.host.run(argv, timeout)
        if result.returncode or len(result.stdout) > 512*1024:
            raise RootActionError('Native LVM inspection or validation failed.', 'LVM_NOT_READY')
        return result.stdout

    def _report(self, command, section, fields):
        output = self._checked([command, '--reportformat', 'json', '--units', 'b', '--nosuffix',
                                *(['--segments'] if command == 'lvs' else []), '-o', fields])
        try:
            report = json.loads(output)['report']
            rows = [row for item in report for row in item[section]]
            if any(not isinstance(row, dict) for row in rows) or len(rows) > 512:
                raise ValueError()
            return rows
        except (ValueError, KeyError, TypeError):
            raise RootActionError('Invalid native LVM report.', 'LVM_NOT_READY') from None

    @staticmethod
    def _integer(value):
        if not isinstance(value, str) or not re.fullmatch(r'[0-9]+(?:\.0*)?', value.strip()):
            raise RootActionError('Native storage geometry is not integral.', 'LVM_NOT_READY')
        return int(value.strip().split('.')[0])

    def _inspect(self):
        self._gate()
        try:
            entries = json.loads(self._checked(['findmnt', '-J', '-T', '/', '-o',
                                                'TARGET,SOURCE,FSTYPE,OPTIONS,MAJ:MIN']))['filesystems']
            if len(entries) != 1:
                raise ValueError()
            mount = entries[0]
            device = tuple(int(part) for part in mount['maj:min'].split(':'))
            if (mount['target'] != '/' or mount['fstype'] != 'ext4'
                    or 'rw' not in mount['options'].split(',') or len(device) != 2):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise RootActionError('Only a writable local ext4 root filesystem is supported.', 'LVM_UNSUPPORTED') from None
        rows = self._report('lvs', 'seg', 'lv_path,lv_name,vg_name,lv_size,lv_attr,segtype,lv_role,origin,pool_lv,lv_uuid,lv_kernel_major,lv_kernel_minor')
        try:
            # Inactive unrelated LVs legitimately report kernel major/minor -1.
            selected = [row for row in rows if tuple(int(row[key].strip()) for key in
                         ('lv_kernel_major','lv_kernel_minor')) == device]
        except (KeyError, TypeError, ValueError):
            raise RootActionError('Invalid native LV block identity.', 'LVM_NOT_READY') from None
        if not selected:
            raise RootActionError('Root filesystem is not an active ordinary LVM volume.', 'LVM_UNSUPPORTED')
        lv = selected[0]
        same = ('lv_path','lv_name','vg_name','lv_size','lv_attr','lv_role','origin','pool_lv','lv_uuid')
        if any(any(row[key] != lv[key] for key in same) or row['segtype'] != 'linear' for row in selected):
            raise RootActionError('Mixed, striped or non-linear root LV layouts are unsupported.', 'LVM_UNSUPPORTED')
        if (not _NAMES.fullmatch(lv['vg_name']) or not _NAMES.fullmatch(lv['lv_name'])
                or lv['lv_path'] != f"/dev/{lv['vg_name']}/{lv['lv_name']}"
                or not lv['lv_attr'].startswith('-w') or len(lv['lv_attr']) < 6
                or lv['lv_attr'][4] != 'a' or lv['origin'] or lv['pool_lv']
                or lv['lv_role'] != 'public' or not lv['lv_uuid']
                or self.host.block_identity(lv['lv_path']) != device):
            raise RootActionError('Unsupported or inconsistent root LV identity.', 'LVM_UNSUPPORTED')
        groups = [row for row in self._report('vgs','vg','vg_name,vg_uuid,vg_size,vg_free,vg_extent_size,vg_free_count')
                  if row['vg_name'] == lv['vg_name']]
        if len(groups) != 1:
            raise RootActionError('Root volume group is ambiguous.', 'LVM_UNSUPPORTED')
        vg = groups[0]
        pvs = [row for row in self._report('pvs','pv','pv_name,vg_name') if row['vg_name'] == lv['vg_name']]
        if not pvs or any(not self.host.plain_pv(row['pv_name']) for row in pvs):
            raise RootActionError('Stacked or unknown physical volume layout is unsupported.', 'LVM_UNSUPPORTED')
        extent = self._integer(vg['vg_extent_size'])
        size, free = self._integer(lv['lv_size']), self._integer(vg['vg_free_count'])
        if not extent or size % extent or free*extent != self._integer(vg['vg_free']):
            raise RootActionError('Inconsistent native VG geometry.', 'LVM_NOT_READY')
        geometry = self._checked(['dumpe2fs','-h',lv['lv_path']])
        found = {}
        for line in geometry.splitlines():
            key, separator, value = line.partition(':')
            if separator and key in {'Block count','Block size'}:
                found[key] = self._integer(value)
        if set(found) != {'Block count','Block size'} or not found['Block size'] or not found['Block count']:
            raise RootActionError('Cannot verify the ext4 filesystem geometry.', 'LVM_NOT_READY')
        fs_size = found['Block count']*found['Block size']
        if fs_size > size:
            raise RootActionError('Filesystem exceeds the detected LV size.', 'LVM_NOT_READY')
        return {'supported':True, 'lv_path':lv['lv_path'], 'lv_uuid':lv['lv_uuid'],
                'vg_name':lv['vg_name'], 'vg_uuid':vg['vg_uuid'], 'lv_bytes':size,
                'vg_free_bytes':free*extent, 'vg_free_extents':free, 'extent_bytes':extent,
                'filesystem':'ext4', 'filesystem_bytes':fs_size, 'block_bytes':found['Block size'],
                'device':list(device), 'mountpoint':'/', 'needs_filesystem_growth':fs_size < size,
                'physical_volumes':sorted(row['pv_name'] for row in pvs)}

    def status(self):
        try:
            return self._inspect()
        except RootActionError as error:
            return {'supported':False, 'detail':str(error), 'code':error.code}

    def plan(self, mode='all', add_gib=None):
        if mode not in {'all','add'} or (mode == 'all' and add_gib is not None):
            raise RootActionError('Choose all free VG space or a positive GiB increment.', 'INVALID_VALUES')
        if mode == 'add' and (type(add_gib) is not int or not 1 <= add_gib <= 65536):
            raise RootActionError('Additional GiB must be an integer from 1 to 65536.', 'INVALID_VALUES')
        state = self._inspect()
        if state['needs_filesystem_growth']:
            raise RootActionError('The LV is larger than its filesystem; complete filesystem recovery first.', 'LVM_FS_RESIZE_REQUIRED')
        extents = state['vg_free_extents'] if mode == 'all' else (add_gib*GIB+state['extent_bytes']-1)//state['extent_bytes']
        if extents > state['vg_free_extents']:
            raise RootActionError('The volume group has insufficient free extents.', 'LVM_NO_SPACE')
        target = state['lv_bytes']+extents*state['extent_bytes']
        command = ['lvextend','--yes','--resizefs','--extents',str(target//state['extent_bytes']),state['lv_path']]
        plan = {'schema':1,'module':'lvm','mode':mode,'add_gib':add_gib,'before':state,
                'target_bytes':target,'grow_bytes':target-state['lv_bytes'],'command':command,
                'notes':['Grows only the detected root LV and ext4 filesystem. This operation cannot be undone by configuration restore.',
                         'The command uses the reviewed absolute extent count so a later VG change cannot consume extra unreviewed space.',
                         'No partition, physical-volume or virtual-disk expansion is performed.']}
        plan['fingerprint'] = _fingerprint(plan)
        return plan

    def _recheck(self, plan):
        if not isinstance(plan,dict) or plan.get('module') != 'lvm' or plan.get('fingerprint') != _fingerprint(plan):
            raise RootActionError('LVM preview was modified.', 'TAMPERED_PLAN')
        trusted = self.plan(plan.get('mode'),plan.get('add_gib'))
        if trusted['fingerprint'] != plan['fingerprint']:
            raise RootActionError('LV, filesystem or VG geometry changed after preview.', 'STALE_PLAN')
        return trusted

    def check(self, plan):
        self._gate()
        with self.manager._lock():
            self.manager._check_pending()
            trusted = self._recheck(plan)
            if trusted['grow_bytes']:
                command = [arg for arg in trusted['command'] if arg != '--resizefs']
                self._checked([command[0],'--test',*command[1:]],90)
            return {'status':'LVM_TEST_PASSED','module':'lvm','message':'LVM test mode passed without filesystem resizing; no growth was performed.',
                    'target_bytes':trusted['target_bytes']}

    def apply(self, plan):
        self._gate()
        with self.manager._lock():
            self.manager._check_pending()
            trusted = self._recheck(plan)
            if not trusted['grow_bytes']:
                return {'status':'NO_CHANGE','module':'lvm','message':'No free VG extents remain; no growth was performed.'}
            self.manager._preflight_state()
            identifier = self.manager._id()
            journal = {'schema':1,'module':'lvm','kind':'root-lv-growth','transaction_id':identifier,
                       'status':'LVM_GROWING','plan':trusted}
            self.manager._journal(journal)
            try:
                result = self.host.run(trusted['command'],900)
                return self._finish(journal, result.returncode)
            except RootActionError as error:
                if error.code in {'LVM_FAILED','LVM_FS_RESIZE_REQUIRED'}:
                    raise
                journal['status'] = 'LVM_RECOVERY_REQUIRED'
                self.manager._journal(journal)
                raise RootActionError(f'Inspect LVM transaction {identifier}; growth may have partially completed.', 'LVM_RECOVERY_REQUIRED') from None

    def _finish(self, journal, returncode=0):
        state = self._inspect()
        plan = journal['plan']
        identity = ('lv_path','lv_uuid','vg_name','vg_uuid','device','filesystem','physical_volumes')
        if any(state[key] != plan['before'][key] for key in identity):
            raise RootActionError('Root LV identity changed; inspect the transaction.', 'LVM_RECOVERY_REQUIRED')
        if state['lv_bytes'] == plan['target_bytes'] and state['filesystem_bytes'] == state['lv_bytes']:
            journal['status'] = 'LVM_APPLIED'
            self.manager._journal(journal)
            return self.manager._log_result({'status':'LVM_APPLIED','module':'lvm','transaction_id':journal['transaction_id'],
                'message':'Root LV and ext4 filesystem grew and their geometry was verified.',
                'command_warning':bool(returncode),'before_bytes':plan['before']['lv_bytes'],
                'after_bytes':state['lv_bytes'],'vg_free_bytes':state['vg_free_bytes']})
        if state['lv_bytes'] == plan['target_bytes'] and state['filesystem_bytes'] < state['lv_bytes']:
            journal['status'] = 'LVM_FS_RESIZE_REQUIRED'
            self.manager._journal(journal)
            raise RootActionError('LV growth completed but filesystem growth requires recovery. Do not repeat the increment. Transaction: '+journal['transaction_id'], 'LVM_FS_RESIZE_REQUIRED')
        if state['lv_bytes'] == plan['before']['lv_bytes'] and state['filesystem_bytes'] == plan['before']['filesystem_bytes']:
            journal['status'] = 'LVM_FAILED'
            self.manager._journal(journal)
            raise RootActionError('Native growth failed; the LV and filesystem did not grow.', 'LVM_FAILED')
        journal['status'] = 'LVM_RECOVERY_REQUIRED'
        self.manager._journal(journal)
        raise RootActionError('Unexpected storage geometry; inspect native state before continuing.', 'LVM_RECOVERY_REQUIRED')

    def recover(self, identifier):
        self._gate()
        if not isinstance(identifier,str) or not _IDENTIFIER.fullmatch(identifier):
            raise RootActionError('Invalid LVM transaction identifier.', 'INVALID_VALUES')
        with self.manager._lock():
            journals = self.manager._journals()
            selected = [journal for journal in journals if journal['transaction_id'] == identifier and journal.get('module') == 'lvm']
            if len(selected) != 1 or selected[0].get('status') not in {'LVM_GROWING','LVM_FS_RESIZE_REQUIRED','LVM_RECOVERY_REQUIRED','LVM_APPLIED'}:
                raise RootActionError('No recoverable LVM transaction was selected.', 'LVM_RECOVERY_REQUIRED')
            journal = selected[0]
            self.manager._check_pending(ignore_id=identifier)
            state, plan = self._inspect(), journal['plan']
            identity = ('lv_path','lv_uuid','vg_name','vg_uuid','device','filesystem','physical_volumes')
            if any(state[key] != plan['before'][key] for key in identity):
                raise RootActionError('The LV no longer matches the recorded growth identity.', 'LVM_RECOVERY_REQUIRED')
            if (journal['status'] in {'LVM_GROWING','LVM_RECOVERY_REQUIRED'}
                    and state['lv_bytes'] == plan['before']['lv_bytes']
                    and state['filesystem_bytes'] == plan['before']['filesystem_bytes']):
                return self._finish(journal, 1)
            if state['lv_bytes'] != plan['target_bytes']:
                raise RootActionError('The LV no longer matches the recorded growth target.', 'LVM_RECOVERY_REQUIRED')
            if state['filesystem_bytes'] < state['lv_bytes']:
                result = self.host.run(['resize2fs',state['lv_path']],900)
                return self._finish(journal,result.returncode)
            return self._finish(journal)

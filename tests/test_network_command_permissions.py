"""Netplan's generated backend files need their requested group-read mode."""
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from server_setting import network_watchdog as guard


class NetworkCommandPermissionsTests(unittest.TestCase):
    def adapter(self, command, metadata=None, output=''):
        executable='/usr/sbin/'+command
        path=Mock()
        path.resolve.return_value=path
        path.parents=[]
        path.stat.return_value=metadata or SimpleNamespace(st_uid=0,st_mode=stat.S_IFREG|0o755)
        result=subprocess.CompletedProcess([executable],0,output,'')
        return executable,path,result

    def test_netplan_only_uses_022_at_the_real_subprocess_boundary(self):
        executable,path,result=self.adapter('netplan')
        with patch.object(guard.shutil,'which',return_value=executable) as which, \
                patch.object(guard,'Path',return_value=path), \
                patch.object(guard.subprocess,'run',return_value=result) as run:
            self.assertIs(guard.native_run(['netplan','generate'],timeout=17),result)
        which.assert_called_once_with('netplan',path=guard.SAFE_PATH)
        args,options=run.call_args
        self.assertEqual(args,([executable,'generate'],))
        self.assertEqual(options['umask'],0o022)
        self.assertEqual(options['timeout'],17)
        self.assertIs(options['shell'],False)
        self.assertEqual(options['stdin'],subprocess.DEVNULL)
        self.assertEqual(options['env'],{'PATH':guard.SAFE_PATH,'HOME':'/root','LANG':'C','LC_ALL':'C','SYSTEMD_PAGER':'cat'})
        self.assertEqual((options['capture_output'],options['text'],options['encoding'],options['errors']),
                         (True,True,'utf-8','strict'))

    def test_other_allowed_children_keep_private_077(self):
        for command in ('ip','resolvectl','systemctl','python3','findmnt','systemd-run','networkctl'):
            executable,path,result=self.adapter(command)
            with self.subTest(command=command),patch.object(guard.shutil,'which',return_value=executable), \
                    patch.object(guard,'Path',return_value=path), \
                    patch.object(guard.subprocess,'run',return_value=result) as run:
                guard.native_run([command,'--version'])
                self.assertEqual(run.call_args.kwargs['umask'],0o077)

    def test_command_trust_allowlist_and_output_bound_still_apply(self):
        for argv in (['sh','-c','true'],['/usr/sbin/netplan','generate'],['netplan','\0']):
            with self.subTest(argv=argv),self.assertRaises(guard.NetworkError):guard.native_run(argv)
        executable,path,result=self.adapter('netplan',SimpleNamespace(st_uid=1000,st_mode=stat.S_IFREG|0o755))
        with patch.object(guard.shutil,'which',return_value=executable),patch.object(guard,'Path',return_value=path), \
                patch.object(guard.subprocess,'run') as run,self.assertRaises(guard.NetworkError):
            guard.native_run(['netplan','generate'])
        run.assert_not_called()
        executable,path,result=self.adapter('netplan',output='x'*(4*guard.LIMIT+1))
        with patch.object(guard.shutil,'which',return_value=executable),patch.object(guard,'Path',return_value=path), \
                patch.object(guard.subprocess,'run',return_value=result),self.assertRaises(guard.NetworkError):
            guard.native_run(['netplan','generate'])

    @unittest.skipUnless(os.name=='posix','requires real POSIX child umask and permission bits')
    def test_real_netplan_child_preserves_requested_0640_under_private_parent(self):
        executable=Path(sys.executable).resolve(strict=True)
        if any(path.stat().st_uid!=0 or path.stat().st_mode&0o022 for path in (executable,*executable.parents)):
            self.skipTest('The real interpreter is not root controlled')
        create="import os,sys;fd=os.open(sys.argv[1],os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o640);os.close(fd)"
        with tempfile.TemporaryDirectory() as directory:
            original=os.umask(0o077)
            try:
                with patch.object(guard.shutil,'which',return_value=str(executable)):
                    for command,mode in (('netplan',0o640),('python3',0o600)):
                        target=Path(directory)/command
                        result=guard.native_run([command,'-I','-B','-c',create,str(target)])
                        self.assertEqual(result.returncode,0,result.stderr)
                        self.assertEqual(stat.S_IMODE(target.stat().st_mode),mode)
                self.assertEqual(os.umask(0o077),0o077,'The parent process umask must remain private')
            finally:os.umask(original)


if __name__=='__main__':unittest.main()

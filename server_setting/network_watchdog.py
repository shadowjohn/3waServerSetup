"""Frozen stdlib worker. Volatile Netplan trials never persist before new SSH."""
from contextlib import contextmanager
import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time

try:
    import fcntl
except ImportError:
    fcntl = None

STATE='/var/lib/3wa-server-setup/network'
TRANSACTIONS='/var/lib/3wa-server-setup/transactions'
BOOT_UNIT='threewa-network-reconcile.service'
ID=re.compile(r'[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}')
SAFE_PATH='/usr/sbin:/usr/bin:/sbin:/bin'
FINAL={'APPLIED','ROLLED_BACK'}
LIMIT=1024*1024


class NetworkError(RuntimeError):
    pass


def digest(raw):return hashlib.sha256(raw).hexdigest()
def encoded(value):return (json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True)+'\n').encode()
def now():return time.clock_gettime(time.CLOCK_BOOTTIME) if hasattr(time,'CLOCK_BOOTTIME') else time.monotonic()
def ticks():return int(now()*(os.sysconf('SC_CLK_TCK') if hasattr(os,'sysconf') else 100))


def native_run(argv,timeout=30):
    if (not isinstance(argv,(list,tuple)) or not argv or argv[0] not in {'netplan','ip','resolvectl','systemctl','python3','findmnt','systemd-run'}
            or any(not isinstance(v,str) or '\x00' in v for v in argv) or not 0<timeout<=180):
        raise NetworkError('Invalid bounded network command.')
    executable=shutil.which(argv[0],path=SAFE_PATH)
    if not executable:raise NetworkError('A required network command is missing.')
    for path in (Path(executable).resolve(strict=True),*Path(executable).resolve(strict=True).parents):
        metadata=path.stat()
        if metadata.st_uid!=0 or metadata.st_mode&0o022:raise NetworkError('Network executable is not root controlled.')
    try:
        result=subprocess.run([executable,*argv[1:]],stdin=subprocess.DEVNULL,capture_output=True,text=True,
            encoding='utf-8',errors='strict',timeout=timeout,shell=False,umask=0o077,
            env={'PATH':SAFE_PATH,'HOME':'/root','LANG':'C','LC_ALL':'C','SYSTEMD_PAGER':'cat'})
    except (OSError,subprocess.TimeoutExpired):raise NetworkError('Network command failed or timed out.') from None
    if len(result.stdout)+len(result.stderr)>4*LIMIT:raise NetworkError('Network output exceeds its bound.')
    return result


def connection_proof(pid=None):
    """Prove the SSH transport from ancestor socket FDs; environment is irrelevant."""
    caller = int(pid or os.getpid())
    if caller <= 1:
        raise NetworkError('A real SSH caller is required.')
    def process(number):
        directory = Path('/proc') / str(number)
        text = (directory / 'stat').read_text()
        fields = text[text.rfind(')') + 2:].split()
        return int(fields[1]), int(fields[19]), directory
    _, caller_start, _ = process(caller)
    current, candidates, visited = caller, [], set()
    binaries = {'/usr/sbin/sshd', '/usr/lib/openssh/sshd-session', '/usr/lib/openssh/sshd-auth'}
    while current > 1 and current not in visited and len(visited) < 64:
        visited.add(current)
        parent, started, directory = process(current)
        try:
            executable = os.readlink(directory / 'exe')
        except OSError:
            executable = ''
        if executable in binaries:
            executable_path = Path(executable).resolve(strict=True)
            for part in [executable_path, *executable_path.parents]:
                info = part.stat()
                if info.st_uid != 0 or info.st_mode & 0o022:
                    raise NetworkError('SSH transport executable is not root controlled.')
            inodes = set()
            for descriptor in (directory / 'fd').iterdir():
                try:
                    target = os.readlink(descriptor)
                except OSError:
                    continue
                match = re.fullmatch(r'socket:\[([0-9]+)\]', target)
                if match:
                    inodes.add(match[1])
            for filename, family in [('tcp', 4), ('tcp6', 6)]:
                for line in (directory / 'net' / filename).read_text().splitlines()[1:]:
                    fields = line.split()
                    if len(fields) < 10 or fields[3] != '01' or fields[9] not in inodes:
                        continue
                    def endpoint(value):
                        address, port = value.split(':')
                        raw = bytes.fromhex(address)
                        raw = raw[::-1] if family == 4 else b''.join(raw[i:i + 4][::-1] for i in range(0, 16, 4))
                        address = ipaddress.ip_address(raw)
                        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
                            address = address.ipv4_mapped
                        return str(address), int(port, 16)
                    server, server_port = endpoint(fields[1])
                    peer, peer_port = endpoint(fields[2])
                    candidates.append({'caller_pid': caller, 'caller_start_ticks': caller_start,
                        'transport_pid': current, 'transport_start_ticks': started, 'socket_inode': fields[9],
                        'peer_ip': peer, 'peer_port': peer_port, 'server_ip': server, 'server_port': server_port,
                        'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()})
        current = parent
    if not candidates:
        raise NetworkError('No verifiable SSH transport was found; no environment fallback is allowed.')
    # Privsep siblings can share one transport. The oldest transport holder is
    # used so a new channel on an old ControlMaster cannot look like a new SSH.
    sockets = {item['socket_inode'] for item in candidates}
    if len(sockets) != 1:
        raise NetworkError('Ambiguous SSH transport ancestry.')
    return min(candidates, key=lambda item: item['transport_start_ticks'])



class Runtime:
    def __init__(self,root=None,run=None):
        self.root=Path(root) if root is not None else Path('/')
        self.run=run or native_run

    def path(self,absolute):
        if (not isinstance(absolute,str) or not absolute.startswith('/') or absolute.startswith('//') or ':' in absolute
                or '\\' in absolute or '\x00' in absolute or any(v in {'.','..'} for v in absolute.split('/'))):
            raise NetworkError('Unsafe network path.')
        path=self.root
        for part in absolute.split('/')[1:]:
            path=path/part
            if path.is_symlink():raise NetworkError('Symbolic network paths are unsupported.')
        return path

    def trusted(self,path,private=False,directory=False):
        path=Path(path);metadata=path.lstat()
        if not (stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)):
            raise NetworkError('Unexpected network filesystem object.')
        for current in (path,*path.parents):
            info=current.lstat()
            if current.is_symlink() or os.name=='posix' and (info.st_uid!=0 or info.st_mode&(0o077 if private and current==path else 0o022)):
                raise NetworkError('Network state is not root controlled.')
            if current==self.root:break

    def directory(self,identifier):
        if not isinstance(identifier,str) or not ID.fullmatch(identifier):raise NetworkError('Invalid network transaction.')
        return TRANSACTIONS+'/'+identifier

    def ensure_dir(self,absolute):
        path=self.path(absolute);missing=[];current=path
        while not current.exists():missing.append(current);current=current.parent
        self.trusted(current,directory=True)
        for directory in reversed(missing):directory.mkdir(mode=0o700);directory.chmod(0o700)
        return path

    @staticmethod
    def sync(path):
        if os.name=='posix':
            descriptor=os.open(path,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(descriptor)
            finally:os.close(descriptor)

    def read(self,absolute,private=False):
        path=self.path(absolute)
        if not path.exists():return None
        self.trusted(path,private)
        if path.stat().st_size>LIMIT:raise NetworkError('Network file exceeds its bound.')
        return path.read_bytes()

    def snapshot(self,absolute):
        raw=self.read(absolute)
        if raw is None:return None
        path=self.path(absolute);info=path.stat();attrs={}
        if hasattr(os,'listxattr'):
            attrs={key:base64.b64encode(os.getxattr(path,key)).decode() for key in os.listxattr(path)}
        return {'sha256':digest(raw),'size':len(raw),'metadata':{'mode':stat.S_IMODE(info.st_mode),'uid':info.st_uid,'gid':info.st_gid,'xattrs':attrs}}

    def atomic(self,absolute,raw,metadata=None):
        path=self.path(absolute);self.ensure_dir('/'+ '/'.join(absolute.split('/')[1:-1]))
        if path.exists():self.trusted(path)
        metadata=metadata or {'mode':0o600,'uid':0,'gid':0,'xattrs':{}}
        descriptor,temporary=tempfile.mkstemp(prefix='.network-',dir=path.parent)
        try:
            with os.fdopen(descriptor,'wb') as stream:
                stream.write(raw);stream.flush()
                if os.name=='posix':os.fchown(stream.fileno(),metadata['uid'],metadata['gid'])
                os.fchmod(stream.fileno(),metadata['mode']) if hasattr(os,'fchmod') else os.chmod(temporary,metadata['mode'])
                for key,value in metadata.get('xattrs',{}).items():os.setxattr(temporary,key,base64.b64decode(value))
                os.fsync(stream.fileno())
            os.replace(temporary,path);self.sync(path.parent)
        finally:
            if os.path.exists(temporary):os.unlink(temporary)

    def read_json(self,absolute,default=None):
        raw=self.read(absolute,private=True)
        return json.loads(raw) if raw is not None else default

    def write_json(self,absolute,value):self.atomic(absolute,encoded(value))
    def write_journal(self,value):self.write_json(self.directory(value['transaction_id'])+'/journal.json',value)

    def journal(self,identifier):
        value=self.read_json(self.directory(identifier)+'/journal.json')
        if not value or value.get('schema')!=1 or value.get('kind')!='network' or value.get('transaction_id')!=identifier:
            raise NetworkError('Network journal is missing or invalid.')
        return value

    def checked(self,argv,timeout=30):
        result=self.run(argv,timeout=timeout)
        if result.returncode:raise NetworkError('A checked network command failed.')
        return result.stdout

    def boot_id(self):return self.path('/proc/sys/kernel/random/boot_id').read_text().strip()
    def proof(self,pid=None):return connection_proof(pid)

    @contextmanager
    def lock(self):
        if fcntl is None:raise NetworkError('Native flock is required.')
        self.ensure_dir(STATE);path=self.path(STATE+'/operation.lock')
        descriptor=os.open(path,os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
        try:
            os.fchmod(descriptor,0o600);self.trusted(path,private=True)
            deadline=time.monotonic()+4
            while True:
                try:fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    if time.monotonic()>=deadline:raise NetworkError('Network worker is busy.')
                    time.sleep(.05)
            yield
        finally:os.close(descriptor)

    def artifacts(self,journal):
        required={'candidate.yaml','before.yaml','watchdog.py'}
        if set(journal['file_hashes'])!=required:raise NetworkError('Network artifact set is invalid.')
        for name,expected in journal['file_hashes'].items():
            if digest(self.read(self.directory(journal['transaction_id'])+'/'+name,private=True) or b'')!=expected:
                raise NetworkError('Frozen network artifact changed.')

    def inventory(self):
        result={}
        directories=['/etc/netplan','/run/netplan','/usr/lib/netplan']
        alias=self.root/'lib'
        if alias.is_symlink():
            if alias.readlink().as_posix() not in {'usr/lib','/usr/lib'}:raise NetworkError('Unknown vendor directory alias.')
        else:directories.append('/lib/netplan')
        for directory in directories:
            path=self.path(directory)
            if not path.exists():continue
            self.trusted(path,directory=True)
            for item in sorted(path.iterdir()):
                if item.name.endswith(('.yaml','.yml')):
                    absolute=directory+'/'+item.name;result[absolute]=self.snapshot(absolute)
        return result

    def owned_overlay(self,journal):
        raw=self.read(journal['overlay'])
        if raw is not None and digest(raw)!=journal['file_hashes']['candidate.yaml']:
            raise NetworkError('An external volatile Netplan edit blocks recovery.')
        return raw is not None

    def check_source(self,journal,allow_committed=False):
        source=journal['source'];actual=self.snapshot(source['path'])
        before=source['snapshot']
        after=dict(before,sha256=journal['file_hashes']['candidate.yaml'],size=journal['candidate_size'],
                   metadata=dict(before['metadata'],mode=0o600 if os.name=='posix' else before['metadata']['mode']))
        if actual!=before and not (allow_committed and actual==after):
            raise NetworkError('An external persistent Netplan edit blocks recovery.')
        current=self.inventory();current.pop(journal['overlay'],None)
        expected=dict(journal['inventory'])
        if allow_committed and actual==after:expected[source['path']]=after
        if current!=expected:raise NetworkError('The Netplan file hierarchy changed externally.')
        for absolute,snapshot in journal.get('dependencies',{}).items():
            if self.snapshot(absolute)!=snapshot:raise NetworkError('Network ownership inputs changed externally.')
        self.owned_overlay(journal)
        return actual==after

    def observed(self,interface):
        links=json.loads(self.checked(['ip','-j','link','show'],timeout=5))
        selected=[v for v in links if v.get('ifname')==interface and v.get('link_type')=='ether']
        if len(selected)!=1:raise NetworkError('Selected Ethernet interface is missing or ambiguous.')
        rows=json.loads(self.checked(['ip','-j','-4','address','show','dev',interface],timeout=5))
        addresses=[v for row in rows for v in row.get('addr_info',[]) if v.get('family')=='inet' and v.get('scope')=='global']
        routes=json.loads(self.checked(['ip','-j','-4','route','show'],timeout=5))
        routes=[{k:v[k] for k in ('dst','gateway','dev','metric','protocol','scope') if k in v} for v in routes if v.get('dev')==interface]
        dns=self.checked(['resolvectl','dns',interface],timeout=5).split(':',1)[-1].strip().split()
        try:dns=[str(ipaddress.ip_address(v)) for v in dns]
        except ValueError:raise NetworkError('Link DNS observation is unavailable.') from None
        return {'addresses':[v['local']+'/'+str(v['prefixlen']) for v in addresses],
            'dynamic':[v['local'] for v in addresses if v.get('dynamic')], 'routes':routes,'dns':dns,
            'mac':selected[0].get('address','').lower()}

    def check_runtime(self,journal,config=None,observed=None):
        config=config or journal['config'];value=observed if observed is not None else self.observed(config['interface'])
        if value['mac']!=journal['mac']:raise NetworkError('Selected interface MAC changed.')
        if len(value['addresses'])!=1:raise NetworkError('Unexpected additional global IPv4 addresses are active.')
        defaults=[v for v in value['routes'] if v.get('dst')=='default']
        if config['mode']=='static':
            if config['address'] not in value['addresses'] or value['dynamic']:raise NetworkError('Target static address is not active without a dynamic lease.')
            target=config['address'].split('/')[0]
        else:
            if len(value['dynamic'])!=1:raise NetworkError('A single DHCP IPv4 lease is required.')
            target=value['dynamic'][0]
        if config['mode']=='static' and ([v.get('gateway') for v in defaults] != ([config['gateway']] if config['gateway'] else [])):
            raise NetworkError('Target default route is not active.')
        if config['mode']=='dhcp' and len(defaults)>1:raise NetworkError('Multiple DHCP default routes are unsupported.')
        subnet=ipaddress.ip_interface(value['addresses'][0]).network
        gateways={v.get('gateway') for v in defaults if v.get('gateway')}
        for route in value['routes']:
            destination=route.get('dst')
            if destination=='default':
                if (route.get('gateway') and ipaddress.ip_address(route['gateway']) not in subnet
                        or config['mode']=='dhcp' and route.get('protocol','dhcp')!='dhcp'):
                    raise NetworkError('Unmanaged default routing is unsupported.')
                continue
            network=ipaddress.ip_network(destination,strict=False)
            connected=network==subnet and not route.get('gateway')
            gateway_host=network.prefixlen==32 and str(network.network_address) in gateways and not route.get('gateway')
            if not (connected or gateway_host):raise NetworkError('Unmanaged IPv4 routes require separate support.')
        if not set(config['dns'])<=set(value['dns']):raise NetworkError('Target manual DNS is not active.')
        return target,value

    @staticmethod
    def unit(role,identifier):
        if not ID.fullmatch(identifier) or role not in {'guard','apply','confirm','recover'}:raise NetworkError('Invalid network service.')
        return 'threewa-net-'+role+'-'+identifier+'.service'

    def arm(self,identifier):
        with self.lock():
            value=self.journal(identifier);self.artifacts(value)
            if value['boot_id']!=self.boot_id():raise NetworkError('Network trial belongs to another boot.')
            if value['status']=='NETWORK_PREPARED':
                value.update(status='NETWORK_ARMED',deadline_boottime=now()+value['timeout_seconds']);self.write_journal(value)
            return value

    def apply(self,identifier):
        with self.lock():
            value=self.journal(identifier);self.artifacts(value)
            if value['status']!='NETWORK_ARMED' or value['boot_id']!=self.boot_id() or now()>=value['deadline_boottime']-15:
                raise NetworkError('An acknowledged live watchdog is required.')
            self.check_source(value)
            if self.read(value['overlay']) is not None:raise NetworkError('The volatile source already exists.')
            value['status']='NETWORK_APPLYING';self.write_journal(value)
            self.atomic(value['overlay'],self.read(self.directory(identifier)+'/candidate.yaml',private=True))
            self.checked(['netplan','generate'],timeout=30)
            self.checked(['netplan','apply'],timeout=60)
            target,observed=self.check_runtime(value)
            value.update(status='NETWORK_AWAITING_NEW_SSH',target_ip=target,observed=observed,applied_ticks=ticks())
            self.write_journal(value)

    def verified_proof(self,value,request):
        actual=self.proof(request['caller_pid']);origin=value['origin']
        if (actual!=request or actual['boot_id']!=value['boot_id'] or actual['socket_inode']==origin['socket_inode']
                or actual['transport_start_ticks']<=value['applied_ticks'] or actual['peer_ip']!=origin['peer_ip']
                or actual['server_port']!=origin['server_port'] or actual['server_ip']!=value['target_ip']):
            raise NetworkError('Confirm using a new physical SSH transport to the target IPv4.')
        target,_=self.check_runtime(value)
        if target!=actual['server_ip']:raise NetworkError('SSH endpoint does not match the selected interface.')
        return actual

    def remove_overlay(self,value):
        if self.owned_overlay(value):
            path=self.path(value['overlay']);path.unlink();self.sync(path.parent)

    def confirm(self,identifier):
        with self.lock():
            value=self.journal(identifier);self.artifacts(value)
            if value['status']!='NETWORK_AWAITING_NEW_SSH' or value['boot_id']!=self.boot_id() or now()>=value['deadline_boottime']:
                raise NetworkError('Network confirmation has expired.')
            self.check_source(value)
            if not self.owned_overlay(value):raise NetworkError('Trial overlay is missing.')
            proof=self.verified_proof(value,self.read_json(self.directory(identifier)+'/confirm-request.json'))
            value.update(status='NETWORK_CONFIRMING',confirmation=proof,confirmation_sha256=digest(encoded(proof)))
            self.write_journal(value)
            metadata=dict(value['source']['snapshot']['metadata'],mode=0o600)
            self.atomic(value['source']['path'],self.read(self.directory(identifier)+'/candidate.yaml',private=True),metadata)
            self.remove_overlay(value)
            value.update(status='APPLIED',network_status='NETWORK_CONFIRMED');self.write_journal(value)

    def expire(self,identifier):
        # Stop bounded mutation cgroups before waiting for their operation lock.
        for role in ('apply','confirm'):
            unit=self.unit(role,identifier)
            result=self.run(['systemctl','show',unit,'--property=LoadState','--property=ActiveState'],timeout=5)
            properties=dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
            if properties.get('LoadState')=='not-found':continue
            if result.returncode or properties.get('LoadState')!='loaded':raise NetworkError('Cannot inspect the network mutation cgroup.')
            stopped=self.run(['systemctl','stop',unit],timeout=10)
            if stopped.returncode:
                again=self.run(['systemctl','show',unit,'--property=LoadState'],timeout=5)
                if 'LoadState=not-found' not in again.stdout.splitlines():raise NetworkError('Network mutation cgroup could not be stopped.')
        return self.recover(identifier)

    def recover(self,identifier):
        with self.lock():
            value=self.journal(identifier);self.artifacts(value)
            if value['status'] in FINAL:return value
            confirming=value['status']=='NETWORK_CONFIRMING'
            committed=self.check_source(value,allow_committed=confirming)
            if committed:
                proof=value.get('confirmation')
                if (not confirming or not isinstance(proof,dict) or digest(encoded(proof))!=value.get('confirmation_sha256')
                        or proof.get('server_ip')!=value.get('target_ip') or proof.get('boot_id')!=value['boot_id']):
                    raise NetworkError('Persistent candidate lacks a durable verified confirmation intent.')
                self.remove_overlay(value)
                value.update(status='APPLIED',network_status='NETWORK_CONFIRMED');self.write_journal(value);return value
            had_overlay=self.owned_overlay(value);self.remove_overlay(value)
            rebooted=value['boot_id']!=self.boot_id()
            if not rebooted and (had_overlay or value['status'] not in {'NETWORK_PREPARED','NETWORK_ARMED'}):
                self.checked(['netplan','generate'],timeout=30);self.checked(['netplan','apply'],timeout=60)
                self.check_runtime(value,value['before_config'])
            value.update(status='ROLLED_BACK',network_status='NETWORK_BOOT_ROLLED_BACK' if rebooted else 'NETWORK_ROLLED_BACK')
            self.write_journal(value);return value

    def boot(self):
        path=self.path(TRANSACTIONS)
        if not path.exists():return
        for child in sorted(path.iterdir()):
            if ID.fullmatch(child.name):
                value=self.read_json(TRANSACTIONS+'/'+child.name+'/journal.json')
                if value and value.get('kind')=='network' and value['status'] not in FINAL:self.recover(child.name)


def notify(message):
    address=os.environ.get('NOTIFY_SOCKET')
    if not address:raise NetworkError('Independent systemd notification socket is required.')
    if address.startswith('@'):address='\0'+address[1:]
    with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as channel:channel.connect(address);channel.sendall(message.encode())


def main():
    if os.name!='posix' or os.geteuid()!=0:raise NetworkError('Native root network worker is required.')
    runtime=Runtime();role=sys.argv[1] if len(sys.argv)>1 else ''
    if role=='boot' and len(sys.argv)==2:runtime.boot();return
    if len(sys.argv)!=3 or role not in {'guard','apply','confirm','recover'}:raise NetworkError('Invalid worker invocation.')
    identifier=sys.argv[2]
    if role=='guard':
        runtime.arm(identifier);notify('READY=1')
        while True:
            value=runtime.journal(identifier)
            if value['status'] in FINAL:return
            notify('WATCHDOG=1')
            if value['boot_id']!=runtime.boot_id() or now()>=value['deadline_boottime']:runtime.expire(identifier);return
            time.sleep(.25)
    elif role=='recover':runtime.expire(identifier)
    else:getattr(runtime,role)(identifier)


if __name__=='__main__':main()

"""Pinned upstream BanIP update with independent SSH-confirmed rollback."""
import hashlib
import ipaddress
from pathlib import Path
import subprocess
import time
import urllib.request

from .root_actions import Host, RootActionError, RootManager, _fingerprint
from .trusted_commands import resolve, environment
from . import firewall_watchdog as fw
from . import banip_watchdog as guard

SOURCE_URL='https://3wa.tw/myapache_banip/api.php?mode=download_script&type=linux'
DATA_URL='https://3wa.tw/myapache_banip/data/banip_csv.txt'
SOURCE_SHA256='88f0b6b63c1545816e1475b732b4050220db58618ddff8608fd862dc81212036'
SCRIPT_SHA256='1fae776d2be394e531fc6f051f74b79c8ef8823211b986e60f3c9d79926cfc0c'
SCRIPT='/root/my_ban_ips.sh'


def prepare_script(raw):
    original=b'while IFS= read -r line || [ -n "$line" ]; do'
    old_normalization=b'        ip="$(echo "$line" | tr -d \'\\r\' | xargs)"'
    new_normalization=(b'        ip="${line//$\'\\r\'/}"\n'
                       b'        ip="${ip#"${ip%%[![:space:]]*}"}"\n'
                       b'        ip="${ip%"${ip##*[![:space:]]}"}"')
    if (hashlib.sha256(raw).hexdigest()!=SOURCE_SHA256 or raw.count(original)!=1
            or raw.count(old_normalization)!=1):
        raise RootActionError('The downloaded BanIP source does not match the reviewed version.','BANIP_SOURCE_CHANGED')
    result=raw.replace(old_normalization,new_normalization)
    if hashlib.sha256(result).hexdigest()!=SCRIPT_SHA256:
        raise RootActionError('The managed BanIP normalization correction could not be verified.','BANIP_SOURCE_CHANGED')
    return result


class BanIPHost(Host):
    def available(self,name):return resolve(name) is not None

    def download_script(self):
        try:
            with urllib.request.urlopen(SOURCE_URL,timeout=15) as response:
                data=response.read(8193)
            if len(data)>8192:raise ValueError()
            return data
        except Exception:
            raise RootActionError('The fixed HTTPS BanIP source could not be downloaded.','BANIP_DOWNLOAD_FAILED') from None

    def run(self,argv,timeout=30):
        names={'nft','iptables','iptables-save','ipset','curl','bash','tr','xargs','awk','dpkg-query','apt-get','systemctl'}
        if (not isinstance(argv,(list,tuple)) or not argv or argv[0] not in names
                or any(not isinstance(value,str) or '\0' in value for value in argv)
                or not isinstance(timeout,(int,float)) or not 0<timeout<=1200):
            raise RootActionError('Invalid bounded BanIP command.','UNSAFE_COMMAND')
        if self.root!=Path('/'):raise RootActionError('Prefixed hosts need a native test command boundary.')
        binary=resolve(argv[0])
        if not binary:raise RootActionError('A required trusted BanIP command is unavailable.','MISSING_COMMAND')
        try:
            result=subprocess.run([binary,*argv[1:]],stdin=subprocess.DEVNULL,capture_output=True,
                text=True,timeout=timeout,check=False,shell=False,env={**environment(),'DEBIAN_FRONTEND':'noninteractive','NEEDRESTART_MODE':'l'})
            if len(result.stdout)>4*1024*1024:raise RootActionError('BanIP command output exceeded its bound.','BANIP_FAILED')
            return result
        except subprocess.TimeoutExpired:raise RootActionError('BanIP command timed out.','TIMEOUT') from None
        except OSError:raise RootActionError('BanIP command failed.','BANIP_FAILED') from None


def verify_companion(runtime):
    """Permit only the root-attested exact ip/filter companion, never foreign hooks."""
    try:
        current=guard.Runtime(runtime.root,runtime.run)
        owned=current.read_json(guard.OWNED)
        if not owned:return False
        snapshot=current.snapshot()
        return (snapshot['exists'] and snapshot['rules']==current.rules(owned['origin'])
            and snapshot['filter_sha256']==owned['filter_sha256'] and snapshot['set_sha256']==owned['set_sha256']
            and snapshot['set_exists'] and not snapshot['temp_exists'])
    except Exception:return False


class BanIPManager:
    def __init__(self,host=None):
        self.host=host or BanIPHost();self.core=RootManager(self.host)
        self.runtime=guard.Runtime(self.host.root,self.host.run)

    def _error(self,error):
        if isinstance(error,RootActionError):return error
        return RootActionError(str(error) if isinstance(error,(fw.FirewallError,ValueError)) else
            'BanIP operation failed; inspect its private transaction.','BANIP_FAILED')

    def _pending(self):
        return [item for item in self.core._journals() if item.get('kind')=='banip' and item.get('status') not in guard.FINAL]

    def _preflight(self):
        self.core._gate('time')
        for name in ('nft','systemctl','env','bash','tr','xargs','awk'):
            if not self.host.available(name):raise fw.FirewallError('A required native BanIP runtime is unavailable.')
        for unit in ('nftables.service','firewalld.service','docker.service','containerd.service','kubelet.service','netfilter-persistent.service'):
            output=self.runtime.checked(['systemctl','show',unit,'--property=LoadState','--property=ActiveState','--property=UnitFileState'],timeout=5)
            properties=dict(line.split('=',1) for line in output.splitlines() if '=' in line)
            if properties.get('LoadState')=='loaded' and (properties.get('ActiveState')=='active' or properties.get('UnitFileState') in {'enabled','enabled-runtime'}):
                raise fw.FirewallError('Another active firewall or container manager is unsupported.')
        ufw=self.host.read('/etc/ufw/ufw.conf')
        if ufw is not None:
            import re
            if re.findall(r'^\s*ENABLED\s*=\s*(\S+)\s*$',ufw,re.M)!=['no']:raise fw.FirewallError('UFW is enabled or ambiguous.')
        for path in ('/proc/sys/net/ipv4/ip_forward','/proc/sys/net/ipv6/conf/all/forwarding'):
            if (self.host.read(path) or '').strip()!='0':raise fw.FirewallError('Forwarding hosts are unsupported.')
        iptables=self.host.available('iptables')
        if iptables and not self.host.available('iptables-save'):raise fw.FirewallError('Native iptables-save is unavailable.')
        if iptables and 'nf_tables' not in self.runtime.checked(['iptables','--version'],timeout=5):raise fw.FirewallError('A native nf_tables backend is required.')
        before=self.runtime.snapshot(self.host.available('ipset'),iptables)
        if before['temp_exists']:raise fw.FirewallError('An existing blacklist_new set requires manual inspection.')
        owned=self.runtime.read_json(guard.OWNED)
        if before['set_exists'] or before['rules']:
            if not owned or not verify_companion(self.runtime):raise fw.FirewallError('Existing blacklist/filter objects are not owned or changed externally.')
        elif owned:raise fw.FirewallError('Attested blacklist state is missing from the kernel.')
        if before['foreign_hooks']:
            service=fw.Runtime(self.host.root,self.host.run)
            committed=service.read_json(fw.CONFIRMED)
            if (not committed or not service.policy_matches(service.snapshot(),committed['owned'])
                    or any(item['family']!='inet' or item['table']!=fw.TABLE for item in before['foreign_hooks'])):
                raise fw.FirewallError('Foreign firewall hooks need separate integration.')
        return before

    def status(self):
        pending=None
        try:
            self.core._gate('time')
            journals=self._pending();active=journals[-1] if journals else None
            pending=({'transaction_id':active['transaction_id'],'status':active['status'],
                'deadline_boottime':active.get('deadline_boottime'),'remaining_seconds':max(0,int(active.get('deadline_boottime',fw.now())-fw.now()))} if active else None)
            before=self._preflight()
            missing=sorted(name for name in ('curl','ipset','iptables') if not self.host.available(name))
            return {'supported':True,'installed':not missing,'packages_missing':missing,'active':before['set_exists'] and bool(before['rules']),
                'count':before['count'],'set_sha256':before['set_sha256'],'script_sha256':SCRIPT_SHA256,'source_sha256':SOURCE_SHA256,
                'persistent':False,'pending':pending,
                'detail':'One manual IPv4 blacklist update; loopback and actual SSH rescue are retained. No cron or boot persistence.'}
        except Exception as error:
            return {'supported':False,'installed':False,'packages_missing':[],'active':None,'count':None,'set_sha256':None,
                'script_sha256':SCRIPT_SHA256,'source_sha256':SOURCE_SHA256,'persistent':False,'pending':pending,'detail':str(self._error(error))}

    def plan(self):
        try:
            before=self._preflight();origin=self.runtime.proof()
            if ipaddress.ip_address(origin['peer_ip']).version!=4:raise fw.FirewallError('This IPv4 blacklist requires an actual IPv4 SSH management transport.')
            result={'schema':1,'module':'banip','packages':sorted(name for name in ('curl','ipset','iptables') if not self.host.available(name)),
                'origin':origin,'rescue':{'port':origin['server_port'],'sources':[origin['peer_ip']+'/32']},'before':before,
                'source_url':SOURCE_URL,'data_url':DATA_URL,'source_sha256':SOURCE_SHA256,'script_sha256':SCRIPT_SHA256,'timeout_seconds':1200,
                'notes':['Run the reviewed upstream script with its EOF handling preserved and a Bash whitespace-normalization correction; no cron or boot persistence.',
                         'A private 8 MiB /tmp and 900 second systemd process bound protect fixed upstream temporary paths.',
                         'A separate root watchdog is ready before kernel changes; confirm using a new physical SSH connection.',
                         'Only IPv4 blacklist traffic is affected. Service firewall policy and IPv6 are unchanged.']}
            result['fingerprint']=_fingerprint(result);return result
        except Exception as error:raise self._error(error) from None

    def _install(self):
        runtime=self.runtime
        runtime.ensure_dir('/etc/systemd/system',private=False)
        runtime.ensure_dir(guard.STATE+'/home')
        prefix=Path(fw.__file__).read_text().split("\nif __name__ == '__main__':",1)[0]
        standalone=(prefix+'\n'+Path(guard.__file__).read_text()).encode()
        runtime.atomic(guard.STATE+'/watchdog.py',standalone)
        base='\nUser=root\nGroup=root\nKillMode=control-group\nTimeoutStopSec=2s\nSendSIGKILL=yes\nUMask=0077\n'
        clean='Environment=PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root LANG=C LC_ALL=C\nUnsetEnvironment=BASH_ENV ENV LD_PRELOAD LD_LIBRARY_PATH PYTHONPATH PYTHONHOME CURL_HOME\n'
        units={
            'threewa-banip-guard@.service':'[Unit]\nDescription=3WA independent BanIP rollback watchdog\n\n[Service]\nType=notify\nNotifyAccess=main\nRestart=on-failure\nRestartSec=1s\nWatchdogSec=60s\nTimeoutStartSec=12s\nExecStart=/usr/bin/python3 -I '+guard.STATE+'/watchdog.py guard %i\n'+base+clean,
            'threewa-banip-rollback@.service':'[Unit]\nDescription=3WA BanIP rollback\n\n[Service]\nType=oneshot\nTimeoutStartSec=30s\nExecStart=/usr/bin/python3 -I '+guard.STATE+'/watchdog.py rollback %i\n'+base+clean,
            'threewa-banip-run@.service':'[Unit]\nDescription=3WA reviewed BanIP one-shot update\n\n[Service]\nType=oneshot\nTimeoutStartSec=900s\nPrivateTmp=yes\nTemporaryFileSystem=/tmp:rw,size=8M,mode=0700\nExecStart=/usr/bin/python3 -I '+guard.STATE+'/watchdog.py run %i\n'+base+clean}
        for name,content in units.items():
            path=self.host.path('/etc/systemd/system/'+name)
            if path.exists() and path.read_bytes()!=content.encode():raise fw.FirewallError('An unknown same-name BanIP unit exists.')
            if not path.exists():self.core._atomic('/etc/systemd/system/'+name,content.encode(),{'mode':0o644,'uid':0,'gid':0,'xattrs':{}})
        runtime.checked(['systemctl','daemon-reload'],timeout=8)

    def update(self,preview):
        identifier=None
        try:
            if not isinstance(preview,dict) or preview.get('fingerprint')!=_fingerprint(preview):raise fw.FirewallError('BanIP preview was modified.')
            with self.core._lock():
                self.core._check_pending();trusted=self.plan()
                if trusted['fingerprint']!=preview['fingerprint']:raise fw.FirewallError('BanIP or SSH state changed after preview.')
                raw=self.host.download_script();managed=prepare_script(raw)
                target=self.host.path(SCRIPT)
                if target.exists() and target.read_bytes()!=managed:raise fw.FirewallError('An existing different /root/my_ban_ips.sh will not be overwritten.')
                if trusted['packages']:
                    self.runtime.checked(['apt-get','install','--yes','--no-install-recommends',*trusted['packages']],timeout=900)
                if not all(self.host.available(name) for name in ('curl','ipset','iptables')):raise fw.FirewallError('Required BanIP packages are still unavailable.')
                after_install=self._preflight()
                if after_install!=trusted['before']:raise fw.FirewallError('Firewall state changed during dependency installation; review again.')
                self.core._preflight_state();self.runtime.ensure_dir(guard.STATE)
                self.runtime.atomic(guard.STATE+'/source.sh',raw)
                self.core._atomic(SCRIPT,managed,{'mode':0o700,'uid':0,'gid':0,'xattrs':{}})
                identifier=self.core._id();self.runtime.ensure_dir(self.runtime.directory(identifier))
                owned=self.runtime.read_json(guard.OWNED)
                before_set=None
                if trusted['before']['set_exists']:
                    ips=guard.members(self.runtime.checked(['ipset','save',guard.SET],timeout=10))
                    before_set=('create blacklist_new hash:ip family inet hashsize 4096 maxelem 65536\n'+''.join('add blacklist_new '+ip+'\n' for ip in ips)).encode()
                    self.runtime.atomic(self.runtime.directory(identifier)+'/before.set',before_set)
                journal={'schema':1,'kind':'banip','module':'banip','status':'BANIP_PREPARED','transaction_id':identifier,'created_ns':time.time_ns(),
                    'origin':trusted['origin'],'before':trusted['before'],'before_owned':owned,'before_set_sha256':fw.digest(before_set) if before_set else None,
                    'boot_id':self.runtime.boot_id(),'timeout_seconds':trusted['timeout_seconds'],'source_sha256':SOURCE_SHA256,'script_sha256':SCRIPT_SHA256}
                self.runtime.write_journal(journal);self._install()
                self.runtime.checked(['systemctl','start',f'threewa-banip-guard@{identifier}.service'],timeout=15)
                journal=self.runtime.journal(identifier)
                if journal['status']!='BANIP_ARMED' or journal['deadline_boottime']<=fw.now()+100:raise fw.FirewallError('Independent BanIP rollback guard did not arm.')
            with self.runtime.lock():
                if self.runtime.snapshot()!=trusted['before']:raise fw.FirewallError('Firewall state changed before BanIP kernel preparation.')
                rules=self.runtime.rules(journal['origin'])
                journal['status']='BANIP_PREPARING_KERNEL';self.runtime.write_journal(journal)
                if not trusted['before']['set_exists']:
                    self.runtime.checked(['ipset','create',guard.SET,'hash:ip','family','inet','hashsize','4096','maxelem','65536'],timeout=8)
                    for rule in reversed(rules):self.runtime.checked(['iptables','-w','5','-I','INPUT',*rule[2:]],timeout=8)
                elif journal['before_owned']['origin']['peer_ip']!=journal['origin']['peer_ip'] or journal['before_owned']['origin']['server_port']!=journal['origin']['server_port']:
                    self.runtime.checked(['iptables','-w','5','-R','INPUT','2',*rules[1][2:]],timeout=8)
                journal['status']='BANIP_RUNNING';self.runtime.write_journal(journal)
            self.runtime.checked(['systemctl','start',f'threewa-banip-run@{identifier}.service'],timeout=920)
            with self.runtime.lock():
                journal=self.runtime.journal(identifier)
                if journal['status']!='BANIP_RUNNING' or fw.now()>=journal['deadline_boottime']:raise fw.FirewallError('BanIP update expired before verification.')
                after=self.runtime.snapshot()
                if not after['count']:raise fw.FirewallError('The upstream script produced an empty blacklist; previous protection is retained.')
                summary=self.runtime.read_json(self.runtime.directory(identifier)+'/data-summary.json')
                if not summary or summary['count']!=after['count'] or summary['set_sha256']!=after['set_sha256']:
                    raise fw.FirewallError('The current downloaded blacklist was not fully verified.')
                after=self.runtime.attest(identifier,journal['origin'])
                journal.update(status='BANIP_AWAITING_NEW_SSH',after=after,applied_ticks=fw.ticks());self.runtime.write_journal(journal)
            return {'status':'AWAITING_NEW_SSH','module':'banip','transaction_id':identifier,'count':after['count'],'set_sha256':after['set_sha256'],'data_summary':summary,
                'remaining_seconds':max(0,int(journal['deadline_boottime']-fw.now())),'deadline_boottime':journal['deadline_boottime'],
                'confirmation_command':'/opt/3waServerSetting/run.sh --banip-confirm '+identifier+' --confirm',
                'message':'IPv4 blacklist active; confirm from a new physical SSH connection before independent rollback.'}
        except Exception as error:
            if identifier:
                try:self.runtime.expire(identifier)
                except Exception:
                    try:
                        journal=self.runtime.journal(identifier);journal['status']='BANIP_RECOVERY_REQUIRED';self.runtime.write_journal(journal)
                    except Exception:pass
            raise self._error(error) from None

    def confirm(self,identifier):
        try:
            self.core._gate('time')
            with self.runtime.lock():
                journal=self.runtime.journal(identifier);actual=self.runtime.proof();origin=journal['origin']
                if (journal['status']!='BANIP_AWAITING_NEW_SSH' or fw.now()>=journal['deadline_boottime']
                        or actual['boot_id']!=journal['boot_id'] or actual['socket_inode']==origin['socket_inode']
                        or actual['transport_start_ticks']<=journal['applied_ticks']
                        or actual['peer_ip']!=origin['peer_ip'] or actual['server_port']!=origin['server_port']):
                    raise fw.FirewallError('A new verified physical SSH transport after the update is required.')
                if not verify_companion(self.runtime):raise fw.FirewallError('BanIP objects changed after application.')
                journal['status']='BANIP_CONFIRMED';journal['confirmation']=actual;self.runtime.write_journal(journal)
            return {'status':'BANIP_CONFIRMED','module':'banip','transaction_id':identifier,'message':'New SSH verified; manual IPv4 blacklist update retained without cron or boot persistence.'}
        except Exception as error:raise self._error(error) from None

    def rollback(self,identifier):
        try:
            self.core._gate('time');self.runtime.expire(identifier,manual=True)
            return {'status':'BANIP_ROLLED_BACK','module':'banip','transaction_id':identifier,'message':'Previous owned blacklist and rules restored; other tables were not changed.'}
        except Exception as error:raise self._error(error) from None

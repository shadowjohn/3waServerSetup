"""Independent BanIP rollback worker; bundled with stdlib safety primitives."""
import ipaddress
import json
from pathlib import Path
import re
import shlex
import sys
import time

if __package__:
    from .firewall_watchdog import Runtime as _BaseRuntime, FirewallError, digest, encoded, normalized, now, ticks, native_run as _fw_run, notify, ID, SAFE_PATH
else:
    # The installed private worker prefixes the reviewed firewall stdlib code.
    _BaseRuntime = Runtime

STATE = '/var/lib/3wa-server-setup/banip'
OWNED = STATE + '/owned.json'
SET = 'blacklist'
TEMP_SET = 'blacklist_new'
FINAL = {'BANIP_CONFIRMED', 'BANIP_ROLLED_BACK', 'BANIP_FAILED'}


def native_run(argv, timeout=15):
    if not argv or argv[0] not in {'nft', 'systemctl', 'iptables', 'iptables-save', 'ipset', 'bash'}:
        raise FirewallError('Unsupported BanIP guard command.')
    import os
    import shutil
    import subprocess
    executable = shutil.which(argv[0], path=SAFE_PATH)
    if not executable:
        raise FirewallError('A required BanIP guard command is missing.')
    resolved = Path(executable).resolve(strict=True)
    for part in [resolved, *resolved.parents]:
        info = part.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise FirewallError('BanIP guard executable is not root controlled.')
    return subprocess.run([executable, *argv[1:]], stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=timeout, check=False,
        env={'PATH':SAFE_PATH,'HOME':STATE+'/home','LANG':'C','LC_ALL':'C'}, shell=False)


def members(text, name=SET):
    rows = [shlex.split(line) for line in text.splitlines() if line.strip()]
    if not rows or rows[0][:3] != ['create',name,'hash:ip']:
        raise FirewallError('The blacklist has an unsupported set type.')
    options = rows[0][3:]
    if len(options)%2:
        raise FirewallError('Unknown blacklist set options.')
    options = dict(zip(options[::2],options[1::2]))
    if (options.get('family')!='inet' or options.get('maxelem')!='65536'
            or set(options)-{'family','hashsize','maxelem','bucketsize','initval'}):
        raise FirewallError('Unknown blacklist set options.')
    found=[]
    for row in rows[1:]:
        if len(row)!=3 or row[:2]!=['add',name]:
            raise FirewallError('Unknown blacklist member record.')
        address=ipaddress.ip_address(row[2])
        if address.version!=4 or str(address)!=row[2]:
            raise FirewallError('A strict IPv4 blacklist is required.')
        found.append(str(address))
    if len(found)>65536 or len(set(found))!=len(found):
        raise FirewallError('Invalid blacklist membership count.')
    return sorted(found,key=lambda value:int(ipaddress.ip_address(value)))


def feed_summary(raw):
    if not isinstance(raw,bytes) or not 0<len(raw)<=8*1024*1024:
        raise FirewallError('The downloaded blacklist is empty or exceeds its private limit.')
    addresses=[]
    for line in raw.decode('ascii').splitlines():
        value=line.replace('\r','').strip(' \t\v\f')
        if not value:continue
        address=ipaddress.ip_address(value)
        if address.version!=4 or str(address)!=value:
            raise FirewallError('The blacklist feed contains unsupported address syntax.')
        addresses.append(value)
    if not addresses or len(addresses)>65536 or len(set(addresses))!=len(addresses):
        raise FirewallError('The blacklist feed must contain unique strict IPv4 addresses.')
    addresses.sort(key=lambda value:int(ipaddress.ip_address(value)))
    return {'raw_sha256':digest(raw),'count':len(addresses),
            'set_sha256':digest(''.join(ip+'\n' for ip in addresses).encode()),
            'ends_with_newline':raw.endswith(b'\n'),
            'source_url':'https://3wa.tw/myapache_banip/data/banip_csv.txt'}


class Runtime(_BaseRuntime):
    def __init__(self, root=None, run=None):
        super().__init__(root,run or native_run)

    def journal(self, identifier):
        value=self.read_json(self.directory(identifier)+'/journal.json')
        if not value or value.get('kind')!='banip' or value.get('transaction_id')!=identifier:
            raise FirewallError('BanIP transaction is missing or invalid.')
        return value

    def lock(self):
        # The base primitive's dedicated module has a different STATE global.
        from contextlib import contextmanager
        @contextmanager
        def locked():
            import os
            try: import fcntl
            except ImportError: raise FirewallError('Native flock is required.') from None
            self.ensure_dir(STATE)
            fd=os.open(self.path(STATE+'/operation.lock'),os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
            try:
                self.trusted(self.path(STATE+'/operation.lock'))
                deadline=time.monotonic()+3
                while True:
                    try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                    except BlockingIOError:
                        if time.monotonic()>=deadline: raise FirewallError('BanIP lock is busy.')
                        time.sleep(.05)
                yield
            finally: os.close(fd)
        return locked()

    @staticmethod
    def rules(origin):
        source=str(ipaddress.ip_network(origin['peer_ip']+'/32'))
        return [['-A','INPUT','-i','lo','-j','ACCEPT'],
                ['-A','INPUT','-s',source,'-p','tcp','-m','tcp','--dport',str(origin['server_port']),'-j','ACCEPT'],
                ['-A','INPUT','-m','set','--match-set',SET,'src','-j','DROP']]

    def policy(self, with_chains=False):
        rows=[shlex.split(line) for line in self.checked(['iptables','-w','5','-S'],timeout=8).splitlines() if line]
        policies=[row for row in rows if row[:1]==['-P']]
        if sorted(policies)!=sorted([['-P',name,'ACCEPT'] for name in ('INPUT','FORWARD','OUTPUT')]):
            raise FirewallError('Only native ACCEPT filter policies are supported.')
        rules=[row for row in rows if row[:1]!=['-P']]
        if any(row[:2]!=['-A','INPUT'] for row in rules):
            raise FirewallError('Foreign filter rules are unsupported.')
        saved=self.checked(['iptables-save','-t','filter'],timeout=8).splitlines()
        significant=[line for line in saved if line and not line.startswith('#')]
        if not significant:
            if rules:raise FirewallError('Native filter serialization is missing.')
            return (rules,[]) if with_chains else rules
        if significant[0]!='*filter' or significant[-1]!='COMMIT':
            raise FirewallError('Unknown native filter serialization.')
        saved_policies=[];saved_rules=[]
        for line in significant[1:-1]:
            tokens=shlex.split(line)
            if line.startswith(':'):
                if len(tokens)!=3 or not re.fullmatch(r'\[[0-9]+:[0-9]+\]',tokens[2]):
                    raise FirewallError('Unsupported native filter chain serialization.')
                saved_policies.append(['-P',tokens[0][1:],tokens[1]])
            else:saved_rules.append(tokens)
        # iptables-nft reports logical ACCEPT defaults for all three builtins
        # through -S even when only INPUT exists in the native nft table.
        materialized=[row[1] for row in saved_policies]
        if (len(set(materialized))!=len(materialized)
                or any(row not in policies for row in saved_policies)
                or (rules and 'INPUT' not in materialized) or saved_rules!=rules):
            raise FirewallError('Native filter inspection disagrees with its serialization.')
        return (rules,materialized) if with_chains else rules

    def run_script(self,identifier):
        import os
        import stat
        journal=self.journal(identifier)
        if journal['status']!='BANIP_RUNNING' or now()>=journal['deadline_boottime']:
            raise FirewallError('The BanIP run is no longer authorized.')
        script=self.path('/root/my_ban_ips.sh')
        info=script.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or stat.S_IMODE(info.st_mode)!=0o700
                or digest(script.read_bytes())!=journal['script_sha256']):
            raise FirewallError('The reviewed BanIP execution script changed.')
        self.checked(['bash','--noprofile','--norc',str(script)],timeout=890)
        feed=Path('/tmp/banip_csv.txt')
        info=feed.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_size>8*1024*1024:
            raise FirewallError('The private downloaded feed is unsafe.')
        summary=feed_summary(feed.read_bytes())
        current=self.snapshot()
        if current['count']!=summary['count'] or current['set_sha256']!=summary['set_sha256']:
            raise FirewallError('The loaded blacklist differs from this downloaded feed.')
        self.write_json(self.directory(identifier)+'/data-summary.json',summary)

    def snapshot(self, sets_available=True, iptables_available=True):
        graph=json.loads(self.checked(['nft','-j','-a','list','ruleset'],timeout=8))['nftables']
        own=[];foreign=[];table=None;hooks=[];chains=[];rule_count=0
        for item in graph:
            kind,value=next(iter(item.items()))
            if kind=='metainfo':continue
            belongs=value.get('family')=='ip' and (value.get('table')=='filter' or kind=='table' and value.get('name')=='filter')
            (own if belongs else foreign).append(item)
            if belongs and kind=='table':table=value
            if belongs and kind=='chain':
                chains.append(value.get('name'))
                if (value.get('name') not in {'INPUT','FORWARD','OUTPUT'} or value.get('hook')!=value['name'].lower()
                        or value.get('policy')!='accept' or value.get('type')!='filter' or value.get('prio')!=0):
                    raise FirewallError('Unknown native ip/filter chain layout.')
            if not belongs and kind=='chain' and value.get('hook'):
                hooks.append({key:value.get(key) for key in ('family','table','name','hook')})
            if belongs and kind not in {'table','chain','rule'}:
                raise FirewallError('Unknown native ip/filter object.')
            if belongs and kind=='rule':rule_count+=1
        names=self.checked(['ipset','list','-name'],timeout=8).splitlines() if sets_available else []
        ips=members(self.checked(['ipset','save',SET],timeout=10)) if SET in names else []
        rules,materialized=self.policy(with_chains=True) if iptables_available else ([],[])
        # iptables-save can serialize the empty ACCEPT FORWARD/OUTPUT defaults
        # even though nft only materialized INPUT. Every native chain still
        # needs a matching serialized ACCEPT policy; virtual extras are inert.
        if (rule_count!=len(rules) or not set(chains).issubset(materialized)
                or (table is not None and 'INPUT' not in chains)):
            raise FirewallError('Native nftables and iptables filter structures disagree.')
        return {'exists':table is not None,'handle':table.get('handle') if table else None,
            'filter_sha256':digest(encoded(normalized(own))),'foreign_sha256':digest(encoded(normalized(foreign))),
            'foreign_hooks':hooks,'rules':rules,
            'set_exists':SET in names,'temp_exists':TEMP_SET in names,'count':len(ips),
            'set_sha256':digest((''.join(ip+'\n' for ip in ips)).encode()) if SET in names else None}

    def attest(self, identifier, origin):
        current=self.snapshot()
        if current['rules']!=self.rules(origin) or not current['set_exists'] or current['temp_exists']:
            raise FirewallError('BanIP policy or temporary set verification failed.')
        value={'schema':1,'transaction_id':identifier,'origin':origin,
               'filter_sha256':current['filter_sha256'],'set_sha256':current['set_sha256'],'count':current['count']}
        self.write_json(OWNED,value)
        return current

    def arm(self, identifier):
        with self.lock():
            journal=self.journal(identifier)
            if journal['status'] in FINAL:return
            if journal['status']!='BANIP_PREPARED' and journal.get('deadline_boottime'):
                # A systemd restart resumes the original deadline, never a new
                # confirmation window. The loop also handles a changed boot.
                return
            if journal['status']!='BANIP_PREPARED' or journal['boot_id']!=self.boot_id():
                raise FirewallError('BanIP transaction cannot arm.')
            journal.update(status='BANIP_ARMED',deadline_boottime=now()+journal['timeout_seconds'])
            self.write_journal(journal)

    def expire(self, identifier, manual=False):
        journal=self.journal(identifier)
        if journal['status'] not in {'BANIP_PREPARED','BANIP_ARMED',*FINAL}:
            self.checked(['systemctl','stop',f'threewa-banip-run@{identifier}.service'],timeout=8)
        return self.rollback(identifier,manual=manual)

    def rollback(self, identifier, manual=False):
        with self.lock():
            journal=self.journal(identifier)
            if journal['status'] in {'BANIP_ROLLED_BACK','BANIP_FAILED'}:return journal
            if journal['status']=='BANIP_CONFIRMED' and not manual:return journal
            current=self.snapshot();before=journal['before']
            if journal['status'] in {'BANIP_PREPARED','BANIP_ARMED'}:
                if current!=before:raise FirewallError('Kernel objects changed before BanIP preparation.')
                journal['status']='BANIP_ROLLED_BACK';self.write_journal(journal)
                return journal
            owned=self.read_json(OWNED)
            if journal['status']=='BANIP_CONFIRMED' and (not owned or owned['transaction_id']!=identifier):
                raise FirewallError('Only the latest confirmed BanIP update can be restored.')
            expected=self.rules(journal['origin'])
            if not before['set_exists']:
                # The private journal owns first-install preparation and undo.
                # A killed operation may leave any ordered subset of our three
                # exact rules; it does not authorize unrelated or duplicate rules.
                valid=[[rule for index,rule in enumerate(expected) if mask&(1<<index)] for mask in range(8)]
            else:
                valid=[expected,self.rules(journal['before_owned']['origin'])]
            if current['rules'] not in valid:
                raise FirewallError('An external filter edit prevents BanIP rollback.')
            if journal.get('after') and journal['status']=='BANIP_CONFIRMED' and (
                    current['filter_sha256']!=journal['after']['filter_sha256'] or current['set_sha256']!=journal['after']['set_sha256']):
                raise FirewallError('An external blacklist edit prevents BanIP rollback.')
            journal['status']='BANIP_ROLLING_BACK'
            self.write_journal(journal)
            if current['temp_exists']:
                members(self.checked(['ipset','save',TEMP_SET],timeout=10),TEMP_SET)
                self.checked(['ipset','destroy',TEMP_SET],timeout=8)
            if before['set_exists']:
                path=self.directory(identifier)+'/before.set'
                data=self.path(path).read_bytes();self.trusted(self.path(path))
                if digest(data)!=journal['before_set_sha256']:
                    raise FirewallError('Previous blacklist backup integrity failed.')
                self.checked(['ipset','-file',str(self.path(path)),'restore'],timeout=15)
                self.checked(['ipset','swap',TEMP_SET,SET],timeout=8)
                self.checked(['ipset','destroy',TEMP_SET],timeout=8)
                oldrules=self.rules(journal['before_owned']['origin'])
                if current['rules']!=oldrules:
                    self.checked(['iptables','-w','5','-R','INPUT','2',*oldrules[1][2:]],timeout=8)
            else:
                # Remove blocking policy before its rescue exceptions. Persisted
                # ROLLING_BACK permits a later worker to resume after any kill.
                for rule in reversed(current['rules']):
                    self.checked(['iptables','-w','5','-D','INPUT',*rule[2:]],timeout=8)
                if current['set_exists']:self.checked(['ipset','destroy',SET],timeout=8)
                if not before['exists'] and current['exists']:
                    fresh=self.snapshot()
                    if fresh['rules']:raise FirewallError('Foreign rules appeared during rollback.')
                    self.checked(['nft','delete','table','ip','handle',str(fresh['handle'])],timeout=8)
            restored=self.snapshot()
            if restored['filter_sha256']!=before['filter_sha256'] or restored['set_sha256']!=before['set_sha256']:
                raise FirewallError('BanIP rollback verification failed.')
            if journal.get('before_owned'):self.write_json(OWNED,journal['before_owned'])
            elif self.path(OWNED).exists():self.trusted(self.path(OWNED));self.path(OWNED).unlink()
            journal['status']='BANIP_ROLLED_BACK';self.write_journal(journal)
            return journal


def main():
    import os
    if os.name!='posix' or os.geteuid()!=0 or len(sys.argv)!=3 or sys.argv[1] not in {'guard','rollback','run'}:
        raise FirewallError('Invalid native BanIP guard invocation.')
    runtime=Runtime();identifier=sys.argv[2]
    if sys.argv[1]=='rollback':runtime.expire(identifier,manual=True);return
    if sys.argv[1]=='run':runtime.run_script(identifier);return
    runtime.arm(identifier);notify('READY=1')
    while True:
        journal=runtime.journal(identifier)
        if journal['status'] in FINAL:return
        notify('WATCHDOG=1')
        if journal['boot_id']!=runtime.boot_id() or now()>=journal['deadline_boottime']:
            runtime.expire(identifier);return
        time.sleep(.25)


if __name__=='__main__':
    try:main()
    except BaseException:
        print('Guarded BanIP operation failed; inspect private transaction state.',file=sys.stderr)
        raise SystemExit(1)

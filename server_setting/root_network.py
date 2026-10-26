"""Primary Ethernet Netplan settings with independent, SSH-confirmed trials."""
import copy
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import tempfile
import time

from .root_actions import Host, RootActionError, RootManager
from . import network_watchdog as guard

DRAFT='/opt/3waServerSetting/config/network/draft.json'
_RUNNING_CODE_ROOT=Path(__file__).resolve().parents[1]
_NAME=re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,14}')
_YAML_READER=r'''
import json,pathlib,sys,yaml
from yaml.events import AliasEvent
class Loader(yaml.SafeLoader): pass
def mapping(loader,node,deep=False):
    result={}
    for key,value in node.value:
        name=loader.construct_object(key,deep=deep)
        if not isinstance(name,str) or name in result: raise ValueError('ambiguous YAML key')
        result[name]=loader.construct_object(value,deep=deep)
    return result
Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,mapping)
raw=pathlib.Path(sys.argv[1]).read_bytes()
if len(raw)>65536: raise ValueError('bounded YAML required')
if any(isinstance(event,AliasEvent) for event in yaml.parse(raw)): raise ValueError('YAML aliases unsupported')
print(json.dumps(yaml.load(raw,Loader=Loader),allow_nan=False))
'''


def normalize(config):
    if not isinstance(config,dict) or set(config)!={'interface','mode','address','gateway','dns'}:
        raise RootActionError('A complete network configuration is required.','NETWORK_INVALID')
    interface=config['interface'];mode=config['mode'];address=config['address'];gateway=config['gateway'];dns=config['dns']
    if (not isinstance(interface,str) or not _NAME.fullmatch(interface) or not isinstance(mode,str) or mode not in {'dhcp','static'}
            or not isinstance(address,str) or not isinstance(gateway,str) or not isinstance(dns,list) or len(dns)>3
            or any(not isinstance(v,str) for v in dns)):
        raise RootActionError('Invalid interface, mode or DNS fields.','NETWORK_INVALID')
    try:
        servers=[str(ipaddress.ip_address(value.strip())) for value in dns if value.strip()]
        if len(servers)!=len(set(servers)):raise ValueError()
        if any(ipaddress.ip_address(value).is_unspecified or ipaddress.ip_address(value).is_multicast for value in servers):raise ValueError()
        address=address.strip();gateway=gateway.strip()
        if mode=='dhcp':
            if address or gateway:raise ValueError()
        else:
            if '/' not in address:raise ValueError()
            target=ipaddress.IPv4Interface(address);network=target.network
            if not 1<=network.prefixlen<=30 or target.ip in {network.network_address,network.broadcast_address} or target.ip.is_loopback or target.ip.is_multicast or target.ip.is_unspecified:raise ValueError()
            address=str(target)
            if gateway:
                route=ipaddress.IPv4Address(gateway)
                if route not in network or route in {target.ip,network.network_address,network.broadcast_address}:raise ValueError()
                gateway=str(route)
    except ValueError:raise RootActionError('Use a valid IPv4 host CIDR, in-subnet gateway and up to three unique DNS IPs.','NETWORK_INVALID') from None
    return {'interface':interface,'mode':mode,'address':address,'gateway':gateway,'dns':servers}


class NetworkHost(Host):
    def run(self,argv,timeout=30):
        if self.root!=Path('/').absolute() or platform.system()!='Linux' or getattr(os,'geteuid',lambda:-1)()!=0:
            raise RootActionError('Native Linux root is required for network commands.','ROOT_REQUIRED')
        try:return guard.native_run(argv,timeout)
        except guard.NetworkError as error:raise RootActionError(str(error),'NETWORK_COMMAND_FAILED') from None


class NetworkManager:
    def __init__(self,host=None):
        self.host=host or NetworkHost();self.core=RootManager(self.host)
        self.runtime=guard.Runtime(self.host.root,self.host.run)

    def _error(self,error):
        if isinstance(error,RootActionError):return error
        return RootActionError(str(error) if isinstance(error,(guard.NetworkError,ValueError)) else
            'Network operation failed; inspect the private network transaction.','NETWORK_FAILED')

    def _gate(self):
        if getattr(os,'geteuid',lambda:-1)()!=0:raise RootActionError('Network changes require root.','ROOT_REQUIRED')
        native=self.host.path('/etc/os-release') if not (self.host.root/'etc/os-release').is_symlink() else self.host.root/'etc/os-release'
        if native.is_symlink():
            if native.readlink().as_posix() not in {'/usr/lib/os-release','../usr/lib/os-release'}:raise RootActionError('Unknown OS alias.','UNSAFE_PATH')
            raw=self.host.read('/usr/lib/os-release') or ''
        else:raw=self.host.read('/etc/os-release') or ''
        fields=dict(line.split('=',1) for line in raw.splitlines() if '=' in line)
        if fields.get('ID','').strip('"')!='ubuntu' or fields.get('VERSION_ID','').strip('"') not in {'22.04','24.04','26.04'}:
            raise RootActionError('Network changes support Ubuntu 22.04, 24.04 and 26.04.','UNSUPPORTED_OS')
        from .release_update import ensure_current_release,ReleaseError
        try:ensure_current_release(_RUNNING_CODE_ROOT)
        except ReleaseError as error:raise RootActionError(str(error),error.code) from None

    def _parse(self,absolute):
        result=self.host.run(['python3','-I','-c',_YAML_READER,str(self.runtime.path(absolute))],timeout=10)
        if result.returncode or len(result.stdout)>65536:raise RootActionError('Netplan/cloud-init YAML must be bounded and unambiguous.','NETWORK_UNSUPPORTED')
        try:return json.loads(result.stdout)
        except (ValueError,TypeError):raise RootActionError('System YAML parser is unavailable.','NETWORK_UNSUPPORTED') from None

    def _pending(self):
        return [value for value in self.core._journals() if value.get('kind')=='network' and value['status'] not in guard.FINAL]

    def _cloud(self):
        dependencies={}
        marker='/etc/cloud/cloud-init.disabled'
        snapshot=self.runtime.snapshot(marker)
        if snapshot is not None:return {marker:snapshot}
        cmdline=self.runtime.read('/proc/cmdline') or b''
        if 'network-config=disabled' in cmdline.decode(errors='strict').split():return {'/proc/cmdline':self.runtime.snapshot('/proc/cmdline')}
        paths=[]
        if self.runtime.read('/etc/cloud/cloud.cfg') is not None:paths.append('/etc/cloud/cloud.cfg')
        directory=self.runtime.path('/etc/cloud/cloud.cfg.d')
        if directory.exists():
            self.runtime.trusted(directory,directory=True)
            paths.extend('/etc/cloud/cloud.cfg.d/'+v.name for v in sorted(directory.iterdir()) if v.name.endswith('.cfg'))
        disabled=False
        for absolute in paths:
            snapshot=self.runtime.snapshot(absolute);value=self._parse(absolute)
            dependencies[absolute]=snapshot
            if isinstance(value,dict) and isinstance(value.get('network'),dict) and 'config' in value['network']:
                disabled=value['network']['config']=='disabled'
        if paths and not disabled:
            raise RootActionError('cloud-init networking is enabled or ambiguous; disable its network regeneration before managing Netplan.','NETWORK_CLOUD_INIT')
        return dependencies

    @staticmethod
    def _config(interface,node):
        addresses=[value for value in node.get('addresses',[]) if isinstance(value,str) and ':' not in value]
        routes=[value for value in node.get('routes',[]) if ':' not in str(value.get('via',''))]
        if node.get('routing-policy') or len(addresses)>1 or any(value.get('to') not in {'default','0.0.0.0/0'} or set(value)-{'to','via','metric'} for value in routes):
            raise RootActionError('Complex IPv4 addresses and routing require separate support.','NETWORK_UNSUPPORTED')
        if any(not isinstance(value,str) for value in node.get('addresses',[])) or len(routes)>1:
            raise RootActionError('Complex address/route definitions are unsupported.','NETWORK_UNSUPPORTED')
        dhcp=node.get('dhcp4',False)
        if type(dhcp) is not bool:raise RootActionError('Ambiguous DHCP setting.','NETWORK_UNSUPPORTED')
        if (dhcp and (addresses or routes or node.get('gateway4')) or routes and node.get('gateway4')):
            raise RootActionError('Additive DHCP/static IPv4 addresses and multiple gateway declarations are unsupported.','NETWORK_UNSUPPORTED')
        return normalize({'interface':interface,'mode':'dhcp' if dhcp else 'static',
            'address':'' if dhcp else (addresses[0] if addresses else ''),
            'gateway':'' if dhcp else node.get('gateway4',(routes[0].get('via','') if routes else '')),
            'dns':node.get('nameservers',{}).get('addresses',[])})

    def _profile(self,allow_pending=False):
        self._gate()
        try:self.runtime.require_exclusive()
        except guard.NetworkError as error:raise RootActionError(str(error),'NETWORK_CONFLICT') from None
        inventory=self.runtime.inventory();pending=self._pending()
        overlay=None
        if pending:
            if len(pending)!=1:raise RootActionError('Multiple pending network transactions need inspection.','RECOVERY_REQUIRED')
            if not allow_pending:raise RootActionError('Recover the pending network trial first.','RECOVERY_REQUIRED')
            item=pending[0]
            if item['overlay'] in inventory:
                self.runtime.owned_overlay(item);overlay=inventory.pop(item['overlay'])
        if len(inventory)!=1:
            raise RootActionError('One unambiguous /etc/netplan YAML source is required; foreign /run or vendor YAML is unsupported.','NETWORK_CONFLICT')
        absolute,snapshot=next(iter(inventory.items()))
        if not re.fullmatch(r'/etc/netplan/[a-zA-Z0-9][a-zA-Z0-9_.-]*\.yaml',absolute):
            raise RootActionError('The primary Netplan source must be a regular /etc YAML file.','NETWORK_CONFLICT')
        dependencies=self._cloud();value=self._parse(absolute)
        if self.runtime.snapshot(absolute)!=snapshot:raise RootActionError('Netplan source changed during inspection.','STALE_PLAN')
        if not isinstance(value,dict) or set(value)!={'network'} or not isinstance(value['network'],dict):
            raise RootActionError('An ordinary Netplan version 2 Ethernet source is required.','NETWORK_UNSUPPORTED')
        network=value['network'];ethernets=network.get('ethernets',{})
        if network.get('version')!=2 or set(network)-{'version','renderer','ethernets'} or not isinstance(ethernets,dict) or len(ethernets)!=1:
            raise RootActionError('Only a single primary Ethernet definition is supported.','NETWORK_UNSUPPORTED')
        interface,node=next(iter(ethernets.items()))
        if not isinstance(node,dict) or not _NAME.fullmatch(interface):raise RootActionError('Invalid Ethernet identity.','NETWORK_UNSUPPORTED')
        if node.get('renderer',network.get('renderer','networkd'))!='networkd':raise RootActionError('Only the networkd renderer is supported.','NETWORK_RENDERER')
        for unit,required in (('systemd-networkd.service',True),('NetworkManager.service',False)):
            result=self.host.run(['systemctl','show',unit,'--property=LoadState','--property=ActiveState','--property=UnitFileState'],timeout=5)
            props=dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
            if (result.returncode or (props.get('ActiveState')=='active')!=required
                    or not required and props.get('UnitFileState') in {'enabled','enabled-runtime','linked','linked-runtime'}
                    or required and props.get('UnitFileState') in {'masked','masked-runtime','disabled'}):
                raise RootActionError('The selected NIC must be managed exclusively by active systemd-networkd.','NETWORK_RENDERER')
        for directory in ('/etc/systemd/network','/run/systemd/network'):
            path=self.runtime.path(directory)
            if path.exists():
                for item in path.iterdir():
                    if item.suffix in {'.network','.netdev'} and (directory.startswith('/etc/') or not re.fullmatch(r'[0-9]+-netplan-[A-Za-z0-9_.-]+\.(network|netdev)',item.name)):
                        raise RootActionError('Custom backend network files require separate integration.','NETWORK_CONFLICT')
        if self.runtime.checked(['findmnt','--noheadings','--output','FSTYPE','--target','/run'],timeout=5).strip()!='tmpfs':
            raise RootActionError('/run must be volatile tmpfs for safe trials.','NETWORK_UNSUPPORTED')
        observed=self.runtime.observed(interface);match=node.get('match',{})
        if (not isinstance(match,dict) or set(match)-{'macaddress'} or match and match.get('macaddress','').lower()!=observed['mac']
                or node.get('set-name',interface)!=interface or not re.fullmatch(r'[0-9a-f]{2}(:[0-9a-f]{2}){5}',observed['mac'])):
            raise RootActionError('Ethernet matching must identify this NIC unambiguously.','NETWORK_UNSUPPORTED')
        if len(observed['addresses'])!=1:raise RootActionError('A single current global IPv4 address is required.','NETWORK_UNSUPPORTED')
        config=self._config(interface,node)
        if not pending:self.runtime.check_runtime({'mac':observed['mac'],'config':config},observed=observed)
        return {'source':{'path':absolute,'snapshot':snapshot},'inventory':inventory,'dependencies':dependencies,
            'document':value,'config':config,'observed':observed,'interface':interface,'mac':observed['mac'],
            'pending':pending[0] if pending else None}

    def status(self):
        pending=None
        try:
            self._gate()
            known=self._pending()
            pending=known[0] if len(known)==1 else None
            profile=self._profile(allow_pending=True);pending=profile['pending'];config=profile['config']
            if pending:config=pending['config']
            return {'supported':True,'detail':'Single Ethernet IPv4; trial is volatile until a new target SSH confirms.',
                'interface':profile['interface'],'interfaces':[profile['interface']],'renderer':'networkd','config':config,
                'observed':profile['observed'],'source':{'path':profile['source']['path'],'sha256':profile['source']['snapshot']['sha256']},
                'saved':self.runtime.path(DRAFT).exists(),'pending':self._pending_public(pending)}
        except Exception as error:
            return {'supported':False,'detail':str(self._error(error)),'interface':None,'interfaces':[],
                'renderer':None,'config':None,'observed':None,'source':None,'saved':False,'pending':self._pending_public(pending)}

    @staticmethod
    def _pending_public(value):
        if not value:return None
        identifier=value['transaction_id']
        return {'transaction_id':identifier,'status':value['status'],'target_ip':value.get('target_ip'),
            'deadline_boottime':value.get('deadline_boottime'),'remaining_seconds':max(0,int(value.get('deadline_boottime',guard.now())-guard.now())),
            'confirmation_command':'/opt/3waServerSetting/run.sh --network-confirm '+identifier+' --confirm'}

    def load_saved(self):
        try:self._gate();value=self.runtime.read_json(DRAFT);return normalize(value) if value is not None else None
        except Exception as error:raise self._error(error) from None

    def save(self,config):
        try:
            self._gate();config=normalize(config)
            with self.core._lock():self._gate();self.core._check_pending();self.runtime.write_json(DRAFT,config)
            return {'status':'SAVED','module':'network','message':'Private network draft saved; live Netplan is unchanged.'}
        except Exception as error:raise self._error(error) from None

    def _plan(self,config):
        config=normalize(config);profile=self._profile()
        if config['interface']!=profile['interface']:raise RootActionError('Select the detected primary Ethernet interface.','NETWORK_INVALID')
        origin=self.runtime.proof()
        if origin['server_ip'] not in [v.split('/')[0] for v in profile['observed']['addresses']]:
            raise RootActionError('Apply from an SSH session using the selected IPv4 interface.','NETWORK_SSH_REQUIRED')
        candidate=copy.deepcopy(profile['document']);node=candidate['network']['ethernets'][config['interface']]
        node['dhcp4']=config['mode']=='dhcp';node.pop('gateway4',None)
        addresses=[v for v in node.get('addresses',[]) if ':' in v]
        if config['mode']=='static':addresses.append(config['address'])
        if addresses:node['addresses']=addresses
        else:node.pop('addresses',None)
        routes=[v for v in node.get('routes',[]) if ':' in str(v.get('via',''))]
        if config['gateway']:routes.append({'to':'default','via':config['gateway']})
        if routes:node['routes']=routes
        else:node.pop('routes',None)
        servers=node.get('nameservers',{});servers.pop('addresses',None)
        if config['dns']:servers['addresses']=config['dns']
        if servers:node['nameservers']=servers
        else:node.pop('nameservers',None)
        overrides=node.get('dhcp4-overrides',{});overrides.pop('use-dns',None)
        if config['mode']=='dhcp' and config['dns']:overrides['use-dns']=False
        if overrides:node['dhcp4-overrides']=overrides
        else:node.pop('dhcp4-overrides',None)
        if node['dhcp4'] and node.get('dhcp6') and node.get('dhcp4-overrides',{})!=node.get('dhcp6-overrides',{}):
            raise RootActionError('DHCPv4/v6 override symmetry would require changing IPv6; this combination is unsupported.','NETWORK_IPV6_POLICY')
        result={'schema':1,'module':'network','config':config,'source':{'path':profile['source']['path'],'sha256':profile['source']['snapshot']['sha256']},
            'before':profile,'candidate':candidate,'origin':origin,'target_ip':config['address'].split('/')[0] if config['mode']=='static' else None,
            'timeout_seconds':120,'cloud_init_action':None,'no_change':candidate==profile['document'],
            'notes':['Only the same-name /run/netplan overlay changes during the 120-second trial.',
                     'Open a new physical SSH connection to the target IPv4 and confirm; an old SSH transport cannot confirm.',
                     'Only confirmation writes the original /etc/netplan source. A reboot before confirmation retains the original persistent source.',
                     'Blank DHCP DNS inherits DHCP; manual entries replace DHCPv4 DNS. Existing IPv6 addressing and RA settings are preserved.']}
        result['fingerprint']=guard.digest(guard.encoded(result));return result

    def plan(self,config):
        try:return self._plan(config)
        except Exception as error:raise self._error(error) from None

    def _spawn(self,role,identifier):
        args=['systemd-run','--quiet','--collect','--unit='+self.runtime.unit(role,identifier),
            '--property=User=root','--property=Group=root','--property=UMask=0077','--property=NoNewPrivileges=yes',
            '--property=KillMode=control-group','--property=TimeoutStopSec=3s',
            '--property=UnsetEnvironment=LD_PRELOAD LD_LIBRARY_PATH PYTHONPATH PYTHONHOME BASH_ENV ENV']
        if role=='guard':
            args+=['--service-type=notify','--property=NotifyAccess=main','--property=Restart=on-failure',
                '--property=RestartSec=1s','--property=WatchdogSec=180s','--property=TimeoutStartSec=10s','--property=RefuseManualStop=yes']
        else:args+=['--service-type=oneshot','--property=TimeoutStartSec=100s']
        args+=['/usr/bin/python3','-I',str(self.runtime.path(self.runtime.directory(identifier)+'/watchdog.py')),role,identifier]
        self.runtime.checked(args,timeout=110 if role!='guard' else 15)

    def _boot_helper(self):
        source=Path(guard.__file__).read_bytes();self.runtime.atomic(guard.STATE+'/boot-helper.py',source)
        content=('[Unit]\nDescription=3WA network trial metadata reconciliation\nDefaultDependencies=no\nAfter=local-fs.target\n'
            'Before=network-pre.target\n\n[Service]\nType=oneshot\nUser=root\nGroup=root\nUMask=0077\n'
            'NoNewPrivileges=yes\nTimeoutStartSec=20s\nExecStart=/usr/bin/python3 -I '+guard.STATE+'/boot-helper.py boot\n'
            '\n[Install]\nWantedBy=multi-user.target\n').encode()
        absolute='/etc/systemd/system/'+guard.BOOT_UNIT;existing=self.runtime.read(absolute)
        if existing is not None and existing!=content:raise RootActionError('Unknown same-name network boot unit exists.','NETWORK_CONFLICT')
        if existing is None:
            path=self.runtime.path('/etc/systemd/system');missing=[];current=path
            while not current.exists():missing.append(current);current=current.parent
            self.runtime.trusted(current,directory=True)
            for directory in reversed(missing):directory.mkdir(mode=0o755);directory.chmod(0o755)
            self.core._atomic(absolute,content,{'mode':0o644,'uid':0,'gid':0,'xattrs':{}})
        self.runtime.checked(['systemctl','daemon-reload'],timeout=8)
        self.runtime.checked(['systemctl','enable',guard.BOOT_UNIT],timeout=8)

    def apply(self,plan):
        identifier=None
        try:
            self._gate()
            if not isinstance(plan,dict):raise RootActionError('A reviewed network plan is required.','NETWORK_INVALID')
            supplied=dict(plan);fingerprint=supplied.pop('fingerprint',None)
            if fingerprint!=guard.digest(guard.encoded(supplied)):raise RootActionError('Network preview was modified.','STALE_PLAN')
            with self.core._lock():
                self._gate();self.core._check_pending();fresh=self._plan(plan['config'])
                if fresh['fingerprint']!=fingerprint:raise RootActionError('Network or SSH state changed after preview.','STALE_PLAN')
                if fresh['no_change']:return {'status':'NO_CHANGE','module':'network','message':'Network configuration already matches.'}
                identifier=self.core._id();directory=self.runtime.directory(identifier)
                staging=guard.STATE+'/prepared-'+identifier;self.runtime.ensure_dir(staging)
                source=fresh['before']['source'];candidate=guard.encoded(fresh['candidate']);before=self.runtime.read(source['path'])
                artifacts={'candidate.yaml':candidate,'before.yaml':before,'watchdog.py':Path(guard.__file__).read_bytes()}
                for name,raw in artifacts.items():self.runtime.atomic(staging+'/'+name,raw)
                validation=staging+'/validation';self.runtime.atomic(validation+source['path'],before)
                # The rollback source must also regenerate successfully; testing
                # only the shadowing replacement could hide a broken baseline.
                self.runtime.checked(['netplan','generate','--root-dir',str(self.runtime.path(validation))],timeout=30)
                self.runtime.atomic(validation+'/run/netplan/'+Path(source['path']).name,candidate)
                self.runtime.checked(['netplan','generate','--root-dir',str(self.runtime.path(validation))],timeout=30)
                journal={'schema':1,'kind':'network','module':'network','transaction_id':identifier,'created_ns':time.time_ns(),
                    'status':'NETWORK_PREPARED','source':source,'inventory':fresh['before']['inventory'],
                    'dependencies':fresh['before']['dependencies'],'config':fresh['config'],'before_config':fresh['before']['config'],
                    'mac':fresh['before']['mac'],'overlay':'/run/netplan/'+Path(source['path']).name,'candidate_size':len(candidate),
                    'origin':fresh['origin'],'boot_id':self.runtime.boot_id(),'timeout_seconds':fresh['timeout_seconds'],
                    'file_hashes':{name:guard.digest(raw) for name,raw in artifacts.items()}}
                self.runtime.write_json(staging+'/journal.json',journal)
                self.runtime.ensure_dir(guard.TRANSACTIONS)
                destination=self.runtime.path(directory)
                if destination.exists():raise RootActionError('Network transaction collision.','NETWORK_CONFLICT')
                os.replace(self.runtime.path(staging),destination)
                self.runtime.sync(destination.parent);self.runtime.sync(self.runtime.path(guard.STATE))
                self._boot_helper();self._spawn('guard',identifier)
                armed=self.runtime.journal(identifier)
                if armed['status']!='NETWORK_ARMED' or armed.get('deadline_boottime',0)<=guard.now()+90:
                    raise RootActionError('Independent network watchdog did not acknowledge readiness.','NETWORK_WATCHDOG')
            self._spawn('apply',identifier)
            journal=self.runtime.journal(identifier)
            if journal['status']!='NETWORK_AWAITING_NEW_SSH':raise RootActionError('Network trial did not reach SSH confirmation.','NETWORK_FAILED')
            return dict(self._pending_public(journal),status='AWAITING_NEW_SSH',module='network',
                message='Connect to the target IPv4 with a new SSH transport and confirm before rollback.')
        except Exception as error:
            if identifier and self.runtime.path(self.runtime.directory(identifier)+'/journal.json').exists():
                try:self._spawn('recover',identifier)
                except Exception:pass
            raise self._error(error) from None

    def confirm(self,transaction_id):
        try:
            self._gate();value=self.runtime.journal(transaction_id)
            proof=self.runtime.proof()
            if proof['socket_inode']==value['origin']['socket_inode'] or proof['server_ip']!=value.get('target_ip'):
                raise RootActionError('Confirm from a new SSH connection to the target IPv4.','NETWORK_NEW_SSH_REQUIRED')
            self.runtime.write_json(self.runtime.directory(transaction_id)+'/confirm-request.json',proof)
            self._spawn('confirm',transaction_id)
            if self.runtime.journal(transaction_id)['status']!='APPLIED':raise RootActionError('Network confirmation did not persist.','NETWORK_FAILED')
            return {'status':'CONFIRMED','module':'network','transaction_id':transaction_id,'message':'Target SSH verified; Netplan now persists across reboot.'}
        except Exception as error:raise self._error(error) from None

    def recover(self,transaction_id=None):
        try:
            self._gate()
            if transaction_id is None:
                pending=self._pending()
                if len(pending)!=1:raise RootActionError('A single pending network transaction is required.','NETWORK_NO_PENDING')
                transaction_id=pending[0]['transaction_id']
            self.runtime.journal(transaction_id)
            self._spawn('recover',transaction_id);value=self.runtime.journal(transaction_id)
            if value['status'] not in guard.FINAL:raise RootActionError('Network recovery remains incomplete.','RECOVERY_REQUIRED')
            return {'status':'CONFIRMED' if value['status']=='APPLIED' else 'ROLLED_BACK','module':'network','transaction_id':transaction_id,
                'message':'Durable confirmation finalized.' if value['status']=='APPLIED' else 'Original persistent Netplan source retained and trial removed.'}
        except Exception as error:raise self._error(error) from None

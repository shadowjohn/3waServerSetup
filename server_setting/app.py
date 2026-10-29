"""Local console: nonroot observation, explicit reviewed root transactions."""
import asyncio
import json
import re
import time
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Footer, Header, Input, Label, RichLog, Select, Static, Switch, TabbedContent, TabPane, Tabs, TextArea
from . import VERSION, revision, version_info, update_management_available, release_public, valid_release_tag, network_management_available, network_public
from .model import ROOT, FIELDS, LABELS, preview, firewall_preview, apache_candidate
from .probes import collect, root_management_available
from .diagnostics import sqlite_smoke
from .mascot import render_mascot, ascii_fallback

ROOT_MODULES = tuple(module for module, fields in FIELDS.items() if fields)
MARIADB_SECURITY_ERROR_CODES = {'MARIADB_NOT_READY', 'MARIADB_UNSUPPORTED', 'MARIADB_AUDIT_ACTIVE',
                               'MARIADB_PASSWORD_REQUIRED', 'MARIADB_INVALID_PASSWORD', 'MARIADB_SQL_FAILED',
                               'MARIADB_PASSWORD_VERIFY_FAILED', 'MARIADB_ROLLED_BACK',
                               'TAMPERED_PLAN', 'STALE_PLAN', 'RECOVERY_REQUIRED'}


class RootReview(ModalScreen[bool]):
    """One confirmation for the entire selected-page transaction."""
    BINDINGS = [('escape', 'cancel', 'Cancel')]
    CSS = """
    RootReview { align: center middle; background: #000000 60%; }
    #root-review-box { width: 90%; height: 88%; padding: 1; background: #173438; }
    #root-review-title { height: auto; margin-bottom: 1; }
    #root-review-detail { height: 1fr; }
    #root-review-buttons { height: 3; margin-top: 1; }
    """

    def __init__(self, title, detail):
        super().__init__()
        self.review_title = title
        self.review_detail = detail

    def compose(self):
        with Vertical(id='root-review-box'):
            yield Static(self.review_title, id='root-review-title', markup=False)
            yield TextArea(self.review_detail, read_only=True, id='root-review-detail')
            with Horizontal(id='root-review-buttons'):
                yield Button('Confirm page operation', id='root-confirm', variant='warning')
                yield Button('Cancel', id='root-cancel')

    def action_cancel(self):
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id in {'root-confirm', 'root-cancel'}:
            event.stop()
            self.dismiss(event.button.id == 'root-confirm')


class SettingApp(App):
    TITLE = '3waServerSetting'
    SUB_TITLE = f'{VERSION} | READ ONLY'
    BINDINGS = [('ctrl+q', 'quit', 'Exit'), ('ctrl+r', 'refresh', 'Read status'),
                ('ctrl+right', 'next_page', 'Next page'), ('ctrl+left', 'previous_page', 'Previous page')]
    CSS = """
    Screen { background: #101820; color: #e5f4f4; }
    #layout { height: 1fr; }
    #main { width: 1fr; }
    #mascot { width: 24; padding: 1; background: #101820; border-left: solid #2c6868; }
    #mode { height: auto; padding: 0 1; background: #173438; }
    .module-state { height: auto; padding: 1; border: solid #2c6868; }
    .field { height: 3; }
    .field Label { width: 38; padding: 1 0; }
    .field Input { width: 1fr; }
    .buttons { height: auto; min-height: 3; }
    Button { margin-right: 1; min-width: 12; }
    .output { min-height: 10; height: 16; border: solid #2c6868; }
    TabPane { padding: 1; }
    VerticalScroll { height: 1fr; }
    .hint { height: auto; color: #a8c6c6; margin-bottom: 1; }
    #fw-sources, #fw-ports { width: 1fr; }
    #packages-list { height: auto; }
    #packages-list Checkbox { width: 1fr; height: auto; min-height: 1; }
    #operation-progress { height: auto; max-height: 3; padding: 0 1; background: #173438; display: none; }
    #operation-log { height: 4; display: none; }
    """

    def __init__(self, ascii_mode=False, plain=False, root_manager=None, firewall_manager=None, lvm_manager=None, swap_manager=None, banip_manager=None, package_manager=None, php_feather_manager=None, startup_manager=None, release_manager=None, network_manager=None, apache_fpm_manager=None):
        super().__init__()
        self.root_manager = root_manager
        self.root_unavailable = ''
        if self.root_manager is None and root_management_available():
            try:
                from .root_actions import RootManager
                self.root_manager = RootManager()
            except Exception as error:
                self.root_unavailable = str(error)
        self.root_mode = self.root_manager is not None
        self.apache_fpm_manager = apache_fpm_manager
        self.update_root = update_management_available()
        self.network_root = network_management_available()
        self.network_manager = network_manager
        self.network_state = {'supported':False, 'pending':None, 'detail':'Network state has not been read.'}
        self._network_initialized = False
        self._network_interfaces = []
        if self.network_manager is None:
            try:
                from .root_network import NetworkManager
                self.network_manager = NetworkManager()
            except Exception:
                self.network_state['detail'] = 'Network backend is unavailable.'
        self.release_manager = release_manager
        self.release_state = {'supported':False, 'managed':False, 'pending':None,
                              'detail':'Local release status has not been read.'}
        if self.release_manager is None:
            try:
                from .release_update import ReleaseManager
                self.release_manager = ReleaseManager()
            except Exception:
                self.release_state['detail'] = 'Release backend is unavailable.'
        self.firewall_manager = firewall_manager
        self.firewall_state = {}
        self._fw_status_at = time.monotonic()
        self.banip_manager = banip_manager
        self.banip_state = {}
        self._banip_status_at = time.monotonic()
        self.package_manager = package_manager
        self.package_catalog = []
        self.package_search_items = []
        self.package_item_records = {}
        self.package_state = {'supported': False, 'detail': 'Package catalog has not been read.'}
        self.php_feather_manager = php_feather_manager
        self.php_feather_state = {'supported': False, 'detail': 'FeatherMountain SQLite status has not been read.'}
        self.startup_manager = startup_manager
        self.startup_state = {'supported': False, 'detail': 'Startup status has not been read.'}
        self._startup_initialized = False
        self.lvm_manager = lvm_manager
        self.lvm_state = {}
        self.swap_manager = swap_manager
        self.swap_state = {}
        self._swap_initialized = False
        self._swap_editor_path = '/swap.img'
        self.fw_draft = None
        self._fw_draft_loaded = False
        if self.root_mode:
            from .firewall_rules import default_config
            self.fw_draft = default_config()
            if self.firewall_manager is None:
                try:
                    from .root_firewall import FirewallManager
                    self.firewall_manager = FirewallManager()
                except Exception:
                    self.firewall_state = {'supported': False, 'detail': 'Firewall backend is unavailable.'}
            if self.lvm_manager is None:
                from .root_lvm import LVMManager
                self.lvm_manager = LVMManager()
            if self.banip_manager is None:
                try:
                    from .root_banip import BanIPManager
                    self.banip_manager = BanIPManager()
                except Exception:
                    self.banip_state = {'supported': False, 'detail': 'BanIP backend is unavailable.'}
            if self.package_manager is None:
                try:
                    from .root_packages import PackageManager
                    self.package_manager = PackageManager()
                except Exception:
                    self.package_state = {'supported': False, 'detail': 'Package backend is unavailable.'}
            if self.php_feather_manager is None:
                try:
                    from .php_feather import FeatherSQLiteManager
                    self.php_feather_manager = FeatherSQLiteManager()
                except Exception:
                    self.php_feather_state = {'supported': False, 'detail': 'FeatherMountain SQLite backend is unavailable.'}
            if self.startup_manager is None:
                try:
                    from .root_startup import StartupManager
                    self.startup_manager = StartupManager()
                except Exception:
                    self.startup_state = {'supported': False, 'detail': 'Startup backend is unavailable.'}
            if self.swap_manager is None:
                try:
                    from .root_swap import SwapManager
                    self.swap_manager = SwapManager()
                except Exception:
                    self.swap_state = {'supported': False, 'detail': 'Swap backend is unavailable.'}
        self.root_busy = False
        self._operation_started = None
        self._operation_event = None
        self._operation_event_at = None
        self._operation_last_stage = None
        self._operation_session = 0
        self.root_capabilities = {}
        self._root_initialized = set()
        self._busy_widgets = []
        self._root_task = None
        self._mariadb_password = None
        self.mariadb_security = {}
        mode = 'ROOT MANAGEMENT' if self.root_mode else ('SERVICE READ ONLY / NETWORK AND PROGRAM MANAGEMENT' if self.network_root or self.update_root else 'READ ONLY')
        identity = version_info()
        self.sub_title = f'{identity["version"]} / commit {identity["commit"][:12]} / {identity["channel"]} | {mode}'
        self.ascii_mode = ascii_fallback(ascii_mode)
        self.plain = plain
        self.snapshot = {'modules': {}}
        self.fw_config = json.loads((ROOT / 'config/firewall.example.json').read_text(encoding='utf-8'))
        self.field_ids = {}

    def compose(self) -> ComposeResult:
        yield Header()
        mode = ('ROOT / Ubuntu 26.04: review and confirm one selected-page operation. '
                'Firewall needs a new SSH confirmation. Root LV growth is irreversible; OS accounts: view only.' if self.root_mode else
                'Service settings are READ ONLY on this platform. Network and program changes use their own reviewed operations.' if self.network_root or self.update_root else
                'READ ONLY: drafts and previews stay in memory. No system apply / installs / SSH.')
        if self.root_unavailable:
            mode += ' Management unavailable: ' + self.root_unavailable
        yield Static(mode, id='mode', markup=False)
        with Horizontal(id='layout'):
            with Vertical(id='main'):
                with TabbedContent(initial='tab-overview', id='pages'):
                    with TabPane('Overview', id='tab-overview'):
                        with VerticalScroll():
                            yield Static('Reading local status...', id='overview', markup=False)
                            with Horizontal(classes='buttons'):
                                yield Button('Read status', id='refresh')
                                yield Button('Exit', id='exit')
                    for module, fields in FIELDS.items():
                        with TabPane(LABELS[module], id=f'tab-{module}'):
                            with VerticalScroll():
                                yield Static('UNVERIFIED: not read yet.', id=f'state-{module}', classes='module-state', markup=False)
                                hint = ('Managed values are saved only after a reviewed operation succeeds. '
                                        'Configuration verification and service health are reported separately.'
                                        if self.root_mode and module in ROOT_MODULES else
                                        'Draft targets only. Observed config is partial; effective runtime is separate. Nothing is saved on Exit.')
                                yield Static(hint, classes='hint', markup=False)
                                if self.root_mode and module in ROOT_MODULES:
                                    yield Static('Reading root recipe capabilities...', id=f'root-cap-{module}', classes='hint', markup=False)
                                    if module == 'ssh':
                                        yield Static('SSH 閒置連線保活：先檢查設定語法與全域生效值，再 reload 已啟動的 SSH。新連線使用新值，現有登入保留；本頁服務與開機狀態維持現況。', classes='hint', markup=False)
                                self.field_ids[module] = {}
                                for index, (key, default) in enumerate(fields.items()):
                                    widget_id = f'{module}-{index}'
                                    self.field_ids[module][key] = widget_id
                                    with Horizontal(classes='field'):
                                        yield Label(key, markup=False)
                                        yield Input(default, id=widget_id)
                                if self.root_mode and module in ROOT_MODULES:
                                    with Horizontal(classes='field'):
                                        yield Label('Service now', markup=False)
                                        yield Select([('Keep current state', 'keep'), ('Start service', 'start'), ('Stop service', 'stop')],
                                                     value='keep', allow_blank=False, id=f'root-active-{module}')
                                    with Horizontal(classes='field'):
                                        yield Label('Start automatically at boot', markup=False)
                                        yield Select([('Keep current boot setting', 'keep'), ('Enable at boot', 'enable'), ('Disable at boot', 'disable')],
                                                     value='keep', allow_blank=False, id=f'root-enabled-{module}')
                                    with Horizontal(classes='buttons'):
                                        yield Button('Install service', id=f'root-install-{module}')
                                        yield Button('Load recommended', id=f'root-recommended-{module}')
                                        yield Button('Load saved', id=f'root-saved-{module}')
                                    with Horizontal(classes='buttons'):
                                        yield Button('Apply recommended', id=f'root-apply-recommended-{module}')
                                        yield Button('Save & apply custom', id=f'root-apply-{module}')
                                        yield Button('Restore last', id=f'root-restore-{module}')
                                    if module == 'apache':
                                        yield Static('Apache event + PHP-FPM：獨立檢閱切換 PHP handler 與 MPM；會 restart Apache。套件安裝不會自行切換既有 mod_php。可另行還原 handler，與上方一般設定還原分開。', classes='hint', markup=False)
                                        with Horizontal(classes='buttons'):
                                            yield Button('Read handler status', id='apache-fpm-status')
                                            yield Button('Review event + FPM', id='apache-fpm-apply', variant='warning')
                                            yield Button('Restore handler', id='apache-fpm-restore')
                                        yield TextArea('', read_only=True, id='out-apache-fpm', classes='output')
                                    if module == 'mariadb':
                                        yield Static('MariaDB root access / separate security transaction', classes='module-state', markup=False)
                                        yield Static('Reading live root access flags...', id='mariadb-security-status', classes='hint', markup=False)
                                        with Horizontal(classes='field'):
                                            yield Label('Remote root@% access', markup=False)
                                            yield Select([('Keep current access', 'keep'), ('ON: root@% admin / 0.0.0.0', 'on'),
                                                          ('OFF: lock root@% / 127.0.0.1', 'off')],
                                                         value='keep', allow_blank=False, id='mariadb-remote')
                                        with Horizontal(classes='field'):
                                            yield Label('New root password (empty = keep)', markup=False)
                                            yield Input(password=True, id='mariadb-new-password')
                                        with Horizontal(classes='field'):
                                            yield Label('Confirm new root password', markup=False)
                                            yield Input(password=True, id='mariadb-confirm-password')
                                        yield Static('ON grants root@% ALL PRIVILEGES ON *.* WITH GRANT OPTION. Restrict client sources with your firewall. Local root@localhost unix_socket access is preserved. Password/access changes are excluded from ordinary Restore last.', classes='hint', markup=False)
                                        yield Button('Apply root access / review', id='root-security-mariadb')
                                        yield TextArea('', read_only=True, id='out-mariadb-security', classes='output')
                                if self.root_mode and module == 'storage':
                                    yield Static('Detected ext4 root LVM / irreversible growth', classes='module-state', markup=False)
                                    yield Static('Reading root LV / VG free space...', id='lvm-state', classes='hint', markup=False)
                                    yield Static('Grow only the detected root LV into existing free VG extents. This cannot be undone by Restore last. Test mode does not resize the filesystem. Disk/partition/PV expansion and swap changes are separate operations.', classes='hint', markup=False)
                                    yield Select([('Use all free VG space', 'all'), ('Add a specified GiB increment', 'add')],
                                                 value='all', allow_blank=False, id='lvm-mode')
                                    yield Input('1', placeholder='Additional GiB (integer, not final total)', disabled=True, id='lvm-add-gib')
                                    with Horizontal(classes='buttons'):
                                        yield Button('Preview growth', id='lvm-preview')
                                        yield Button('Review test mode', id='lvm-check')
                                        yield Button('Review & grow', id='lvm-apply', variant='warning')
                                    yield Button('Read LV / VG status', id='lvm-status')
                                    yield TextArea('', read_only=True, id='out-lvm', classes='output')
                                    yield Static('Swap file / independent transaction', classes='module-state', markup=False)
                                    yield Static('Reading actual existing swap file...', id='swap-state', classes='hint', markup=False)
                                    yield Static('Initial fields show the existing file and its actual size. 16384 MiB is only a loadable recommendation. Active now and persistent at boot are independent. Changes to active swap may require swapoff; memory/disk checks run first. LVM is separate.', classes='hint', markup=False)
                                    yield Select([('/swap.img', '/swap.img'), ('/swapfile', '/swapfile')], value='/swap.img', allow_blank=False, id='swap-path')
                                    yield Input('', placeholder='Target MiB, 16..1048576; not changed until Apply', id='swap-size-mib')
                                    with Horizontal(classes='field'):
                                        yield Label('Active now', markup=False)
                                        yield Switch(False, id='swap-active')
                                    with Horizontal(classes='field'):
                                        yield Label('Persistent at boot', markup=False)
                                        yield Switch(False, id='swap-persistent')
                                    with Horizontal(classes='buttons'):
                                        yield Button('Load recommended', id='swap-recommended')
                                        yield Button('Save recipe', id='swap-save')
                                        yield Button('Load saved', id='swap-load')
                                    with Horizontal(classes='buttons'):
                                        yield Button('Review & apply swap', id='swap-apply', variant='warning')
                                        yield Button('Read swap status', id='swap-status')
                                    yield Input('', placeholder='Recovery transaction ID from failed operation', max_length=64, id='swap-recovery-id')
                                    yield Button('Review recovery', id='swap-recover')
                                    yield TextArea('', read_only=True, id='out-swap', classes='output')
                                elif self.root_mode and module == 'startup':
                                    yield Static('Reading managed startup metadata...', id='startup-live', classes='module-state', markup=False)
                                    yield Static('Bash script for the managed systemd oneshot. Save keeps a private draft. Apply changes configuration and boot enablement; it does not execute the script. Run once now reviews the currently applied script separately. rc.local is observation only.', classes='hint', markup=False)
                                    yield TextArea('', soft_wrap=True, show_line_numbers=True, id='startup-script', classes='output')
                                    yield Checkbox('Enable at boot', False, id='startup-enabled')
                                    yield Checkbox('Wait for network-online.target (not an Internet health check)', True, id='startup-wait-network')
                                    with Horizontal(classes='field'):
                                        yield Label('Execution timeout seconds (1..600)', markup=False)
                                        yield Input('120', max_length=3, id='startup-timeout')
                                    with Horizontal(classes='buttons'):
                                        yield Button('Load current', id='startup-current')
                                        yield Button('Load template', id='startup-template')
                                        yield Button('Load saved', id='startup-saved')
                                    with Horizontal(classes='buttons'):
                                        yield Button('Save draft', id='startup-save')
                                        yield Button('Review & apply', id='startup-apply', variant='warning')
                                    with Horizontal(classes='buttons'):
                                        yield Button('Run once now', id='startup-run', variant='warning')
                                        yield Button('Restore / recover', id='startup-restore')
                                        yield Button('Refresh status', id='startup-refresh')
                                elif not fields:
                                    yield Static('Observation only in this milestone. Storage / startup actions are not implemented.', classes='hint', markup=False)
                                if not (self.root_mode and module == 'startup'):
                                    with Horizontal(classes='buttons'):
                                        yield Button('Preview draft', id=f'preview-{module}')
                                        if module == 'apache':
                                            yield Button('Sensitive files', id='apache-candidate')
                                yield TextArea('', read_only=True, id=f'out-{module}', classes='output')
                    if self.root_mode:
                        with TabPane('System updates', id='tab-apt'):
                            with VerticalScroll():
                                yield Static('Explicit APT maintenance. Update refreshes package indexes; upgrade reviews package versions before changing packages. No package removals, autoremove, distribution upgrade or automatic reboot. Modified conffiles are preserved. Package scripts may restart services; configuration Restore last cannot undo package upgrades.', classes='hint', markup=False)
                                with Horizontal(classes='buttons'):
                                    yield Button('APT update', id='root-update-apt')
                                    yield Button('APT upgrade', id='root-upgrade-apt')
                                yield TextArea('', read_only=True, id='out-apt', classes='output')
                    if self.root_mode:
                        with TabPane('Install packages', id='tab-packages'):
                            with VerticalScroll():
                                yield Static('Reading package catalog...', id='packages-state', classes='module-state', markup=False)
                                yield Static('Select missing packages to install together. Installed versions are view only; use System updates for upgrades. PHP builtin/provider labels describe distro packaging; the separate FeatherMountain SQLite build is not an official APT package.', classes='hint', markup=False)
                                with Horizontal(classes='buttons'):
                                    yield Button('Refresh catalog', id='packages-refresh')
                                    yield Button('Preview selected', id='packages-preview')
                                    yield Button('Install selected', id='packages-install', variant='warning')
                                yield Input('', placeholder='Optional APT search: up to four plain words', max_length=80, id='packages-query')
                                yield Button('Search packages', id='packages-search')
                                with Vertical(id='packages-list'):
                                    pass
                                yield Static('FeatherMountain SQLite / SQL load_extension', classes='module-state', markup=False)
                                yield Static('Reading custom module status...', id='feather-state', classes='hint', markup=False)
                                yield Static('Build and activate the custom SQLite module separately from official php-sqlite3. Select the CLI/FPM scopes; unselected scopes keep their current activation. Review the authenticated source, build dependencies and SQL.so capability verification.', classes='hint', markup=False)
                                with Horizontal(classes='field'):
                                    yield Label('Activate custom module for PHP CLI', markup=False)
                                    yield Switch(True, id='feather-cli')
                                with Horizontal(classes='field'):
                                    yield Label('Activate custom module for PHP FPM', markup=False)
                                    yield Switch(True, id='feather-fpm')
                                with Horizontal(classes='buttons'):
                                    yield Button('Build & activate', id='feather-apply', variant='warning')
                                    yield Button('Restore stock', id='feather-restore')
                                    yield Button('Read build status', id='feather-status')
                                yield TextArea('', read_only=True, id='out-php-feather', classes='output')
                                yield TextArea('', read_only=True, id='out-packages', classes='output')
                    with TabPane('Network', id='tab-network'):
                        with VerticalScroll():
                            yield Static('Reading Ethernet / Netplan status...', id='network-live', classes='module-state', markup=False)
                            yield Static('Main Ethernet interface only. DHCP with blank DNS inherits DHCP DNS; manual DNS replaces it. Static IPv4 uses CIDR. Save keeps a private draft. Apply trials the configuration with an automatic rollback deadline; persistence requires a NEW SSH connection to the target IP. The current console cannot confirm it.', classes='hint', markup=False)
                            with Horizontal(classes='field'):
                                yield Label('Ethernet interface')
                                yield Select([], id='network-interface', disabled=not self.network_root)
                            with Horizontal(classes='field'):
                                yield Label('IPv4 mode')
                                yield Select([('DHCP', 'dhcp'), ('Static IPv4', 'static')], value='dhcp', allow_blank=False, id='network-mode', disabled=not self.network_root)
                            for identifier, label, placeholder in (
                                    ('address', 'Static IPv4 / CIDR', '192.168.137.24/24'),
                                    ('gateway', 'IPv4 gateway (optional)', '192.168.137.1'),
                                    ('dns1', 'DNS 1 (optional)', '1.1.1.1'),
                                    ('dns2', 'DNS 2 (optional)', '8.8.8.8'),
                                    ('dns3', 'DNS 3 (optional)', 'IPv4 or IPv6 resolver')):
                                with Horizontal(classes='field'):
                                    yield Label(label)
                                    yield Input('', placeholder=placeholder, max_length=64, id='network-'+identifier, disabled=not self.network_root)
                            with Horizontal(classes='buttons'):
                                yield Button('Read network status', id='network-status')
                                if self.network_root:
                                    yield Button('Load current', id='network-current', disabled=True)
                                    yield Button('Load saved draft', id='network-saved', disabled=True)
                            if self.network_root:
                                with Horizontal(classes='buttons'):
                                    yield Button('Save draft', id='network-save', disabled=True)
                                    yield Button('Review & apply', id='network-apply', variant='warning', disabled=True)
                                    yield Button('Recover pending', id='network-recover', disabled=True)
                            yield TextArea('', read_only=True, id='out-network', classes='output')
                    with TabPane('Firewall', id='tab-firewall'):
                        with VerticalScroll():
                            if self.root_mode:
                                yield Static('Reading live firewall status...', id='fw-live', classes='module-state', markup=False)
                                yield Static('IPv4 + IPv6 INPUT default DROP: disabled or unlisted services block new connections. Loopback, established/related, verified SSH rescue and required ICMP/DHCP are exceptions. This does not start services or change DB accounts, NAT, VPN or forwarding.', classes='hint', markup=False)
                                yield Select([(item['id'], item['id']) for item in self.fw_draft['services']] + [('New custom service', '__new__')],
                                             value='http', allow_blank=False, id='fw-service')
                                yield Input('http', placeholder='Unique custom service ID', disabled=True, id='fw-rule-id')
                            else:
                                yield Static('IPv4 INPUT offline candidate ONLY. No apply, IPv6, VPN, PPP or NAT. SSH rescue uses the template placeholder: edit/verify outside this milestone before any future deployment.', classes='hint', markup=False)
                                yield Select([('HTTP / HTTPS 80,443', '80,443'), ('SSH 22', '22'), ('MariaDB 3306', '3306'),
                                              ('FTP 21', '21'), ('FTP PASV example', '60000-60100'), ('SMB 445', '445'), ('Custom', '')],
                                             value='80,443', allow_blank=False, id='fw-preset')
                            yield Input('80,443', placeholder='Ports: 80,443 or 60000-60100', id='fw-ports')
                            yield Select([('TCP', 'tcp'), ('UDP', 'udp')], value='tcp', allow_blank=False, id='fw-protocol')
                            yield Select([('Specified IPv4 / IPv6 / CIDR' if self.root_mode else 'Specified IPv4 / CIDR', 'restricted'), ('Public for ONLY selected ports', 'public')],
                                         value='public' if self.root_mode else 'restricted', allow_blank=False, id='fw-source-mode')
                            yield Input('', placeholder='IP/CIDR, comma separated (IPv4/IPv6)' if self.root_mode else 'Required IPv4/CIDR, comma separated. No real data saved.', id='fw-sources')
                            if self.root_mode:
                                yield Static('Restricted accepts multiple IP/CIDR: 0.0.0.0/0 = all IPv4; ::/0 = all IPv6. Public = both families for only this service. An IPv4-only allow list blocks new IPv6 connections. SSH rescue uses the verified actual peer and SSH port, independently of this service list.', classes='hint', markup=False)
                                yield Static('多個來源用半形逗號，例如 192.168.0.0/16,111.246.98.67/32。逐服務 Update service draft → Save recipe → 最後一次 Review & apply。Apply 會 replace 整份服務規則；預設拒絕新入站，再允許所選服務，不必先套用 deny。保留目前 SSH 救援與 120 秒回復，須由新 SSH 確認。', id='fw-help', classes='hint', markup=False)
                                yield Static('Saved recipe not loaded：若曾儲存規則，請先 Load saved 再修改；啟動畫面使用全部服務停用的草稿。', id='fw-draft-state', classes='hint', markup=False)
                            with Horizontal(classes='field'):
                                yield Label('Enable this draft rule', markup=False)
                                yield Switch(False, id='fw-enabled')
                            yield Static('FTP: 20 is active-mode server SOURCE, not an automatic inbound opening. PASV range must match vsftpd and sources. NetBIOS 137/138 UDP + 139 TCP is optional legacy use, not opened with 445.', classes='hint', markup=False)
                            if self.root_mode:
                                yield Static('All preset services start disabled.', id='fw-service-summary', classes='hint', markup=False)
                                with Horizontal(classes='buttons'):
                                    yield Button('Update service draft', id='fw-update')
                                    yield Button('Preview effective rules', id='fw-preview')
                                with Horizontal(classes='buttons'):
                                    yield Button('Save recipe', id='fw-save')
                                    yield Button('Load saved', id='fw-load')
                                    yield Button('Review & apply', id='fw-apply', variant='warning')
                                with Horizontal(classes='buttons'):
                                    yield Button('Read live status', id='fw-status')
                                    yield Button('Deny services draft', id='fw-reset')
                            else:
                                with Horizontal(classes='buttons'):
                                    yield Button('Add draft rule', id='fw-add')
                                    yield Button('Preview rules', id='fw-preview')
                                    yield Button('Reset draft', id='fw-reset')
                            yield TextArea('', read_only=True, id='fw-output', classes='output')
                            if self.root_mode:
                                yield Static('BanIP blacklist / IPv4 only', classes='module-state', markup=False)
                                yield Static('Reading live BanIP status...', id='banip-live', classes='hint', markup=False)
                                yield Static('Fixed 3wa download; review before installing required packages and running once. IPv6 is not covered. No cron or boot task; existing service policy is not enabled automatically.', classes='hint', markup=False)
                                with Horizontal(classes='buttons'):
                                    yield Button('Download & run once', id='banip-update', variant='warning')
                                    yield Button('Read BanIP status', id='banip-status')
                                yield TextArea('', read_only=True, id='out-banip', classes='output')
                    with TabPane('Software update', id='tab-update'):
                        with VerticalScroll():
                            yield Static('Reading local release identity...', id='update-state', classes='module-state', markup=False)
                            yield Static('Program versions only. Check contacts the fixed GitHub release source; ordinary startup stays local. Apply and rollback preserve host settings, backups and modules. A successful switch closes this console; reopen run.sh to use the selected version.', classes='hint', markup=False)
                            yield Input('', placeholder='Optional tag, e.g. v2026.10.25.001; blank selects latest stable', max_length=64, id='release-tag')
                            with Horizontal(classes='buttons'):
                                yield Button('Check releases', id='update-check')
                                yield Button('Read local status', id='update-status')
                            if self.update_root:
                                with Horizontal(classes='buttons'):
                                    yield Button('Review & update', id='update-apply', variant='warning', disabled=True)
                                    yield Button('Rollback / recover', id='update-rollback', disabled=True)
                            yield TextArea('', read_only=True, id='out-update', classes='output')
                    with TabPane('SQLite', id='tab-sqlite'):
                        with VerticalScroll():
                            yield Static('CLI 記憶體測試：PDO 讀寫與兩個 driver 的 RTree 正反例。未指定模組時 SpatiaLite 為 UNVERIFIED。CLI 通過不代表 FPM；這裡不改 PHP ini 或開啟載入開關。', classes='hint', markup=False)
                            yield Button('Run PDO + RTree', id='sqlite-run')
                            if self.root_mode:
                                yield Static('以下兩項會載入你指定的可信本機原生模組。先安裝官方 libsqlite3-mod-spatialite，並完成羽山版 PDO 與 SQLite3 extension_dir 設定。快速測試含 SQL load_extension 與空間查詢；完整驗收為兩個 driver 各 30,000 筆。', classes='hint', markup=False)
                                yield Input('/var/www/sqlite3_ext/mod_spatialite.so', placeholder='Trusted absolute mod_spatialite.so path', id='sqlite-module')
                                with Horizontal(classes='buttons'):
                                    yield Button('Test SQL + spatial', id='sqlite-spatial')
                                    yield Button('Full 30k / driver', id='sqlite-acceptance')
                            yield TextArea('', read_only=True, id='sqlite-output', classes='output')
            yield Static(render_mascot(self.ascii_mode or self.plain), id='mascot', markup=False)
        if self.root_mode:
            yield Static('', id='operation-progress', markup=False)
            yield RichLog(id='operation-log', markup=False, highlight=False, max_lines=100, auto_scroll=True)
        yield Footer()

    async def on_mount(self):
        if self.plain:
            self.styles.background = 'black'
            self.styles.color = 'white'
        self.adapt_layout()
        await self.action_refresh()
        if self.root_mode:
            self.set_interval(1, self._fw_tick)
            self.set_interval(1, self._banip_tick)
            self.set_interval(1, self._operation_tick)
        if self.network_root:
            self.set_interval(1, self._network_tick)

    def adapt_layout(self, size=None):
        try:
            mascot = self.query_one('#mascot')
        except NoMatches:
            return
        size = size or self.size
        mascot.display = size.width >= 120 and size.height >= 30

    def on_resize(self, event):
        # The public handler runs before App updates its cached size.
        self.adapt_layout(event.size)

    async def action_refresh(self):
        if self.root_busy:
            self.notify('A reviewed operation is in progress. Wait for its result.')
            return
        await self._refresh_observations()

    async def _refresh_observations(self, update_module=None):
        self.query_one('#overview', Static).update('Reading bounded local probes...')
        self.snapshot = await asyncio.to_thread(collect)
        lines = [f'Platform: {self.snapshot["platform"]} | root={self.snapshot["root"]}',
                 f'Release: {self.snapshot.get("release", "UNAVAILABLE")} | supported target (not validated): {self.snapshot.get("supported_target", False)}',
                 self.snapshot.get('platform_warning', 'Linux runtime / platform validation has not been completed.'),
                 f'Read at: {self.snapshot["collected_at"]}', 'No runtime health verdicts are inferred from configuration.']
        for module, item in self.snapshot['modules'].items():
            lines.append(f'{LABELS[module]}: [{item["status"]}] {item["detail"]}')
            if module in FIELDS:
                configured = '\n'.join(f'{key}: {value}' for key, value in item['configured'].items()) or 'UNKNOWN'
                self.query_one(f'#state-{module}', Static).update(f'[{item["status"]}] {item["detail"]}\nObserved selected config:\n{configured}\nEffective runtime: {item["runtime"]}')
        self.query_one('#overview', Static).update('\n\n'.join(lines))
        await self._refresh_network_status()
        await self._refresh_release_status()
        if self.root_mode:
            for module in ROOT_MODULES:
                try:
                    capability = await asyncio.to_thread(self.root_manager.capabilities, module)
                except Exception as error:
                    capability = {'supported': False, 'detail': str(error), 'configured': {}}
                self.root_capabilities[module] = capability
                configured = capability.get('configured') or {}
                detail = json.dumps({key: value for key, value in capability.items() if key != 'configured'},
                                    ensure_ascii=True, default=str)
                self.query_one(f'#root-cap-{module}', Static).update(detail)
                selected = '\n'.join(f'{key}: {value}' for key, value in configured.items()) or 'UNKNOWN'
                self.query_one(f'#state-{module}', Static).update(
                    f'Root recipe observation (not whole-service health):\n{selected}')
                if module not in self._root_initialized or module == update_module:
                    self._set_form_values(module, configured)
                    self._root_initialized.add(module)
            try:
                status = await asyncio.to_thread(self.root_manager.mariadb_security_status)
                self.mariadb_security = {key: status[key] for key in (
                    'installed', 'active', 'supported', 'detail', 'remote_enabled', 'root_remote_exists',
                    'root_remote_locked', 'bind_address', 'local_socket_preserved', 'local_password_set',
                    'password_required_for_remote_on') if key in status}
            except Exception:
                self.mariadb_security = {'supported': False, 'detail': 'Live MariaDB root access status is unavailable.'}
            self.query_one('#mariadb-security-status', Static).update(json.dumps(self.mariadb_security, ensure_ascii=True))
            await self._refresh_firewall_status()
            await self._refresh_banip_status()
            await self._refresh_lvm_status()
            await self._refresh_swap_status()
            await self._refresh_package_catalog()
            await self._refresh_php_feather_status()
            await self._refresh_startup_status()
            if not self.root_busy:
                self._update_root_buttons()

    def _set_form_values(self, module, values):
        for key, value in values.items():
            if key in self.field_ids[module] and value is not None:
                self.query_one('#' + self.field_ids[module][key], Input).value = str(value)

    def _update_root_buttons(self):
        for module, capability in self.root_capabilities.items():
            # Recompute controls after pending recovery; busy snapshots may contain the old disabled state.
            for operation in ('recommended', 'saved', 'apply-recommended', 'apply', 'restore'):
                self.query_one(f'#root-{operation}-{module}', Button).disabled = False
            self.query_one(f'#root-install-{module}', Button).disabled = not bool(capability.get('packages'))
            no_unit = not capability.get('unit')
            self.query_one(f'#root-active-{module}', Select).disabled = no_unit
            self.query_one(f'#root-enabled-{module}', Select).disabled = no_unit
        for identifier in ('root-update-apt', 'root-upgrade-apt'):
            self.query_one('#'+identifier, Button).disabled = False
        security_ready = (self.mariadb_security.get('supported') and self.mariadb_security.get('installed')
                          and self.mariadb_security.get('active') in (True, 'active'))
        for selector in ('#root-security-mariadb', '#mariadb-remote', '#mariadb-new-password', '#mariadb-confirm-password'):
            self.query_one(selector).disabled = not security_ready
        self._fw_update_controls()
        self._lvm_update_controls()
        self._swap_update_controls()
        self._packages_update_controls()
        self._php_feather_update_controls()
        self._startup_update_controls()
        self._release_update_controls()
        self._network_update_controls()
        self.query_one('#banip-update', Button).disabled = self._mutation_pending() or not self.banip_state.get('supported')
        if self._mutation_pending():
            for button in self.query(Button):
                if (button.id or '').startswith('root-'):
                    button.disabled = True

    def _network_pending(self):
        return bool(self.firewall_state.get('pending') or self.banip_state.get('pending') or self.network_state.get('pending'))

    def _mutation_pending(self):
        return self._network_pending() or bool(self.startup_state.get('pending') or self.release_state.get('pending'))

    def _set_network_config(self, config):
        interface = config.get('interface')
        if isinstance(interface, str) and interface not in self._network_interfaces:
            self._network_interfaces.append(interface)
            self.query_one('#network-interface', Select).set_options([(name, name) for name in self._network_interfaces])
        if interface in self._network_interfaces:
            self.query_one('#network-interface', Select).value = interface
        self.query_one('#network-mode', Select).value = config.get('mode', 'dhcp')
        self.query_one('#network-address', Input).value = config.get('address', '')
        self.query_one('#network-gateway', Input).value = config.get('gateway', '')
        dns = config.get('dns', [])
        for index in range(3):
            self.query_one('#network-dns'+str(index+1), Input).value = dns[index] if index < len(dns) else ''
        self._network_initialized = True

    def _network_config(self):
        interface = self.query_one('#network-interface', Select).value
        mode = self.query_one('#network-mode', Select).value
        return {'interface':interface if isinstance(interface, str) else '', 'mode':mode,
                'address':self.query_one('#network-address', Input).value.strip() if mode == 'static' else '',
                'gateway':self.query_one('#network-gateway', Input).value.strip() if mode == 'static' else '',
                'dns':[self.query_one('#network-dns'+str(index), Input).value.strip() for index in range(1, 4)
                       if self.query_one('#network-dns'+str(index), Input).value.strip()]}

    async def _refresh_network_status(self):
        try:
            if self.network_manager is not None:
                self.network_state = await asyncio.to_thread(self.network_manager.status)
        except Exception:
            self.network_state = {'supported':False, 'pending':self.network_state.get('pending'),
                                  'detail':'Network status cannot be verified; preserve the pending recovery instructions.'}
        self.query_one('#network-live', Static).update(json.dumps(network_public(self.network_state), ensure_ascii=True, default=str))
        interfaces = self.network_state.get('interfaces') or []
        if interfaces and interfaces != self._network_interfaces:
            selected = self.query_one('#network-interface', Select).value
            self._network_interfaces = list(interfaces)
            self.query_one('#network-interface', Select).set_options([(name, name) for name in interfaces])
            if selected in interfaces:
                self.query_one('#network-interface', Select).value = selected
        if not self._network_initialized and self.network_state.get('config'):
            self._set_network_config(self.network_state['config'])
        if not self.root_busy:
            self._network_update_controls()

    async def _network_tick(self):
        if self.root_busy or not self.network_state.get('pending'):
            return
        await self._refresh_network_status()
        if self.root_mode:
            self._update_root_buttons()
        else:
            self._release_update_controls()

    def _network_update_controls(self):
        self.query_one('#network-status', Button).disabled = self.root_busy or self.network_manager is None
        ready = bool(self.network_root and self.network_manager is not None and self.network_state.get('supported'))
        editable = ready and not self.root_busy and not self._mutation_pending()
        for identifier in ('interface', 'mode', 'dns1', 'dns2', 'dns3'):
            self.query_one('#network-'+identifier).disabled = not editable
        static = self.query_one('#network-mode', Select).value == 'static'
        for identifier in ('address', 'gateway'):
            self.query_one('#network-'+identifier).disabled = not editable or not static
        if not self.network_root:
            return
        for identifier in ('current', 'saved'):
            self.query_one('#network-'+identifier, Button).disabled = self.root_busy or not ready or (identifier == 'saved' and not self.network_state.get('saved'))
        for identifier in ('save', 'apply'):
            self.query_one('#network-'+identifier, Button).disabled = not editable
        foreign = bool(self.firewall_state.get('pending') or self.banip_state.get('pending') or self.startup_state.get('pending') or self.release_state.get('pending'))
        self.query_one('#network-recover', Button).disabled = self.root_busy or self.network_manager is None or foreign or not self.network_state.get('pending')

    def _begin_network_operation(self, operation):
        if self.root_busy or self.network_manager is None or operation not in {'status', 'current', 'saved', 'save', 'apply', 'recover'}:
            return
        if operation != 'status' and not self.network_root:
            self._root_result('network', {'status':'DENIED', 'code':'ROOT_REQUIRED', 'message':'Network changes require root on Ubuntu 22.04, 24.04 or 26.04.'})
            return
        if operation in {'save', 'apply'} and self._mutation_pending():
            return
        if operation == 'recover' and (self.firewall_state.get('pending') or self.banip_state.get('pending') or self.startup_state.get('pending') or self.release_state.get('pending')):
            return
        config = self._network_config() if operation in {'save', 'apply'} else None
        self._start_root_busy()
        self._root_task = asyncio.create_task(self._prepare_network_operation(operation, config))

    async def _prepare_network_operation(self, operation, config):
        try:
            if operation in {'status', 'current'}:
                await self._refresh_network_status()
                if operation == 'current' and self.network_state.get('config'):
                    self._set_network_config(self.network_state['config'])
                self._root_result('network', self.network_state)
            elif operation == 'saved':
                saved = await asyncio.to_thread(self.network_manager.load_saved)
                if saved:
                    self._set_network_config(saved)
                self._root_result('network', {'status':'DRAFT_LOADED' if saved else 'NO_SAVED_CONFIG'})
            elif operation == 'save':
                self._root_result('network', await asyncio.to_thread(self.network_manager.save, config))
                await self._refresh_network_status()
            else:
                plan = await asyncio.to_thread(self.network_manager.plan, config) if operation == 'apply' else {'module':'network', 'operation':'recover', 'pending':self.network_state.get('pending')}
                detail = ('Trial network change: open a NEW SSH connection to the target IP and run its confirmation command before the rollback timeout. '
                          'This console cannot confirm. Unconfirmed changes roll back automatically. Recover restores the network settings from before the pending trial.\n\n'
                          + json.dumps(network_public(plan), ensure_ascii=True, indent=2, default=str))
                self.push_screen(RootReview('Network / '+operation+': review target and rollback', detail),
                                 lambda confirmed: self._network_review_completed(confirmed, operation, plan))
                return
        except Exception as error:
            self._network_error(error)
        self._finish_root_operation()

    def _network_error(self, error):
        code = getattr(error, 'code', 'NETWORK_FAILED')
        if not isinstance(code, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', code):
            code = 'NETWORK_FAILED'
        self._root_result('network', {'status':'FAILED', 'code':code, 'message':'Network operation failed. Read network status for pending recovery instructions.'})

    def _network_review_completed(self, confirmed, operation, plan):
        if not confirmed:
            self._root_result('network', {'status':'CANCELLED', 'message':'No network operation was performed.'})
            self._finish_root_operation()
            return
        self._root_task = asyncio.create_task(self._execute_network_operation(operation, plan))

    async def _execute_network_operation(self, operation, plan):
        try:
            result = await asyncio.to_thread(self.network_manager.apply, plan) if operation == 'apply' else await asyncio.to_thread(
                self.network_manager.recover, (plan.get('pending') or {}).get('transaction_id'))
            self._root_result('network', result)
            await self._refresh_network_status()
        except Exception as error:
            self._network_error(error)
            await self._refresh_network_status()
        finally:
            self._finish_root_operation()

    async def _refresh_release_status(self):
        try:
            if self.release_manager is not None:
                self.release_state = await asyncio.to_thread(self.release_manager.status)
        except Exception:
            self.release_state = {'supported':False, 'managed':False, 'pending':self.release_state.get('pending'),
                                  'detail':'Local release state cannot be verified; program switching is disabled.'}
        self.query_one('#update-state', Static).update(json.dumps(release_public(self.release_state), ensure_ascii=True, default=str))
        if not self.root_busy:
            self._release_update_controls()

    def _release_update_controls(self):
        self.query_one('#update-check', Button).disabled = self.root_busy or self.release_manager is None
        self.query_one('#update-status', Button).disabled = self.root_busy
        if not self.update_root:
            return
        ready = bool(self.update_root and self.release_manager is not None and self.release_state.get('supported')
                     and self.release_state.get('managed'))
        foreign = self._network_pending() or bool(self.startup_state.get('pending'))
        own_pending = self.release_state.get('pending')
        self.query_one('#update-apply', Button).disabled = self.root_busy or not ready or foreign or bool(own_pending)
        recoverable = bool(isinstance(own_pending, dict) and own_pending.get('recoverable'))
        self.query_one('#update-rollback', Button).disabled = self.root_busy or not ready or foreign or not (
            recoverable if own_pending else self.release_state.get('previous'))

    def _begin_release_operation(self, operation):
        if self.root_busy or self.release_manager is None or operation not in {'status', 'check', 'apply', 'rollback'}:
            return
        if operation in {'apply', 'rollback'}:
            if not self.update_root:
                self._root_result('update', {'status':'DENIED', 'code':'ROOT_REQUIRED', 'message':'Program switching requires root on Ubuntu 22.04, 24.04 or 26.04.'})
                return
            if self._network_pending() or self.startup_state.get('pending') or (operation == 'apply' and self.release_state.get('pending')):
                return
        tag = self.query_one('#release-tag', Input).value.strip() or None
        if operation in {'check', 'apply'} and tag is not None and not valid_release_tag(tag):
            self._root_result('update', {'status':'ERROR', 'code':'INVALID_ARGUMENT', 'message':'Use vYYYY.MM.DD.NNN with a real date and sequence 001..999, optionally followed by -rc.N.'})
            return
        self._start_root_busy()
        self._root_task = asyncio.create_task(self._prepare_release_operation(operation, tag))

    async def _prepare_release_operation(self, operation, tag):
        try:
            if operation in {'status', 'check'}:
                if operation == 'status':
                    await self._refresh_release_status()
                    result = self.release_state
                else:
                    result = await asyncio.to_thread(self.release_manager.check, tag)
                self._root_result('update', release_public(result))
                self._finish_root_operation()
                return
            plan = await asyncio.to_thread(self.release_manager.rollback_plan) if operation == 'rollback' else await asyncio.to_thread(self.release_manager.plan, tag)
            detail = ('Switch program version and its private venv. Host settings and business data remain as they are.\n'
                      'The console closes after a successful switch; reopen run.sh.\n\n'
                      + json.dumps(release_public(plan), ensure_ascii=True, indent=2, default=str))
            self.push_screen(RootReview('Software update / '+operation+': review program version switch', detail),
                             lambda confirmed: self._release_review_completed(confirmed, operation, plan))
        except Exception as error:
            self._release_error(error)
            self._finish_root_operation()

    def _release_error(self, error):
        code = getattr(error, 'code', 'UPDATE_FAILED')
        if not isinstance(code, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', code):
            code = 'UPDATE_FAILED'
        self._root_result('update', {'status':'FAILED', 'code':code,
                                    'message':'Program switch failed. Inspect local update status; staging details are not shown here.'})

    def _release_review_completed(self, confirmed, operation, plan):
        if not confirmed:
            self._root_result('update', {'status':'CANCELLED', 'message':'No program version was switched.'})
            self._finish_root_operation()
            return
        self._root_task = asyncio.create_task(self._execute_release_operation(operation, plan))

    async def _execute_release_operation(self, operation, plan):
        result = None
        try:
            method = self.release_manager.rollback if operation == 'rollback' else self.release_manager.apply
            result = await asyncio.to_thread(method, plan)
            self._root_result('update', release_public(result))
            await self._refresh_release_status()
        except Exception as error:
            self._release_error(error)
            await self._refresh_release_status()
        finally:
            self._finish_root_operation()
        if result and result.get('status') in {'UPDATED', 'UPDATE_ROLLED_BACK'}:
            self.exit(release_public(dict(result, restart_required=True)),
                      message='Program version switched. Reopen /opt/3waServerSetting/run.sh to continue.')

    def _selected_package_names(self):
        names = set()
        for box in self.query('#packages-list Checkbox'):
            item = self.package_item_records.get(box.id, {})
            if box.value and item.get('installed') is False and item.get('candidate'):
                names.update(item.get('packages', []))
        return sorted(names)

    def _packages_update_controls(self):
        ready = self.package_state.get('supported') and not self._mutation_pending()
        for identifier in ('packages-preview', 'packages-install'):
            self.query_one('#' + identifier, Button).disabled = not ready
        for box in self.query('#packages-list Checkbox'):
            item = self.package_item_records.get(box.id, {})
            box.disabled = bool(self.root_busy or item.get('installed') is not False or
                                not item.get('candidate') or not item.get('packages'))

    async def _render_package_catalog(self):
        selected = set(self._selected_package_names())
        container = self.query_one('#packages-list', Vertical)
        await container.remove_children()
        self.package_item_records = {}
        widgets = []
        groups = self.package_catalog + ([{'label': 'Search results', 'items': self.package_search_items}] if self.package_search_items else [])
        for group in groups:
            widgets.append(Static(group['label'], classes='module-state', markup=False))
            for item in group.get('items', []):
                identifier = 'packages-item-' + str(len(self.package_item_records))
                self.package_item_records[identifier] = item
                installed = item.get('installed')
                label = (f"{item['label']} | " + ('INSTALLED' if installed is True else 'NOT INSTALLED' if installed is False else 'UNVERIFIED') +
                         f" | current: {item.get('current') or '-'} | candidate: {item.get('candidate') or '-'}")
                detail = ' / '.join(str(item[key]) for key in ('provided_by', 'component_status', 'detail') if item.get(key))
                if detail:
                    label += '\n' + detail
                eligible = installed is False and bool(item.get('candidate') and item.get('packages'))
                widgets.append(Checkbox(Text(label), id=identifier, value=eligible and bool(selected.intersection(item['packages'])),
                                        disabled=self.root_busy or not eligible))
        if widgets:
            await container.mount(*widgets)

    async def _refresh_package_catalog(self):
        try:
            if self.package_manager is None:
                raise ValueError('Package backend is unavailable.')
            self.package_catalog = await asyncio.to_thread(self.package_manager.catalog)
            if self.package_search_items:
                rows = await self._package_status_rows([item['id'] for item in self.package_search_items])
                self.package_search_items = [{**item, **rows.get(item['id'], {})} for item in self.package_search_items]
            self.package_state = {'supported': True, 'detail': 'Configured APT catalog. Installed packages cannot be selected for upgrade here.'}
            await self._render_package_catalog()
        except Exception as error:
            self.package_state = {'supported': False, 'detail': str(error)}
        self.query_one('#packages-state', Static).update(json.dumps(self.package_state, ensure_ascii=True))

    async def _package_status_rows(self, names):
        rows = {}
        # Search returns up to 50 candidates; each status request accepts at most 32.
        for index in range(0, len(names), 32):
            status = await asyncio.to_thread(self.package_manager.status, names[index:index + 32])
            rows.update((item['name'], item) for item in status['items'])
        return rows

    def _begin_packages_operation(self, operation):
        if self.root_busy or (self._mutation_pending() and operation in {'preview', 'install'}):
            return
        names = self._selected_package_names()
        if operation in {'preview', 'install'} and not 1 <= len(names) <= 32:
            self._root_result('packages', {'status': 'INVALID_SELECTION', 'message': 'Select 1..32 missing packages.'})
            return
        query = self.query_one('#packages-query', Input).value.strip()
        self._clear_mariadb_secret()
        self._start_root_busy()
        self._root_task = asyncio.create_task(self._prepare_packages_operation(operation, names, query))

    async def _prepare_packages_operation(self, operation, names, query):
        try:
            if self.package_manager is None:
                raise ValueError('Package backend is unavailable.')
            if operation == 'refresh':
                await self._refresh_package_catalog()
            elif operation == 'search':
                hits = await asyncio.to_thread(self.package_manager.search, query)
                rows = await self._package_status_rows([item['name'] for item in hits])
                self.package_search_items = [{**rows.get(item['name'], {}), 'id': item['name'], 'label': item['name'],
                                              'packages': [item['name']], 'detail': item['description']} for item in hits]
                await self._render_package_catalog()
                self._root_result('packages', {'status': 'SEARCHED', 'count': len(hits), 'message': 'Candidates only; nothing selected or installed automatically.'})
            else:
                plan = await asyncio.to_thread(self.package_manager.plan, names)
                detail = self._review_details('packages', operation, plan)
                if operation == 'install':
                    self.push_screen(RootReview('Install selected packages', detail),
                                     lambda confirmed: self._review_completed(confirmed, 'packages', 'install', plan))
                    return
                self.query_one('#out-packages', TextArea).load_text(detail)
        except Exception as error:
            self._root_result('packages', {'status': 'FAILED', 'code': getattr(error, 'code', 'PACKAGES_FAILED'), 'message': str(error)})
        self._finish_root_operation()

    async def _refresh_php_feather_status(self):
        try:
            if self.php_feather_manager is None:
                raise ValueError('FeatherMountain SQLite backend is unavailable.')
            self.php_feather_state = await asyncio.to_thread(self.php_feather_manager.status)
        except Exception as error:
            self.php_feather_state = {'supported': False, 'detail': str(error)}
        self.query_one('#feather-state', Static).update(json.dumps(self.php_feather_state, ensure_ascii=True, default=str))

    def _php_feather_update_controls(self):
        ready = self.php_feather_manager is not None and self.php_feather_state.get('supported') and not self._mutation_pending()
        for identifier in ('feather-cli', 'feather-fpm', 'feather-apply', 'feather-restore'):
            self.query_one('#' + identifier).disabled = not ready

    def _begin_php_feather_operation(self, operation):
        if self.root_busy or (self._mutation_pending() and operation != 'status'):
            return
        cli = self.query_one('#feather-cli', Switch).value
        fpm = self.query_one('#feather-fpm', Switch).value
        if operation == 'apply' and not (cli or fpm):
            self._root_result('php-feather', {'status': 'INVALID_TARGET', 'message': 'Select CLI, FPM or both.'})
            return
        self._clear_mariadb_secret()
        self._start_root_busy()
        self._root_task = asyncio.create_task(self._prepare_php_feather_operation(operation, cli, fpm))

    async def _prepare_php_feather_operation(self, operation, cli, fpm):
        try:
            if self.php_feather_manager is None:
                raise ValueError('FeatherMountain SQLite backend is unavailable.')
            if operation == 'status':
                await self._refresh_php_feather_status()
            else:
                plan = (await asyncio.to_thread(self.php_feather_manager.restore_plan) if operation == 'restore' else
                        await asyncio.to_thread(self.php_feather_manager.plan, cli=cli, fpm=fpm))
                self.push_screen(RootReview('Restore stock PHP SQLite' if operation == 'restore' else 'Build and activate FeatherMountain SQLite',
                                           self._review_details('php-feather', operation, plan)),
                                 lambda confirmed: self._review_completed(confirmed, 'php-feather', operation, plan))
                return
        except Exception as error:
            self._root_result('php-feather', {'status': 'FAILED', 'code': getattr(error, 'code', 'PHP_FEATHER_FAILED'), 'message': str(error)})
        self._finish_root_operation()

    @staticmethod
    def _startup_public(value):
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

    def _set_startup_form(self, config):
        self.query_one('#startup-script', TextArea).load_text(config['script'])
        self.query_one('#startup-enabled', Checkbox).value = config['enabled']
        self.query_one('#startup-wait-network', Checkbox).value = config['wait_network']
        self.query_one('#startup-timeout', Input).value = str(config['timeout_seconds'])
        self._startup_initialized = True

    async def _refresh_startup_status(self):
        try:
            if self.startup_manager is None:
                raise ValueError('Startup backend is unavailable.')
            state = self._startup_public(await asyncio.to_thread(self.startup_manager.status))
            if not state.get('supported') and self.startup_state.get('pending'):
                state['pending'] = self.startup_state['pending']
            self.startup_state = state
            if state.get('supported') and not self._startup_initialized:
                current = await asyncio.to_thread(self.startup_manager.load_current)
                if current is None:
                    from .root_startup import default_config
                    current = default_config()
                self._set_startup_form(current)
        except Exception:
            self.startup_state = {**self.startup_state, 'supported': False,
                                  'detail': 'Startup status is unavailable; pending protection is preserved.'}
        self.query_one('#startup-live', Static).update(json.dumps(self.startup_state, ensure_ascii=True, default=str))
        self.query_one('#state-startup', Static).update('Managed startup configuration and runtime are observed separately; Apply does not execute scripts.')

    def _startup_update_controls(self):
        ready = bool(self.startup_manager is not None and self.startup_state.get('supported'))
        blocked = self._mutation_pending() or not ready
        for identifier in ('startup-script', 'startup-enabled', 'startup-wait-network', 'startup-timeout',
                           'startup-current', 'startup-template', 'startup-saved', 'startup-save', 'startup-apply', 'startup-run'):
            self.query_one('#'+identifier).disabled = blocked
        recoverable = self.startup_manager is not None and bool(self.startup_state.get('supported') or self.startup_state.get('pending'))
        self.query_one('#startup-restore', Button).disabled = self._network_pending() or not recoverable

    def _begin_startup_operation(self, operation):
        if self.root_busy or not self.root_mode:
            return
        if (operation == 'restore' and self._network_pending()) or (operation not in {'restore', 'refresh'} and self._mutation_pending()):
            return
        try:
            config = None
            if operation in {'save', 'apply'}:
                timeout = self.query_one('#startup-timeout', Input).value
                if not timeout.isascii() or not timeout.isdigit() or not 1 <= int(timeout) <= 600:
                    raise ValueError('Timeout must be an integer from 1 to 600 seconds.')
                config = {'schema': 1, 'script': self.query_one('#startup-script', TextArea).text,
                          'enabled': self.query_one('#startup-enabled', Checkbox).value,
                          'wait_network': self.query_one('#startup-wait-network', Checkbox).value,
                          'timeout_seconds': int(timeout)}
            self._clear_mariadb_secret()
            self._start_root_busy()
            self._root_task = asyncio.create_task(self._prepare_startup_operation(operation, config))
        except ValueError as error:
            self._root_result('startup', {'status': 'INVALID_TARGET', 'message': str(error)})

    async def _prepare_startup_operation(self, operation, config):
        try:
            if self.startup_manager is None:
                raise ValueError('Startup backend is unavailable.')
            if operation in {'current', 'template', 'saved'}:
                if operation == 'template':
                    from .root_startup import default_config
                    loaded = default_config()
                else:
                    loaded = await asyncio.to_thread(self.startup_manager.load_current if operation == 'current' else self.startup_manager.load_saved)
                if loaded is not None:
                    self._set_startup_form(loaded)
                self._root_result('startup', {'status': 'DRAFT_LOADED' if loaded else 'NO_SAVED_CONFIG' if operation == 'saved' else 'NO_CURRENT_CONFIG',
                                              'message': 'Editor draft only; no configuration applied or script executed.'})
            elif operation == 'save':
                self._root_result('startup', await asyncio.to_thread(self.startup_manager.save, config))
            elif operation in {'apply', 'run', 'restore'}:
                plan = (await asyncio.to_thread(self.startup_manager.plan, config) if operation == 'apply' else
                        await asyncio.to_thread(self.startup_manager.run_plan if operation == 'run' else self.startup_manager.restore_plan))
                self.push_screen(RootReview('Startup / '+operation+': review one explicit operation', self._review_details('startup', operation, plan)),
                                 lambda confirmed: self._review_completed(confirmed, 'startup', operation, plan))
                return
            await self._refresh_startup_status()
        except Exception as error:
            self._root_result('startup', {'status': 'FAILED', 'code': getattr(error, 'code', 'STARTUP_FAILED'),
                                          'message': 'Startup operation failed; script text and output are not included.'})
        self._finish_root_operation()

    def _set_swap_form(self, config):
        self._swap_editor_path = config['path']
        self.query_one('#swap-path', Select).value = config['path']
        size = config.get('size_mib')
        self.query_one('#swap-size-mib', Input).value = str(size) if size else ''
        self.query_one('#swap-active', Switch).value = bool(config.get('active'))
        self.query_one('#swap-persistent', Switch).value = bool(config.get('persistent'))

    async def _refresh_swap_status(self, force=False, path=None):
        if self.swap_manager is not None:
            try:
                selected = path if path is not None else (self._swap_editor_path if self._swap_initialized else None)
                self.swap_state = await asyncio.to_thread(self.swap_manager.status, selected)
            except Exception:
                self.swap_state = {'supported': False, 'detail': 'Live swap status is unavailable.'}
        self.query_one('#swap-state', Static).update(json.dumps(self.swap_state, indent=2, ensure_ascii=True, default=str))
        if (force or not self._swap_initialized) and self.swap_state.get('path') in {'/swapfile', '/swap.img'}:
            self._set_swap_form(self.swap_state)
            self._swap_initialized = True

    def _swap_update_controls(self):
        pending = self._mutation_pending()
        for selector in ('#swap-path', '#swap-size-mib', '#swap-active', '#swap-persistent', '#swap-recommended',
                         '#swap-save', '#swap-load', '#swap-recovery-id', '#swap-recover'):
            self.query_one(selector).disabled = pending or self.swap_manager is None
        self.query_one('#swap-apply', Button).disabled = pending or not self.swap_state.get('supported')

    def _begin_swap_operation(self, operation):
        if self.root_busy or (self._mutation_pending() and operation != 'status'):
            return
        try:
            if operation == 'recommended':
                self.query_one('#swap-size-mib', Input).value = str(self.swap_state.get('recommended_size_mib') or 16384)
                self._root_result('swap', {'status': 'DRAFT_LOADED', 'message': 'Recommended size only; existing file and active/boot state unchanged.'})
                return
            config = None
            if operation in {'save', 'apply'}:
                value = self.query_one('#swap-size-mib', Input).value
                if not value.isascii() or not value.isdigit() or not 16 <= int(value) <= 1048576:
                    raise ValueError('Swap size must be an integer MiB from 16 to 1048576.')
                config = {'schema': 1, 'path': self.query_one('#swap-path', Select).value, 'size_mib': int(value),
                          'active': self.query_one('#swap-active', Switch).value,
                          'persistent': self.query_one('#swap-persistent', Switch).value}
            identifier = self.query_one('#swap-recovery-id', Input).value.strip()
            if operation == 'recover' and not identifier:
                raise ValueError('Enter the recovery transaction ID reported by the failed operation.')
            self._clear_mariadb_secret()
            self._start_root_busy()
            self._root_task = asyncio.create_task(self._prepare_swap_operation(operation, config, identifier))
        except ValueError as error:
            self._root_result('swap', {'status': 'INVALID_TARGET', 'message': str(error)})

    async def _prepare_swap_operation(self, operation, config, identifier):
        try:
            if self.swap_manager is None:
                raise ValueError('Swap backend is unavailable.')
            if operation in {'apply', 'recover'}:
                plan = (await asyncio.to_thread(self.swap_manager.plan, config) if operation == 'apply' else
                        {'module': 'swap', 'transaction_id': identifier})
                self.push_screen(RootReview('Swap transaction: ' + operation, self._review_details('swap', operation, plan)),
                                 lambda confirmed: self._review_completed(confirmed, 'swap', operation, plan))
                return
            if operation == 'save':
                self._root_result('swap', await asyncio.to_thread(self.swap_manager.save, config))
            elif operation == 'load':
                saved = await asyncio.to_thread(self.swap_manager.load_saved)
                if saved is not None:
                    self._set_swap_form(saved)
                self._root_result('swap', {'status': 'DRAFT_LOADED' if saved else 'NO_SAVED_RECIPE', 'message': 'No live swap state changed.'})
            await self._refresh_swap_status(force=operation == 'status')
        except Exception as error:
            self._root_result('swap', {'status': 'FAILED', 'code': getattr(error, 'code', 'SWAP_FAILED'), 'message': str(error)})
        self._finish_root_operation()

    async def _refresh_lvm_status(self):
        try:
            self.lvm_state = await asyncio.to_thread(self.lvm_manager.status)
        except Exception:
            self.lvm_state = {'supported': False, 'detail': 'Live root LV / VG status is unavailable.'}
        text = json.dumps(self.lvm_state, indent=2, ensure_ascii=True)
        if self.lvm_state.get('needs_filesystem_growth'):
            text += '\nFilesystem recovery is required. Do not repeat a growth increment; use the reported transaction with --lvm-recover ID --confirm.'
        self.query_one('#lvm-state', Static).update(text)

    def _lvm_update_controls(self):
        ready = bool(self.lvm_state.get('supported') and not self._mutation_pending()
                     and not self.lvm_state.get('needs_filesystem_growth'))
        for selector in ('#lvm-preview', '#lvm-check', '#lvm-apply', '#lvm-mode'):
            self.query_one(selector).disabled = not ready
        self.query_one('#lvm-add-gib', Input).disabled = not ready or self.query_one('#lvm-mode', Select).value != 'add'

    def _begin_lvm_operation(self, operation):
        if self.root_busy or (self._mutation_pending() and operation != 'status'):
            return
        try:
            mode = self.query_one('#lvm-mode', Select).value
            additional = None
            if mode == 'add' and operation != 'status':
                value = self.query_one('#lvm-add-gib', Input).value
                if not value.isascii() or not value.isdigit() or not 1 <= int(value) <= 65536:
                    raise ValueError('Additional GiB must be an integer from 1 to 65536.')
                additional = int(value)
            self._clear_mariadb_secret()
            self._start_root_busy()
            self._root_task = asyncio.create_task(self._prepare_lvm_operation(operation, mode, additional))
        except ValueError as error:
            self._root_result('lvm', {'status': 'INVALID_TARGET', 'message': str(error)})

    async def _prepare_lvm_operation(self, operation, mode, additional):
        try:
            if operation != 'status':
                plan = await asyncio.to_thread(self.lvm_manager.plan, mode=mode, add_gib=additional)
                detail = self._review_details('lvm', operation, plan)
                if operation in {'check', 'apply'}:
                    self.push_screen(RootReview('LVM TEST MODE' if operation == 'check' else 'IRREVERSIBLE root LV growth', detail),
                                     lambda confirmed: self._review_completed(confirmed, 'lvm', operation, plan))
                    return
                self.query_one('#out-lvm', TextArea).load_text(detail)
            await self._refresh_lvm_status()
        except Exception as error:
            self._root_result('lvm', {'status': 'FAILED', 'code': getattr(error, 'code', 'LVM_FAILED'), 'message': str(error)})
        self._finish_root_operation()

    async def _refresh_firewall_status(self):
        if self.firewall_manager is not None:
            try:
                self.firewall_state = await asyncio.to_thread(self.firewall_manager.status)
                self._fw_status_at = time.monotonic()
            except Exception:
                self.firewall_state = {**self.firewall_state, 'supported': False, 'detail': 'Live firewall status unavailable; pending protection is not cancelled.'}
        self._fw_tick()

    def _fw_tick(self):
        state = self.firewall_state
        pending = state.get('pending')
        text = json.dumps(state, ensure_ascii=True, default=str)
        if pending:
            remaining = max(0, int(pending.get('remaining_seconds', 0) - (time.monotonic() - self._fw_status_at)))
            identifier = pending['transaction_id']
            text += (f'\nAWAITING_NEW_SSH: {identifier}; remaining about {remaining}s; deadline {pending.get("deadline_boottime")}.'
                     '\nOpen a NEW SSH connection created after apply, then run:\n'
                     f'/opt/3waServerSetting/run.sh --firewall-confirm {identifier} --confirm'
                     '\nThis UI cannot confirm or cancel the rollback watchdog. Refresh status after timeout; quitting keeps protection active.')
        try:
            self.query_one('#fw-live', Static).update(text)
        except NoMatches:
            # A pending timer may finish its callback while the screen unmounts.
            pass

    def _fw_update_controls(self):
        pending = self._mutation_pending()
        for selector in ('#fw-service', '#fw-ports', '#fw-protocol', '#fw-source-mode', '#fw-sources', '#fw-enabled',
                         '#fw-update', '#fw-save', '#fw-load', '#fw-reset'):
            self.query_one(selector).disabled = pending
        self.query_one('#fw-rule-id', Input).disabled = pending or self.query_one('#fw-service', Select).value != '__new__'
        for selector in ('#fw-apply', '#fw-preview'):
            self.query_one(selector).disabled = pending or not self.firewall_state.get('supported')

    def _fw_show_selected(self, identifier):
        if identifier == '__new__':
            existing = {item['id'] for item in self.fw_draft['services']}
            number = 1
            while f'custom-{number}' in existing:
                number += 1
            item = {'id': f'custom-{number}', 'ports': [], 'protocol': 'tcp', 'sources': [], 'enabled': False}
        else:
            item = next((item for item in self.fw_draft['services'] if item['id'] == identifier), None)
            if item is None:
                return
        self.query_one('#fw-rule-id', Input).value = item['id']
        self.query_one('#fw-ports', Input).value = ','.join(map(str, item['ports']))
        self.query_one('#fw-protocol', Select).value = item['protocol']
        public = item['sources'] == ['public']
        self.query_one('#fw-source-mode', Select).value = 'public' if public else 'restricted'
        self.query_one('#fw-sources', Input).value = '' if public else ','.join(item['sources'])
        self.query_one('#fw-enabled', Switch).value = item['enabled']
        if not self.root_busy:
            self._fw_update_controls()

    def _fw_set_draft(self, config, selected=None):
        from .firewall_rules import normalize
        self.fw_draft = normalize(config)
        options = [(item['id'], item['id']) for item in self.fw_draft['services']]
        select = self.query_one('#fw-service', Select)
        selected = selected or select.value
        select.set_options(options + [('New custom service', '__new__')])
        select.value = selected if selected in {item[1] for item in options} else (options[0][1] if options else '__new__')
        self._fw_show_selected(select.value)
        self.query_one('#fw-service-summary', Static).update('\n'.join(
            f"{item['id']}: {'ALLOW' if item['enabled'] else 'BLOCK new'} {item['protocol']} {item['ports']} from {item['sources']}"
            for item in self.fw_draft['services']))

    def _fw_update_draft(self):
        selected = self.query_one('#fw-service', Select).value
        identifier = self.query_one('#fw-rule-id', Input).value if selected == '__new__' else selected
        sources = (['public'] if self.query_one('#fw-source-mode', Select).value == 'public' else
                   [value.strip() for value in self.query_one('#fw-sources', Input).value.split(',') if value.strip()])
        ports = [int(value.strip()) if value.strip().isascii() and value.strip().isdigit() else value.strip()
                 for value in self.query_one('#fw-ports', Input).value.split(',')]
        item = {'id': identifier, 'protocol': self.query_one('#fw-protocol', Select).value, 'ports': ports,
                'sources': sources, 'enabled': self.query_one('#fw-enabled', Switch).value}
        services = [entry for entry in self.fw_draft['services'] if entry['id'] != selected]
        self._fw_set_draft({'schema': 2, 'services': services + [item]}, selected=identifier)

    def _begin_firewall_operation(self, operation):
        if self.root_busy or (self._mutation_pending() and operation != 'status'):
            return
        try:
            if operation == 'reset':
                from .firewall_rules import default_config
                self._fw_set_draft(default_config())
                self._fw_draft_loaded = True
                self.query_one('#fw-draft-state', Static).update('全部服務封鎖草稿：只改記憶體，尚未套用。套用仍保留目前 SSH 救援；開放所需服務後可一次 Apply。')
                return
            if operation in {'update', 'save', 'preview', 'apply'}:
                self._fw_update_draft()
                if operation == 'update':
                    self._root_result('firewall', {'status': 'DRAFT_UPDATED', 'message': 'Memory only; save and apply are separate.'})
                    return
            config = json.loads(json.dumps(self.fw_draft))
            self._clear_mariadb_secret()
            self._start_root_busy()
            self._root_task = asyncio.create_task(self._prepare_firewall_operation(operation, config))
        except (ValueError, TypeError) as error:
            self._root_result('firewall', {'status': 'INVALID_DRAFT', 'message': str(error)})

    async def _prepare_firewall_operation(self, operation, config):
        try:
            if self.firewall_manager is None:
                raise ValueError('Firewall backend is unavailable.')
            if operation == 'load':
                saved = await asyncio.to_thread(self.firewall_manager.load_saved)
                if saved is not None:
                    self._fw_set_draft(saved)
                    self._fw_draft_loaded = True
                    self.query_one('#fw-draft-state', Static).update('已載入 saved recipe；修改各服務後，最後一次套用整份規則。草稿不代表目前 kernel 規則。')
                self._root_result('firewall', {'status': 'DRAFT_LOADED' if saved else 'NO_SAVED_RECIPE', 'message': 'No live firewall rules changed.'})
            elif operation == 'save':
                self._root_result('firewall', await asyncio.to_thread(self.firewall_manager.save, config))
                self._fw_draft_loaded = True
                self.query_one('#fw-draft-state', Static).update('整份草稿已儲存，尚未套用；Review & apply 會替換整份服務規則。')
            elif operation in {'preview', 'apply'}:
                plan = await asyncio.to_thread(self.firewall_manager.plan, config)
                detail = self._review_details('firewall', 'apply', plan)
                if operation == 'apply':
                    self.push_screen(RootReview('Firewall: apply candidate with independent timed rollback', detail),
                                     lambda confirmed: self._review_completed(confirmed, 'firewall', 'apply', plan))
                    return
                self.query_one('#fw-output', TextArea).load_text(detail)
            await self._refresh_firewall_status()
        except Exception as error:
            self._root_result('firewall', {'status': 'FAILED', 'code': getattr(error, 'code', 'FIREWALL_FAILED'), 'message': str(error)})
        self._finish_root_operation()

    async def _refresh_banip_status(self):
        if self.banip_manager is not None:
            try:
                self.banip_state = await asyncio.to_thread(self.banip_manager.status)
                self._banip_status_at = time.monotonic()
            except Exception:
                self.banip_state = {**self.banip_state, 'supported': False,
                                    'detail': 'Live BanIP status unavailable; pending protection is not cancelled.'}
        self._banip_tick()

    def _banip_tick(self):
        text = json.dumps(self.banip_state, ensure_ascii=True, default=str)
        pending = self.banip_state.get('pending')
        if pending:
            remaining = max(0, int(pending.get('remaining_seconds', 0) - (time.monotonic() - self._banip_status_at)))
            text += (f'\nAWAITING_NEW_SSH: {pending["transaction_id"]}; remaining about {remaining}s; deadline {pending.get("deadline_boottime")}.'
                     '\nOpen a NEW SSH connection created after update, then run:\n'
                     f'/opt/3waServerSetting/run.sh --banip-confirm {pending["transaction_id"]} --confirm'
                     '\nThis UI cannot confirm or cancel the rollback watchdog. Refresh status after timeout; quitting keeps protection active.')
        try:
            self.query_one('#banip-live', Static).update(text)
        except NoMatches:
            pass

    def _begin_banip_operation(self, operation):
        if self.root_busy or (self._mutation_pending() and operation != 'status'):
            return
        self._clear_mariadb_secret()
        self._start_root_busy()
        self._root_task = asyncio.create_task(self._prepare_banip_operation(operation))

    async def _prepare_banip_operation(self, operation):
        try:
            if self.banip_manager is None:
                raise ValueError('BanIP backend is unavailable.')
            if operation == 'update':
                plan = await asyncio.to_thread(self.banip_manager.plan)
                self.push_screen(RootReview('BanIP blacklist: download and run once', self._review_details('banip', 'update', plan)),
                                 lambda confirmed: self._review_completed(confirmed, 'banip', 'update', plan))
                return
            await self._refresh_banip_status()
        except Exception as error:
            self._root_result('banip', {'status': 'FAILED', 'code': getattr(error, 'code', 'BANIP_FAILED'), 'message': str(error)})
        self._finish_root_operation()

    def _clear_mariadb_secret(self):
        self._mariadb_password = None
        for selector in ('#mariadb-new-password', '#mariadb-confirm-password'):
            try:
                self.query_one(selector, Input).clear()
            except NoMatches:
                pass

    def _start_root_busy(self):
        self.root_busy = True
        self._operation_session += 1
        self._operation_started = time.monotonic()
        self._operation_event = None
        self._operation_event_at = None
        self._operation_last_stage = None
        if self.root_mode:
            self.query_one('#operation-log', RichLog).clear()
            self.query_one('#operation-log', RichLog).display = True
            self._operation_tick()
        self._busy_widgets = [(widget, widget.disabled) for widget in self.query('Button, Checkbox, Input, Select, Switch')]
        self._busy_widgets += [(widget, widget.disabled) for widget in self.query(TextArea) if not widget.read_only]
        for widget, _disabled in self._busy_widgets:
            widget.disabled = True

    @staticmethod
    def _output_size(count):
        value = float(count)
        for unit in ('B', 'KiB', 'MiB', 'GiB'):
            if value < 1024 or unit == 'GiB':
                return f'{value:.1f} {unit}'
            value /= 1024

    def _receive_operation_progress(self, session, event):
        if not self.root_busy or session != self._operation_session:
            return
        self._operation_event = event
        self._operation_event_at = time.monotonic()
        stage = (event['phase'], event.get('command'), event['state'])
        if stage != self._operation_last_stage:
            self._operation_last_stage = stage
            elapsed = int(time.monotonic() - self._operation_started)
            self.query_one('#operation-log', RichLog).write(
                Text(f"{elapsed:>4}s  {event['phase']}  {event.get('command') or ''}  {event['state']}"))
        self._operation_tick()

    def _operation_tick(self):
        if not self.root_mode or self._operation_started is None:
            return
        try:
            widget = self.query_one('#operation-progress', Static)
            widget.display = self.root_busy
            if not self.root_busy:
                return
            elapsed = int(time.monotonic() - self._operation_started)
            event = self._operation_event
            text = f'執行中 · 已經過 {elapsed}s · '
            if event is None:
                text += '準備／檢閱設定；尚無命令輸出'
            else:
                since_event = time.monotonic() - self._operation_event_at
                text += f"{event['phase']} / {event.get('command') or '準備'} / {event['state']}"
                text += ' · 診斷輸出量 ' + self._output_size(event['output_bytes'])
                text += f" · 距最近輸出約 {int(event['idle_seconds'] + since_event)}s"
                if 'download_bytes' in event:
                    text += ' · APT 已下載 ' + self._output_size(event['download_bytes'])
            widget.update(text)
        except NoMatches:
            pass

    def _begin_mariadb_security(self):
        if not self.root_mode or self.root_busy or self._mutation_pending():
            return
        password = self.query_one('#mariadb-new-password', Input).value
        confirmation = self.query_one('#mariadb-confirm-password', Input).value
        self._clear_mariadb_secret()
        if password != confirmation or len(password.encode('utf-8')) > 4096 or any(char in password for char in '\r\n\x00'):
            self._root_result('mariadb-security', {'status': 'ERROR', 'message': 'Passwords must match and contain at most 4096 UTF-8 bytes, with no newline or NUL.'})
            return
        self._mariadb_password = password or None
        remote = self.query_one('#mariadb-remote', Select).value
        self._start_root_busy()
        self._root_task = asyncio.create_task(self._prepare_mariadb_security(
            None if remote == 'keep' else remote == 'on', bool(password)))

    async def _prepare_mariadb_security(self, remote, change_password):
        try:
            plan = await asyncio.to_thread(self.root_manager.mariadb_security_plan,
                                           remote_enabled=remote, change_password=change_password)
            self.push_screen(RootReview('MariaDB root access: confirm separate security operation',
                                        self._review_details('mariadb-security', 'security', plan)),
                             lambda confirmed: self._review_completed(confirmed, 'mariadb-security', 'security', plan))
        except Exception as error:
            code = getattr(error, 'code', None)
            self._root_result('mariadb-security', {'status': 'FAILED', 'code': code if code in MARIADB_SECURITY_ERROR_CODES else 'MARIADB_SECURITY_PLAN_FAILED',
                                                  'message': 'MariaDB root access preview failed. Check the live service and sanitized transaction status.'})
            self._finish_root_operation()

    def _begin_root_operation(self, module, operation):
        if not self.root_mode or self.root_busy or self._mutation_pending() or module not in (*ROOT_MODULES, 'apt'):
            return
        self._clear_mariadb_secret()
        values = {key: self.query_one('#' + widget_id, Input).value
                  for key, widget_id in self.field_ids.get(module, {}).items()}
        active = self.query_one(f'#root-active-{module}', Select).value if module != 'apt' else 'keep'
        enabled = self.query_one(f'#root-enabled-{module}', Select).value if module != 'apt' else 'keep'
        targets = (None if active == 'keep' else active == 'start',
                   None if enabled == 'keep' else enabled == 'enable')
        self._start_root_busy()
        self._root_task = asyncio.create_task(self._prepare_root_operation(module, operation, values, targets))

    def _finish_root_operation(self):
        self._clear_mariadb_secret()
        for widget, disabled in self._busy_widgets:
            widget.disabled = disabled
        self._busy_widgets = []
        self.root_busy = False
        self._operation_started = None
        if self.root_mode:
            self.query_one('#operation-progress', Static).display = False
            self.query_one('#operation-log', RichLog).display = False
        if self.root_mode:
            self._update_root_buttons()
        else:
            self._release_update_controls()
            self._network_update_controls()

    def _root_result(self, module, value):
        if module == 'startup':
            value = self._startup_public(value)
        elif module == 'update':
            value = release_public(value)
        elif module == 'network':
            value = network_public(value)
        selector = '#fw-output' if module == 'firewall' else f'#out-{module}'
        self.query_one(selector, TextArea).load_text(json.dumps(value, ensure_ascii=True, indent=2, default=str))

    def _begin_apache_fpm(self, operation):
        if not self.root_mode or self.root_busy or self._mutation_pending():
            return
        self._clear_mariadb_secret()
        self._start_root_busy()
        self._root_task = asyncio.create_task(self._prepare_apache_fpm(operation))

    async def _prepare_apache_fpm(self, operation):
        try:
            if self.apache_fpm_manager is None:
                from .root_apache import ApacheFPMManager
                self.apache_fpm_manager = ApacheFPMManager(self.root_manager)
            if operation == 'status':
                self._root_result('apache-fpm', await asyncio.to_thread(self.apache_fpm_manager.status))
            else:
                method = self.apache_fpm_manager.restore_plan if operation == 'restore' else self.apache_fpm_manager.plan
                plan = await asyncio.to_thread(method)
                detail = ('Apache event + PHP-FPM / ' + operation + '\n\n'
                          '此動作會切換 native module/conf aliases，並重新啟動 Apache；FPM 先通過設定檢查與獨立 socket probe。\n'
                          '發生錯誤時還原本次 aliases 與服務狀態；外部變更會阻止自動還原。HTTP/vhost 應用另行驗證。\n\n'
                          + json.dumps(plan, ensure_ascii=False, indent=2, default=str))
                self.push_screen(RootReview('Apache handler: ' + operation, detail),
                                 lambda confirmed: self._review_completed(confirmed, 'apache-fpm', operation, plan))
                return
        except Exception as error:
            self._root_result('apache-fpm', {'status': 'FAILED', 'code': getattr(error, 'code', 'APACHE_FPM_FAILED'), 'message': str(error)})
        self._finish_root_operation()

    async def _prepare_root_operation(self, module, operation, values, targets):
        try:
            if operation in {'recommended', 'saved'}:
                if operation == 'recommended':
                    loaded = await asyncio.to_thread(self.root_manager.recommended, module)
                else:
                    saved = await asyncio.to_thread(self.root_manager.load_saved, module)
                    loaded = saved.get('values', {}) if saved else {}
                    if saved:
                        self.query_one(f'#root-active-{module}', Select).value = (
                            'keep' if saved.get('service_active') is None else 'start' if saved['service_active'] else 'stop')
                        self.query_one(f'#root-enabled-{module}', Select).value = (
                            'keep' if saved.get('service_enabled') is None else 'enable' if saved['service_enabled'] else 'disable')
                self._set_form_values(module, loaded)
                self._root_result(module, {'status': 'DRAFT_LOADED' if loaded else 'NO_SAVED_RECIPE',
                                          'message': 'Form values only; no host changes.'})
                self._finish_root_operation()
                return
            if module == 'apt':
                plan = await asyncio.to_thread(self.root_manager.apt_plan, operation)
            elif operation == 'install':
                plan = await asyncio.to_thread(self.root_manager.install_plan, module)
            elif operation == 'restore':
                plan = await asyncio.to_thread(self.root_manager.restore_plan, module)
            else:
                if operation == 'apply-recommended':
                    values = await asyncio.to_thread(self.root_manager.recommended, module)
                plan = await asyncio.to_thread(self.root_manager.plan, module, values,
                                               service_active=targets[0], service_enabled=targets[1])
            detail = self._review_details(module, operation, plan)
            label = 'System updates' if module == 'apt' else LABELS[module]
            self.push_screen(RootReview(f'{label} / {operation}: confirm the complete page operation', detail),
                             lambda confirmed: self._review_completed(confirmed, module, operation, plan))
        except Exception as error:
            self._root_result(module, {'status': 'FAILED', 'code': getattr(error, 'code', 'PLAN_FAILED'), 'message': str(error)})
            self._finish_root_operation()

    def _review_details(self, module, operation, plan):
        if module == 'startup':
            common = ['Managed unit: threewa-startup.service',
                      'Script: /opt/3waServerSetting/config/startup/startup.sh; unit: /etc/systemd/system/threewa-startup.service',
                      'Execution identity: root; WorkingDirectory=/root; fixed noninteractive environment.']
            if operation == 'apply':
                config = plan['config']
                return '\n\n'.join(['Configuration and boot enablement only: Apply does not execute the script.', *common,
                                      'Selected flags: '+json.dumps({key:config[key] for key in ('enabled','wait_network','timeout_seconds')}),
                                      'wait_network requests network-online.target, not an Internet connectivity guarantee.',
                                      'Selected Bash script (explicit review only):\n'+config['script'], '\n'.join(plan.get('notes', []))])
            if operation == 'run':
                return '\n\n'.join(['Run the currently applied script once NOW; editor drafts are not applied or run here.', *common,
                                      'Applied script SHA256: '+str(plan['script_sha256']), 'Timeout seconds: '+str(plan['timeout_seconds']),
                                      'Business side effects are not reversible. Failure is not automatically retried.',
                                      'An already active/exited unit returns NO_CHANGE to prevent duplicate execution.', '\n'.join(plan.get('notes', []))])
            return '\n\n'.join(['Recover pending transaction' if plan.get('recovery') else 'Restore previous startup configuration', *common,
                                  'Transaction: '+str(plan['transaction_id']),
                                  'Restore config and boot enablement, preserve runtime: do not stop, restart or rerun business scripts.',
                                  'A pending run is inspected only; recovery does not rerun it. Prior business side effects cannot be undone.',
                                  '\n'.join(plan.get('notes', []))])
        if module == 'php-feather':
            if operation == 'restore':
                return '\n\n'.join(['Restore the reviewed stock SQLite activation. Custom build artifacts do not replace the distro module.',
                                      'Restore all currently managed CLI/FPM scopes to their first stock aliases; unowned scopes are untouched. The current scope switches do not limit this restore.',
                                      'The backend checks the recorded CLI/FPM configuration and reloads FPM when affected.',
                                      'Recorded restore targets:\n' + json.dumps(plan, indent=2, ensure_ascii=True, default=str)])
            return '\n\n'.join(['Explicit custom module build and activation; official php-sqlite3 alone does not provide the FeatherMountain SQL load_extension capability.',
                                  f'Selected scopes / CLI: {"on" if plan["cli"] else "off"}; FPM: {"on" if plan["fpm"] else "off"}',
                                  'Unselected scopes are preserved: off means do not touch that SAPI; it does not disable an existing custom activation.',
                                  'Authenticated Ubuntu source, exact ABI, development dependencies and compile plan:\n' + json.dumps(plan.get('build_plan', {}), indent=2, ensure_ascii=True, default=str),
                                  'Build in a separate artifact location without overwriting the system SQLite module. Verify native SQL.so loading and a real SQL function.',
                                  'Selecting this build enables the root-controlled SQL load_extension capability only in the selected scopes. FPM changes require reload and runtime verification.',
                                  '\n'.join(plan.get('notes', []))])
        if module == 'packages':
            return '\n\n'.join(['Selected missing packages: ' + ', '.join(plan['names']),
                                  'Candidate versions and dependency changes:\n' + json.dumps(plan['changes'], indent=2, ensure_ascii=True),
                                  f'Download archive bytes (cache may reduce transfer): {plan["download_bytes"]}',
                                  f'Disk size change bytes: {plan["disk_bytes"]}',
                                  'Package postinst scripts may start or restart services. No automatic APT update, removal, vhost or CGI configuration.',
                                  '\n'.join(plan.get('notes', []))])
        if module == 'banip':
            return '\n\n'.join(['BanIP blacklist / IPv4 only. IPv6 is not covered by this operation.',
                                  'Fixed script source: ' + plan['source_url'],
                                  'Pinned downloaded source SHA256: ' + plan['source_sha256'],
                                  'Executed managed script SHA256: ' + plan['script_sha256'],
                                  'Managed normalization corrections are described in the notes below.',
                                  'Blacklist data source: ' + plan.get('data_url', ''),
                                  'Required packages to install: ' + (', '.join(plan.get('packages', [])) or '(none)'),
                                  'Own blacklist set and IPv4 INPUT DROP rule; verified SSH rescue is preserved.',
                                  'Verified SSH rescue: ' + json.dumps(plan['rescue'], ensure_ascii=True),
                                  'Protected connection: ' + json.dumps(plan.get('origin', {}), ensure_ascii=True),
                                  'Existing service policy is not enabled automatically; no cron or boot task is added.',
                                  f'Timed rollback: {plan.get("timeout_seconds", 120)} seconds. After update, open a new SSH connection and confirm there.',
                                  'This UI cannot confirm reachability or cancel the watchdog. Package installation is not rolled back.',
                                  '\n'.join(plan.get('notes', []))])
        if module == 'swap':
            if operation == 'recover':
                return ('Recover only swap transaction ' + plan['transaction_id'] +
                        '.\n\nThe backend validates the journal and restores its recorded swap state. No new size target or LVM operation is included.')
            return '\n\n'.join(['Independent swap transaction. LVM growth is a separate operation and is not run here.',
                                  'Current observations / memory / disk checks:\n' + json.dumps(plan.get('observations', {}), indent=2, ensure_ascii=True),
                                  'Selected path, target size MiB, active now and persistent at boot:\n' + json.dumps(plan['config'], indent=2),
                                  'Changing active swap may require swapoff. The backend checks memory/disk and guards stale plans before changes.',
                                  '\n'.join(plan.get('notes', []))])
        if module == 'lvm':
            command = plan['command']
            if operation == 'check':
                command = [command[0], '--test', *[arg for arg in command[1:] if arg != '--resizefs']]
            return '\n\n'.join(['TEST MODE: without filesystem resizing; no growth is performed.' if operation == 'check' else
                                  'IRREVERSIBLE root LV + ext4 growth. Configuration Restore last cannot undo this operation.',
                                  'Detected root LV and free VG geometry:\n' + json.dumps(plan['before'], indent=2, ensure_ascii=True),
                                  'Growth mode: ' + plan['mode'] + '; additional GiB: ' + str(plan.get('add_gib')),
                                  f'Target bytes: {plan["target_bytes"]}; growth bytes: {plan["grow_bytes"]}',
                                  'Exact command: ' + ' '.join(command),
                                  'No swap change, disk/partition expansion or additional automatic action is included.',
                                  '\n'.join(plan.get('notes', []))])
        if module == 'firewall':
            return '\n\n'.join(['IPv4 + IPv6 INPUT default DROP: disabled and unlisted services block new connections.',
                                  'Fixed exceptions: loopback, established/related, verified SSH rescue, required ICMP/DHCP.',
                                  'Protected current SSH connection: ' + json.dumps(plan.get('origin', {}), ensure_ascii=True),
                                  'Verified SSH rescue (actual source and TCP port): ' + json.dumps(plan['rescue']),
                                  'Selected canonical service rules:\n' + json.dumps(plan['config'], indent=2, ensure_ascii=True),
                                  'Effective rules:\n' + plan['script'],
                                  f'Timed rollback: {plan.get("timeout_seconds", 120)} seconds. After apply, open a new SSH connection and confirm there.',
                                  'This first confirmation only applies a candidate. This UI cannot confirm reachability or cancel the watchdog.',
                                  'Firewall allow rules do not install/start services or grant DB access; no NAT or forwarding changes.',
                                  '\n'.join(plan.get('notes', []))])
        if module == 'mariadb-security':
            remote = plan.get('remote_enabled')
            remote_target = ('KEEP / listener, account lock and grants unchanged' if remote is None else
                             'ON / bind 0.0.0.0 / ALL PRIVILEGES ON *.* WITH GRANT OPTION' if remote else
                             'OFF / lock root@% / bind 127.0.0.1')
            return '\n\n'.join(['Separate MariaDB root access transaction; ordinary Restore last does not reverse this operation.',
                                  'Remote root@%: ' + remote_target,
                                  'Root password: ' + ('change root@localhost; synchronize root@% when managed by this operation (value hidden)'
                                                       if plan['change_password'] else 'keep current password'),
                                  'Preserve local root@localhost unix_socket access.',
                                  'Restrict source addresses with your firewall. This operation does not edit firewall rules.',
                                  '\n'.join(plan.get('notes', []))])
        if module == 'apt':
            return '\n\n'.join(['APT ' + operation,
                                  'Command: ' + ' '.join(plan['command']),
                                  'Package versions (simulation):\n' + ('\n'.join(
                                      f"{item['name']}: {item.get('installed') or '(new)'} -> {item['candidate']}"
                                      for item in plan['packages']) or '(none; update only refreshes indexes)'),
                                  'Kept back: ' + (', '.join(plan['kept_back']) or '(none)'),
                                  'Reboot currently required: ' + str(plan['reboot_required']),
                                  '\n'.join(plan['notes'])])
        lines = []
        if 'values' in plan:
            lines.append('Selected targets:\n' + '\n'.join(f'{key}: {value}' for key, value in plan['values'].items()))
            lines.append(preview(module, plan['values'], self.root_capabilities.get(module, {}).get('configured') or {}))
        if operation not in {'install', 'restore'}:
            active = plan.get('service_active')
            enabled = plan.get('service_enabled')
            lines += ['Service now: ' + ('keep current' if active is None else 'start' if active else 'stop'),
                      'At boot: ' + ('keep current' if enabled is None else 'enable' if enabled else 'disable')]
        if operation == 'install':
            lines.append('Packages selected: ' + ', '.join(plan.get('packages', [])))
            lines.append('Package scripts may start or enable services. Use Apply for explicit service/boot targets.')
            lines.append('Installed packages are not removed by configuration restore.')
        if operation == 'restore':
            lines.append('Restore prior files and recorded service state; external edits are guarded.')
            lines.append('Prior service state: ' + json.dumps(plan.get('service_snapshot', {}), default=str))
            if plan.get('observations', {}).get('previous_timezone'):
                lines.append('Restore timezone: ' + plan['observations']['previous_timezone'])
            lines.append('Restore commands: ' + json.dumps(plan.get('restore_commands', []), default=str))
        affected = []
        for item in plan.get('files', []):
            text = item['path']
            if operation == 'restore' and 'after_sha256' in item:
                text += ' (' + str(item.get('before_sha256') or 'absent')[:12] + ' -> ' + str(item.get('after_sha256') or 'absent')[:12] + ')'
            affected.append(text)
        lines.append('Affected files:\n' + ('\n'.join(affected) or '(none)'))
        lines.append('Validation: ' + json.dumps(plan.get('validate', []), default=str))
        lines.append('Service commands: ' + json.dumps(plan.get('apply_commands', []), default=str))
        lines.append('Verification scope: ' + str(plan.get('health_scope', 'See operation result; no whole-host health claim.')))
        lines.append('Notes: ' + json.dumps(plan.get('notes', []), ensure_ascii=True, default=str))
        return '\n\n'.join(lines)

    def _review_completed(self, confirmed, module, operation, plan):
        if not confirmed:
            self._root_result(module, {'status': 'CANCELLED', 'message': 'No operation was applied.'})
            self._finish_root_operation()
            return
        self._root_task = asyncio.create_task(self._execute_root_operation(module, operation, plan))

    async def _execute_root_operation(self, module, operation, plan):
        from .operation_progress import progress_context
        loop = asyncio.get_running_loop()
        session = self._operation_session
        def receive(event):
            loop.call_soon_threadsafe(self._receive_operation_progress, session, event)
        with progress_context(receive):
            await self._execute_root_operation_body(module, operation, plan)

    async def _execute_root_operation_body(self, module, operation, plan):
        self._root_result(module, {'status': 'RUNNING', 'message': 'Operation in progress; exit is disabled until the result.'})
        succeeded = False
        password = None
        try:
            if module == 'apache-fpm':
                method = self.apache_fpm_manager.restore if operation == 'restore' else self.apache_fpm_manager.apply
                result = await asyncio.to_thread(method, plan)
            elif module == 'startup':
                method = {'apply': self.startup_manager.apply, 'run': self.startup_manager.run_once, 'restore': self.startup_manager.restore}[operation]
                result = await asyncio.to_thread(method, plan)
            elif module == 'php-feather':
                method = self.php_feather_manager.restore if operation == 'restore' else self.php_feather_manager.apply
                result = await asyncio.to_thread(method, plan)
            elif module == 'packages':
                result = await asyncio.to_thread(self.package_manager.install, plan)
            elif module == 'banip':
                result = await asyncio.to_thread(self.banip_manager.update, plan)
                if result.get('status') == 'AWAITING_NEW_SSH':
                    self.banip_state['pending'] = {key: result.get(key) for key in
                                                   ('status', 'transaction_id', 'deadline_boottime', 'remaining_seconds')}
                    self._banip_status_at = time.monotonic()
            elif module == 'swap':
                result = await asyncio.to_thread(self.swap_manager.recover, plan['transaction_id']) if operation == 'recover' else await asyncio.to_thread(self.swap_manager.apply, plan)
            elif module == 'lvm':
                result = await asyncio.to_thread(self.lvm_manager.check if operation == 'check' else self.lvm_manager.apply, plan)
            elif module == 'firewall':
                result = await asyncio.to_thread(self.firewall_manager.apply, plan)
                if result.get('status') == 'AWAITING_NEW_SSH':
                    self.firewall_state['pending'] = {key: result.get(key) for key in
                                                       ('status', 'transaction_id', 'deadline_boottime', 'remaining_seconds')}
                    self._fw_status_at = time.monotonic()
            elif module == 'mariadb-security':
                password = self._mariadb_password
                self._mariadb_password = None
                result = await asyncio.to_thread(self.root_manager.mariadb_security_apply, plan, password=password)
            elif module == 'apt':
                result = await asyncio.to_thread(self.root_manager.apt_run, plan)
            elif operation == 'install':
                result = await asyncio.to_thread(self.root_manager.install, module)
            elif operation == 'restore':
                result = await asyncio.to_thread(self.root_manager.restore_last, module,
                                                 expected_fingerprint=plan['fingerprint'])
            else:
                result = await asyncio.to_thread(self.root_manager.apply, plan)
            succeeded = True
            self._root_result(module, result)
        except Exception as error:
            code = getattr(error, 'code', None)
            self._root_result(module, {'status': 'FAILED', 'code': code or 'STARTUP_FAILED',
                                       'message': 'Startup operation failed; script text and output are not included.'} if module == 'startup' else
                              {'status': 'FAILED', 'code': code if code in MARIADB_SECURITY_ERROR_CODES else 'MARIADB_SECURITY_FAILED',
                                       'message': 'MariaDB root access operation failed. Inspect its sanitized transaction status.'}
                              if module == 'mariadb-security' else
                              {'status': 'FAILED', 'code': getattr(error, 'code', 'OPERATION_FAILED'), 'message': str(error)})
        finally:
            password = None
            try:
                if module == 'swap':
                    await self._refresh_swap_status(force=True)
                await self._refresh_observations(update_module=module if succeeded else None)
            finally:
                self._finish_root_operation()

    def action_quit(self):
        if self.root_busy:
            self.notify('Wait for the reviewed operation to finish, or cancel its review.')
        else:
            self._clear_mariadb_secret()
            self.exit()

    def on_unmount(self):
        self._clear_mariadb_secret()

    def action_next_page(self):
        if self.root_busy:
            return
        tabs = self.query_one('#pages', TabbedContent)
        ids = ['tab-overview'] + [f'tab-{key}' for key in FIELDS] + (['tab-apt', 'tab-packages'] if self.root_mode else []) + ['tab-network', 'tab-firewall', 'tab-update', 'tab-sqlite']
        # Move off the old pane before Hide can refocus its sibling and reactivate it.
        self.set_focus(tabs.query_one(Tabs))
        tabs.active = ids[(ids.index(tabs.active) + 1) % len(ids)]

    def action_previous_page(self):
        if self.root_busy:
            return
        tabs = self.query_one('#pages', TabbedContent)
        ids = ['tab-overview'] + [f'tab-{key}' for key in FIELDS] + (['tab-apt', 'tab-packages'] if self.root_mode else []) + ['tab-network', 'tab-firewall', 'tab-update', 'tab-sqlite']
        self.set_focus(tabs.query_one(Tabs))
        tabs.active = ids[(ids.index(tabs.active) - 1) % len(ids)]

    def on_select_changed(self, event: Select.Changed):
        if event.select.id == 'network-mode' and self.is_mounted:
            self._network_update_controls()
        elif event.select.id == 'fw-preset' and isinstance(event.value, str):
            self.query_one('#fw-ports', Input).value = event.value
        elif self.root_mode and event.select.id == 'fw-service' and isinstance(event.value, str):
            self._fw_show_selected(event.value)
        elif self.root_mode and event.select.id == 'lvm-mode' and not self.root_busy:
            self._lvm_update_controls()
        elif self.root_mode and event.select.id == 'swap-path' and isinstance(event.value, str) and event.value != self._swap_editor_path:
            self._swap_editor_path = event.value
            self._begin_swap_operation('status')

    async def on_button_pressed(self, event: Button.Pressed):
        action = event.button.id or ''
        if self.root_busy:
            return
        if action.startswith('update-'):
            self._begin_release_operation(action[len('update-'):])
            return
        if action.startswith('network-'):
            self._begin_network_operation(action[len('network-'):])
            return
        if self.root_mode and action.startswith('startup-'):
            self._begin_startup_operation(action[len('startup-'):])
            return
        if self.root_mode and action.startswith('feather-'):
            self._begin_php_feather_operation(action[len('feather-'):])
            return
        if self.root_mode and action.startswith('packages-'):
            self._begin_packages_operation(action[len('packages-'):])
            return
        if self.root_mode and action.startswith('banip-'):
            self._begin_banip_operation(action[len('banip-'):])
            return
        if self.root_mode and action.startswith('fw-'):
            self._begin_firewall_operation(action[len('fw-'):])
            return
        if self.root_mode and action.startswith('lvm-'):
            self._begin_lvm_operation(action[len('lvm-'):])
            return
        if self.root_mode and action.startswith('swap-'):
            self._begin_swap_operation(action[len('swap-'):])
            return
        if action == 'root-security-mariadb':
            self._begin_mariadb_security()
            return
        if self.root_mode and action.startswith('apache-fpm-'):
            self._begin_apache_fpm(action[len('apache-fpm-'):])
            return
        if action.startswith('root-'):
            operation, _, module = action[len('root-'):].rpartition('-')
            self._begin_root_operation(module, operation)
            return
        if action == 'exit':
            self.action_quit()
        elif action == 'refresh':
            await self.action_refresh()
        elif action.startswith('preview-'):
            module = action[len('preview-'):]
            target = {key: self.query_one(f'#{widget_id}', Input).value for key, widget_id in self.field_ids[module].items()}
            try:
                output = preview(module, target, self.snapshot['modules'].get(module, {}).get('configured', {}))
            except ValueError as error:
                output = f'INVALID DRAFT: {error}'
            self.query_one(f'#out-{module}', TextArea).load_text(output)
        elif action == 'apache-candidate':
            self.query_one('#out-apache', TextArea).load_text(apache_candidate())
        elif action in {'fw-add', 'fw-preview', 'fw-reset'}:
            output = self.query_one('#fw-output', TextArea)
            try:
                if action == 'fw-reset':
                    self.fw_config = json.loads((ROOT / 'config/firewall.example.json').read_text(encoding='utf-8'))
                if action == 'fw-add':
                    ports = [int(item.strip()) if item.strip().isascii() and item.strip().isdigit() else item.strip()
                             for item in self.query_one('#fw-ports', Input).value.split(',')]
                    sources = ['public'] if self.query_one('#fw-source-mode', Select).value == 'public' else [item.strip() for item in self.query_one('#fw-sources', Input).value.split(',')]
                    rule = {'enabled': self.query_one('#fw-enabled', Switch).value,
                            'protocol': self.query_one('#fw-protocol', Select).value, 'ports': ports, 'sources': sources}
                    candidate = {**self.fw_config, 'rules': self.fw_config['rules'] + [rule]}
                    firewall_preview(json.dumps(candidate))
                    self.fw_config = candidate
                output.load_text(firewall_preview(json.dumps(self.fw_config)))
            except (ValueError, TypeError) as error:
                output.load_text(f'INVALID DRAFT: {error}')
        elif action in {'sqlite-run', 'sqlite-spatial', 'sqlite-acceptance'}:
            if self.root_busy:
                return
            sqlite_buttons = list(self.query('#sqlite-run, #sqlite-spatial, #sqlite-acceptance'))
            if self.root_mode:
                self._start_root_busy()
                self._receive_operation_progress(self._operation_session, {
                    'phase': 'verify', 'command': 'php', 'state': 'running',
                    'output_bytes': 0, 'idle_seconds': 0})
            else:
                for button in sqlite_buttons:
                    button.disabled = True
            self.query_one('#sqlite-output', TextArea).load_text('Running explicitly requested local CLI smoke...')
            try:
                if action == 'sqlite-run':
                    output = await asyncio.to_thread(sqlite_smoke)
                else:
                    from .diagnostics import sqlite_spatial_smoke
                    module = self.query_one('#sqlite-module', Input).value.strip()
                    output = await asyncio.to_thread(sqlite_spatial_smoke, module, acceptance=action == 'sqlite-acceptance')
                self.query_one('#sqlite-output', TextArea).load_text(output)
            finally:
                if self.root_mode:
                    self._finish_root_operation()
                else:
                    for button in sqlite_buttons:
                        button.disabled = False

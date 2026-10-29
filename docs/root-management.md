# Root 安裝、儲存與套用

`3waServerSetting` 以 root 執行時，在 Ubuntu 26.04 提供 Apache、PHP-FPM、vsftpd、Samba、MariaDB 與時區的安裝及設定，另有 System updates 頁操作 APT。一般帳號維持唯讀觀察與記憶體內預覽。Ubuntu 22.04／24.04 尚未開放寫入。

測試 VM 的部署目錄是 `/opt/3waServerSetting`，程式與 venv 均由 root 管理。不要以 root 執行一般帳號可修改目錄中的程式。

```sh
/opt/3waServerSetting/run.sh
```

各服務頁面提供：

- `Install service`：檢查並安裝該頁固定的 Ubuntu 套件及必要相依。PHP 頁包含 FPM、CLI、SQLite；不升級整套系統或變更 apt 來源。套件腳本可能啟動／啟用服務，套件與 Apache/FPM 整合不由設定還原解除。
- `Load recommended`／`Load saved`：只載入表單，不改主機。
- `Apply recommended`：使用推薦值，先檢閱本頁計畫再確認套用。
- `Save & apply custom`：使用表單值，驗證成功後儲存配方。
- `Service now` 與 `Start automatically at boot`：分別設定當下啟停及開機啟動；預設保留目前狀態。
- `Restore last`：檢閱並還原該頁最近一次仍可安全還原的設定交易。

`Ctrl+Right`／`Ctrl+Left` 切換頁籤，`Tab` 移動焦點，`Ctrl+Q` 離開。80×24 終端會隱藏看板娘，控制項仍可用 Tab 到達。進行交易時會阻止重複操作及離開。

設定採原子寫入，驗證後才回報成功及儲存配方；失敗會嘗試還原原始檔案、metadata 與服務狀態。重複套用相同值不重新載入服務。外部修改、過期計畫、非預期 symlink、未完成交易或無法可靠還原的服務狀態會阻擋操作。`RECOVERY_REQUIRED` 必須先查看交易記錄及原生服務診斷，不會自動覆蓋外部修改。

持久資料：

| 內容 | 路徑 |
| --- | --- |
| 成功儲存的非秘密配方 | `/opt/3waServerSetting/config/recipes` |
| 原始設定備份 | `/opt/3waServerSetting/backups/<transaction-id>` |
| 交易記錄 | `/var/lib/3wa-server-setup/transactions` |
| 簡要結果記錄 | `/var/lib/3wa-server-setup/logs` |

這些目錄由 root 私有管理。設定備份可能包含既有服務設定，不應放入 Git 或測試包。`Restore last` 不移除套件、不還原資料庫資料，也不恢復安裝套件前的整台 VM；完整回復仍使用 Hyper-V 匯出備份。

## CLI

MariaDB root 密碼與 root@% 外連使用[獨立 Root access 操作](mariadb-root-access.md)，不放入普通配方，也不受一般 Restore last 還原；其他帳號、分享與資料仍未管理。

CLI 輸出 JSON。變更必須明確加上 `--confirm`，`--values` 是完整欄位的 inline JSON。

```sh
/opt/3waServerSetting/run.sh --root-install php --confirm
/opt/3waServerSetting/run.sh --root-plan php --start --enable
/opt/3waServerSetting/run.sh --root-apply php --start --enable --confirm
/opt/3waServerSetting/run.sh --root-restore php --confirm
/opt/3waServerSetting/run.sh --root-apply time --confirm
/opt/3waServerSetting/run.sh --apt-plan upgrade
/opt/3waServerSetting/run.sh --apt-update --confirm
/opt/3waServerSetting/run.sh --apt-upgrade --confirm
```

設定值、檔案路徑、驗證命令及服務目標可從 `--root-plan <module>` 檢閱。服務安裝使用固定套件清單；不接受任意檔案或 shell 命令。獨立的[套件勾選安裝頁](apt-install.md)可另外搜尋 Ubuntu APT 套件。

## APT 更新與升級

System updates 頁的 `apt update` 更新來源清單，任何來源錯誤皆回報失敗。`apt upgrade` 先模擬，顯示套件舊／新版本及暫緩套件，再檢閱確認。執行前重新核對套件狀態與索引，過期預覽會被阻擋。

程式使用適合腳本的 `apt-get` 介面，搭配 `--with-new-pkgs --no-remove` 對應 `apt upgrade` 的新相依套件行為；保留修改過的 conffile，套件腳本仍可能重啟服務。命令語義參照 [Ubuntu 26.04 apt](https://manpages.ubuntu.com/manpages/resolute/man8/apt.8.html) 與 [apt-get](https://manpages.ubuntu.com/manpages/resolute/man8/apt-get.8.html)。

不自動執行 autoremove、full-upgrade、跨版本升級或重開機，也不解除 hold／分階段更新。完成後回報 `/run/reboot-required`。套件操作無設定交易的自動 rollback；失敗或逾時可能已有部分變更，需先檢查原生 APT／dpkg 狀態。原始來源 URL 與命令輸出不寫入工具結果記錄。

## 驗證範圍

SSH 推薦設定為 `ClientAliveInterval 60` 與 `TCPKeepAlive yes`；兩者是不同機制，並非改名。前者在沒有收到 client 資料時經 SSH 加密通道要求回應，後者啟用 TCP 層 keepalive。`ClientAliveCountMax` 沿用主機原值；正常有回應的閒置連線與真正無回應的連線不同，這不是保證網路中斷後永不斷線的設定。[OpenSSH sshd_config](https://man.openbsd.org/sshd_config#ClientAliveInterval)

工具使用原生 `/etc/ssh/sshd_config.d/00-3wa-server-setup.conf`，由 `/etc/ssh/sshd_config` 的全域 Include 載入。套用先通過 `sshd -t`、`sshd -T` 全域生效值檢查，原服務已啟動時才 reload；不提供停止 SSH 或切換開機狀態的操作。原本的 Port、登入驗證、金鑰及 Match 區塊保留，Match 使用者的覆寫另以實際連線情境驗證。

reload 後的新連線使用新設定；目前已建立的登入保留原連線設定。自訂 ssh.service／SSHD_OPTS 可能改用其他設定檔，工具會拒絕不支援的 unit／環境配置，避免把預設檔的 `sshd -T` 結果當成另一份設定的證據。

FTP 推薦設定保留本機帳號寫入與既有 PASV 60000–60100，另加入使用者指定的 IPv4 監聽、權限與傳輸記錄：

```ini
file_open_mode=0777
local_umask=022
listen=YES
listen_ipv6=NO
local_enable=YES
anonymous_enable=NO
xferlog_enable=YES
xferlog_file=/var/log/vsftpd.log
```

這些值進入同一份 `/etc/vsftpd.conf` 檢閱、儲存、套用與還原；未管理的原生指令保留。權限使用帶 `0` 前綴的八進位值，拒絕含 8／9 的值，以及同時 `listen=YES`、`listen_ipv6=YES`。傳輸記錄格式沿用主機既有選擇：`xferlog_file` 用於 standard xferlog 格式；預設 vsftpd 格式使用 `vsftpd_log_file`（原生預設也是 `/var/log/vsftpd.log`）。若主機已自訂 syslog 或雙格式設定，需另依該設定確認實際記錄位置。[Ubuntu vsftpd.conf](https://manpages.ubuntu.com/manpages/resolute/man5/vsftpd.conf.5.html)

PHP 配方同時寫入同版本 CLI、FPM 的 `conf.d/zz-3wa-server-setup.ini`，不覆寫原本的 `php.ini`、Feather 或 SQLite3 模組設定。CLI 使用版本化原生命令讀取實際生效值，FPM 使用獨立 FastCGI request；結果分列 `sapis.cli` 與 `sapis.fpm`。若其他 ini 或 pool 覆寫導致不符，交易還原，並指出 SAPI、欄位與可能來源。FPM 未啟動時仍檢查 CLI，整體 FPM 驗證維持 UNVERIFIED。

Time 頁的 `Install service` 安裝 `ntpsec-ntpdate`，提供 `ntpdate` 指令；時區套用不會自行停止既有校時服務或跳動系統時間。[Ubuntu 26.04 ntpdate](https://manpages.ubuntu.com/manpages/resolute/man8/ntpdate.8.html)

長命令執行時，畫面下方顯示階段、整體經過秒數、距最近輸出的時間與診斷輸出量；每秒更新，階段轉換另列在簡要 log。這些是安全量測，不公開原始來源 URL、密碼或編譯輸出。APT 若提供 `Fetched` 摘要，另顯示實際下載量；沒有可靠總量時不提供百分比或預估剩餘時間。Feather 的編譯 worker 可能靜默，畫面仍顯示 compile 階段與計時，不能把靜默判定為卡死。

成功結果會列出實際驗證範圍。Apache 檢查 native include、設定語法與 daemon；PHP 透過獨立 FastCGI request 檢查 FPM 生效值及 SQLite 模組，CLI 結果分開顯示。Samba 檢查 testparm 全域值；MariaDB 檢查 option file 及本機 socket 的選定 GLOBAL 變數。FTP 僅檢查選定設定結構與 daemon。時區檢查目前值，NTP 同步獨立觀察。

FTP 登入／TLS／傳輸、Samba 分享權限與帳密、資料庫應用程式帳號與資料、各網站 vhost 都需要另作應用層驗收。多版本／多 pool PHP、舊 mod_php/prefork 自動遷移、自訂 include／systemd FPM 結構並未支援；PHP 偵測到非空 prepend／append bootstrap 時會保留原值並阻擋 probe，不會修改 worker 的 PHP_ADMIN_VALUE。

新增獨立的[防火牆來源與 SSH 保護](firewall-root.md)、[Swap 容量／啟停／fstab](swap-root.md)與[根 LV 擴容](root-lvm.md)操作。三者各自檢閱確認；Swap 建議值 16384 MiB 不會自動套用，LVM 不提供縮容還原。MariaDB root 帳號與監聽仍由獨立 Root access 操作管理。

另有[羽山版 PDO SQLite](php-feather.md)編譯、CLI／FPM 分別啟用及官方模組還原，以及[手動 BanIP](banip-root.md)下載／執行／新 SSH 確認。BanIP 不加入排程，與服務來源防火牆獨立。

[Startup 開機腳本](startup-root.md)使用獨立的 root Bash 編輯器、自有 oneshot unit 與私有草稿。套用只寫配置／開機旗標，立即執行另外檢閱；還原不重跑或停止已啟動工作，既有 rc.local 保留為觀察。

工具不改 sshd、OS IP、登入帳號或虛擬磁碟／partition／PV；羽山版編譯僅會建立預覽中列出的專用無登入 builder 系統帳號。Apache 敏感檔案規則仍是候選預覽。防火牆儲存草稿在 `/opt/3waServerSetting/config/firewall/draft.json`，確認後開機設定在 `/var/lib/3wa-server-setup/firewall/confirmed.json`，不寫 `/root/nat.sh`。

# Ubuntu 26.04 root 驗收

2026-10-24 至 2026-10-25，在使用者授權的 Ubuntu 26.04 Hyper-V 測試 VM 執行。這是 `3waAI/root-install-apply` 未發布開發版，base commit `b37d776`；不代表 Ubuntu 22.04／24.04 或其他主機通過驗收。

程式及專用 venv 部署到 root 控制的 `/opt/3waServerSetting`，來源包逐檔 SHA256 核對。原本兩份 john 唯讀測試目錄不作 root 程式來源；既有完整 Hyper-V 匯出備份保留，沒有宣稱已完成開機還原測試。

## 已通過

2026-10-25 後續擴充已新增 [MariaDB root 密碼／root@% 外連驗收](mariadb-root-access.md)，以及使用者提供的 3wa 圖像。下表保留上一輪安裝／設定／APT 驗收；新增功能的最終整套與真實 TTY 結果列在文件末尾。

| 項目 | 實際證據 |
| --- | --- |
| 完整 Linux 測試 | Python 3.14.4，以 root 跑完整 unittest：125/125 通過、0 skip，82.329 秒；含真實暫存檔、POSIX 權限、flock、UI 與 CLI |
| 安裝 | 新管理器安裝 Apache、PHP-FPM/CLI/SQLite、vsftpd、Samba、MariaDB；Apache/FPM 原生 handler 整合成功 |
| 推薦設定 | 六頁套用成功；五個 daemon active、boot enabled；再次套用回 NO_CHANGE 並重新檢查限定範圍 |
| 時區 | UTC → Asia/Taipei → NO_CHANGE → 還原 UTC → 再套用 Taipei；NTP 同步獨立觀察為 yes |
| PHP 自訂值與儲存 | FPM memory_limit 128M → 256M，儲存配方；還原後配方與獨立 FPM request 均為 128M |
| 真正 HTTP/FPM | Apache HTTP 200，PHP 8.5.4 的 fpm-fcgi，七個推薦值生效；PDO SQLite、SQLite3 與 RTree 通過；Server 僅 Apache，沒有 X-Powered-By；臨時非秘密 PHP 端點已移除 |
| 過期計畫 | 對管理檔案加上測試外部修改後，舊計畫被 STALE_PLAN 拒絕，外部內容未被工具覆蓋；測試結束移除測試修改 |
| 失敗回復 | 對原生 Apache 設定交易注入一次驗證命令失敗，實際檔案 bytes 還原、交易 ROLLED_BACK；後續原生 parser／daemon 驗證通過 |
| 當下啟停與開機設定 | FTP stop + disable，結果明列 runtime 未驗證；Restore last 恢復 start + enable，再驗證通過 |
| 一般帳號 | john 可讀狀態；root apply 與 APT update 均 ROOT_REQUIRED，交易檔案沒有變動 |
| 真 SSH TTY | root、實際 /dev/pts；按鈕焦點 Ctrl+Right／Left 不反跳；140×40／80×24、resize、確認畫面取消與 Ctrl+Q 通過 |
| TTY 真正儲存／還原 | 表單輸入 Etc/UTC → Save & apply custom → 原生 UTC → Restore last → 原生 Asia/Taipei；UI apply／restore 對應同一筆交易 `20261024T155307Z-ece03a1b8884`，journal 為 root:root 0600、RESTORED |
| APT update | strict update 成功，狀態 APT_UPDATED，執行前／後都有交易結果 |
| APT upgrade | 實際升級 65 個套件，443.65 秒，APT_UPGRADED；13 個套件按原生 phasing 暫緩。其餘待升級數 0、dpkg --audit 無異常、reboot-required 為 false |
| 升級後保留設定 | 六頁再套用均 NO_CHANGE、限定 runtime 檢查通過；時區 Taipei／NTP yes，IP 192.168.137.26/24、MAC 與 static default route 正確；HTTP/FPM 再驗成功 |
| CLI SQLite | SQLite 3.46.1，SQLite3 RTree／PDO Unicode／RTree 三項 PASS；SpatiaLite 未提供模組，明確 UNVERIFIED，smoke 依設計 exit 2，沒有當作全數通過 |

升級後版本：Apache 2.4.66-2ubuntu2.6、PHP-FPM 8.5.4-0ubuntu1.5、MariaDB 11.8.6-5ubuntu0.1、Samba 4.23.6+dfsg-1ubuntu2.2、vsftpd 3.0.5-0.4。此次沒有執行 reboot 或完整 VM 開機還原測試。

## 驗收時修正的原生差異

- Ubuntu 原生 `/etc/os-release` symlink 採固定 canonical 讀取例外，管理檔案仍拒絕 symlink。
- `/var/log` 是 root:syslog 0775；工具結果日誌改放 `/var/lib/3wa-server-setup/logs`，交易前先檢查持久目錄，晚期日誌失敗會保留實際套用結果。
- systemctl 的空 EnvironmentFiles 結構陣列即使 `--all` 也可能省略；已用原生 D-Bus 與 systemd 實作確認，只將這個已知欄位的省略視為空。未知 FPM 環境／drop-in／ExecStart 與非空 bootstrap 仍拒絕 probe。
- Python 3.14 `Path.lstat()` 直接呼叫 os.lstat；POSIX 目錄權限測試改用真實 chmod，避免 Windows mock 邊界掩蓋測試差異。

## 限制與重跑

健康範圍見 [Root 管理](root-management.md)。FTP 登入、TLS、資料傳輸、被動連線，Samba 分享／帳密權限，MariaDB 應用程式帳號與資料，以及各網站 vhost 均未驗收。SpatiaLite native 模組未載入。Startup 尚無主機寫入功能；新版防火牆、Swap、LVM 另見下方 2026-10-25 擴充驗收。

```sh
cd /opt/3waServerSetting
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m pip check
./run.sh --root-plan php --start --enable
./run.sh
```

125 項測試中的 root engine／recipes 使用獨立暫存 filesystem；表格中另外列出的原生安裝、設定、HTTP、還原及 TTY 才是測試 VM 證據，兩者不互相替代。原始測試輸出與故障注入 driver 留在開發工作目錄的 `.root-dev`，未加入來源包、Git 或發布內容。

## 2026-10-25 MariaDB root 與圖像擴充

新版完整 Ubuntu root suite **167/167 通過、0 skip，113.651 秒**。新增 root 密碼／root@% 控制與原生網路檢查，並換用提供的 3wa 圖；原 PNG SHA256 `3303716e96567a3076d0642e2f0313ef80e7498e6963403e6a95efeb5be19b2c`，未修改原檔。終端以 40×80 Braille dots 保留比例、置於既有 20×20 cells；沒有新增 runtime 圖片依賴。

MariaDB 實際改密碼／Windows 登入／OFF 拒絕／故障補償／秘密掃描見 [root access 驗收](mariadb-root-access.md)。此輪另外以真正 root SSH TTY 驗證：

- 140×40 顯示新圖，主區 116 欄，圖像不取得焦點；80×24 收起圖像、主區 80 欄。
- 從按鈕焦點 Ctrl 左右切頁正常。
- 新密碼與確認欄遮蔽；公開 canary 在預覽前清空，ON／KEEP 預覽均可取消，畫面／SVG／終端輸出不含其明文。
- 此輪 TTY 只預覽／取消，未按 Confirm；帳號與密碼狀態、journal（34→34）及 access 設定檔前後未變，Ctrl+Q 正常退出。

TTY 報告與實際 SVG 截圖留在 `.root-dev/mariadb-tty-e025e1d2`。正式使用前結束舊 TUI 再啟動，才會載入新面板與圖像。

## 2026-10-25 防火牆、Swap 與 LVM 擴充

完整 Ubuntu 26.04 root suite **232/232 通過、0 skip，182.839 秒**，執行前後 source/test SHA256 一致。此數字包含暫存 filesystem、fake kernel/systemd 邊界與 UI／CLI 測試；下表另外列出實際 VM 的 kernel／systemd／檔案系統證據。

| 原生項目 | 實測結果 |
|---|---|
| FW backend／語法 | nft 1.1.6、雙族 nf_tables；`nft --check` 通過；只處理自有 inet INPUT table |
| FW 原 SSH 不能確認 | 實際新規則生效後，原實體 SSH transport 被拒絕確認；新 SSH transport 才提交 |
| FW 斷線回復 | SSH 救援限定候選生效，原 controller 與 SSH 結束；獨立 systemd watchdog 到 120 秒仍自行恢復 table absent／boot disabled，新 SSH 成功連回 |
| 每服務來源／雙棧 | namespace/veth 獨立來源：HTTP IPv4 200、IPv6 drop；UDP IPv6 echo、IPv4 drop；FTP／SSH 未許可測試來源 drop，彼此沒有交叉放行 |
| public 與 IPv4 全開 | public HTTP 雙棧皆 200；改成 `0.0.0.0/0` 後 HTTP IPv4 200、IPv6 drop |
| FW 更新／還原 | 三次新 SSH 確認更新，逐筆還原至首次未啟用狀態；保留 root 私有草稿 |
| FW 原生開機 loader | 經新 SSH 確認後，只刪核對過的自有 table，實際 restart systemd boot loader；同 policy SHA、handle 15→16，status／後續還原正常。未重開 VM |
| Swap 空間防護 | 16384 MiB 目標因目前剩餘約 9.2 GiB，以 SWAP_DISK_SPACE 在寫檔前停止 |
| Swap 實際 kernel | 新 `/swapfile` 128→256 MiB，on／boot ON、off／boot ON、on／boot OFF、off／boot OFF、NO_CHANGE 均通過 |
| Swap 失敗回復 | 注入一次新檔 swapon 失敗，恢復測試原 inode、UUID、kernel active 與 fstab；成功 resize 後清除 inactive 原 paging file |
| Swap 保留現況 | 原 `/swap.img` inode 9256、3998220288 bytes（3813 MiB）、UUID、active 與 persistent 未變；fstab bytes／metadata 完全恢復；新測試檔與 staged swap files 已清除 |
| Root LV 試算 | 實際 root LV／ext4 各 19834863616 bytes，VG free 同量 4729 extents；原生 LVM test mode 通過，未套 filesystem resize |
| LVM 實際擴容 | 獨立 loop VG/LV/ext4：256→764 MiB，`lvextend --resizefs` 成功，檔案 marker 保留，VG free 0 再操作 no-op |
| LVM 部分完成恢復 | 另一輪只完成 LV 擴容，工具顯示 LVM_FS_RESIZE_REQUIRED；recover 僅跑 resize2fs 完成 filesystem，沒有再加一次 extents |
| LVM 保留現況 | 測試 VG、LV、mount、loop backing 已清除；VM 真正 root LV／filesystem／VG free 前後完全一致 |

實測修正了兩個原生格式差異：`lvs --segments` JSON 使用 `seg` 而非 `lv`；`blkid --output export` 會輸出 DEVNAME，須核對選定路徑。Ubuntu installer 的既有 swap 可能使用 unwritten extents，不能把 SEEK_HOLE 單獨當作未實體配置；僅在配置量下限與實際 kernel／root 私有完整身分可核對時接受，未知 inactive 檔仍拒絕。Swap 私有狀態移到 `config/swap/`，保留公共 config 範本目錄權限。

這批驗收結束當下，`.26` 依使用者選擇還原防火牆未啟用；保留原 3813 MiB `/swap.img`，真根 LV 當時約 18.47 GiB。這批沒有執行 VM reboot、記憶體壓力測試或 root LV 真正擴容。後續使用狀態見下一節，不能將此歷史基準當成目前容量。Swap 開機持續驗證到 fstab 與原生 systemd generator；防火牆驗證到實際 systemd loader，兩者均不宣稱完整 reboot 驗證。

原始證據留於 `.root-dev/firewall-native-{timeout,flows,boot}-report.json`、`swap-native-report.json`、`lvm_native_acceptance.py.log`、`run_firewall_storage_suite.py.log`；不包含於發布來源包或 Git。

真正 root SSH TTY 驗收也通過：140×40 主區 116 欄與 Braille 圖像、80×24 收起圖像並保有 80 欄；按鈕焦點 Ctrl+左右不反跳。HTTP／SSH／FTP／PASV／SMB／MariaDB 六份來源草稿獨立，雙棧範例、檢閱／取消、LVM all-free 不可逆檢閱／取消、Swap 現有 3813 MiB 與僅載入 16384 MiB 建議值都完成鍵盤操作。沒有按 Save、Confirm 或 Recover，結束時 kernel 規則、root LV／FS／VG free、原 swap inode／大小／權限、fstab／儲存配方與 journal（47→47）全部不變。24 張真實 SVG 與 report 留於 `.root-dev/firewall-storage-tty-10b68798`；寬／窄 PNG 已轉出並人工檢視。

最後另以 john 執行實際 `run.sh --smoke`，一般帳號啟動／鍵盤／resize 成功，root swap 操作以 ROOT_REQUIRED 拒絕，journal 不變；公共 config 0755、Swap 私有子目錄 0700。原有六頁服務／FPM／時間限定檢查通過，五個 daemon 仍 active/enabled，`.26/24` 與 static default route 保持，dpkg audit 與 pip check 通過，暫時 namespace／veth／loop 裝置已移除。

## 2026-10-25 BanIP 與 APT 套件頁擴充

後續盤點時，VM root LV／ext4 已為 39,669,727,232 bytes（約 36.94 GiB）、VG free 0；`/swap.img` 已為 17,179,869,184 bytes（16 GiB），inode 786444、UUID `c409783a-aa42-4ff7-87dd-c33a5ce6abd1`、active／fstab persistence 均為 True。本輪以此新基準保留，不恢復成上一批的小容量。

BanIP 實際交易 `20261025T035802Z-6231886e27e2` 成功載入 24,851 個唯一 IPv4，當次 CSV 與完整集合 count／SHA256 一致。原 SSH 被拒絕確認，另一條全新實體 SSH 確認成功。隔離 namespace 中使用真正黑名單成員 `1.9.211.178` 作來源，HTTP 被 DROP 且 counter 0→2；正常來源 HTTP 200。測試 namespace、veth 與暫時路由已清除。保留本次 runtime 黑名單，服務來源防火牆仍 off，沒有 cron／開機排程。

來源原文 2663 bytes、SHA256 `88f0b6b63c1545816e1475b732b4050220db58618ddff8608fd862dc81212036`；執行版 2726 bytes、SHA256 `1fae776d2be394e531fc6f051f74b79c8ef8823211b986e60f3c9d79926cfc0c`。原文保留，上游已處理無換行末筆；執行版只改 Bash builtin normalization，實際完整清單處理由原先估計數分鐘降至數秒。原生 iptables-nft 只 materialize INPUT、iptables-save 邏輯上仍序列化三個 ACCEPT chain 的差異已補測及修正，不放寬外部規則辨識。

APT 套件頁實際讀取 catalog／搜尋／provider 與依賴試算。14 個 GIS 套件的模擬結果為 379 個新相依、下載 348,286,612 bytes、磁碟 1,536,043,008 bytes，沒有因此安裝整套 GIS。真實批次選取 `screen` 與 `ntpsec-ntpdate`：screen 原已安裝 4.9.1-3ubuntu2，保留版本；新增 `ntpsec-ntpdate`、`ntpsec-ntpdig`、`python3-ntp` 1.2.3+dfsg1-8ubuntu2，下載 141,192 bytes、磁碟 549,888 bytes，dpkg 逐一核對成功，交易 `20261025T041007Z-c6abbff331bc` 為 PACKAGES_INSTALLED。沒有升級／移除套件、主動校時或改寫官方 PDO SQLite 模組。

原生證據留於 `.root-dev/banip-native-report.json`、`packages-native-preview.json`、`packages_native_install.py.log`，不納入來源包。羽山版 PDO SQLite 與本輪完整 suite／TTY 驗收另列後續紀錄。

## 2026-10-25 羽山版 PDO SQLite 原生驗收

使用精確 Ubuntu PHP 8.5.4-0ubuntu1.5 source／dev、NTS amd64 ABI 20250925、SQLite 3.46.1-9ubuntu0.3。私有 APT Sources 驗證及來源檔 SHA256 通過；Ubuntu 來源包另附 `.orig.tar.xz.asc`，已補上已知檔案格式與逐檔完整性驗證，仍要求真正 orig tar／debian tar。兩次先前未知格式的嘗試在安裝依賴／建立 builder／變更 ini 前停止，交易保留 FEATHER_FAILED。

實際安裝固定缺少的開發依賴，建立無登入、無額外群組的 `threewa-php-build` 系統帳號；解壓／phpize／configure／make／native SQL probe 在有時限、無網路的 unprivileged systemd cgroup 完成。未執行 make install。三次真實編譯、下列啟用／還原流程全部成功：

- CLI-only 啟用：CLI 真正 SQL load_extension() 成功，FPM alias 完全不變；另以 john 執行原有 `new PDO(...)` 寫法載入自有 nonce `.so` 並呼叫其函式成功。
- CLI-only 還原：恢復原生官方 ini symlink，CLI SQL 載入以 not authorized 拒絕。
- CLI＋FPM 啟用：獨立 CLI 與真正 Unix FastCGI request 皆成功載入；FPM 原生 config test／reload 通過。
- 還原失敗補償：在真實 stock restore 的第一次 probe 注入公開測試錯誤，返回 FEATHER_ROLLED_BACK；前一個可用 custom CLI／FPM alias 與 active manifest 原 bytes 全部恢復，再次 SQL probe 成功。
- 正常還原兩個 SAPI：官方 aliases 完整恢復，CLI／FPM 真正 SQL 載入均拒絕。
- 最後再次真實編譯並啟用兩個 SAPI，保留此狀態供使用。每次建置的自有 `.so` 證明 custom gate On 成功、Off 與官方模組拒絕、ini_set() 無法開啟 SYSTEM gate、Unicode PDO 操作成功；另驗證新 DB 連線未繼承已載入函式。

官方 `/usr/lib/php/20250925/pdo_sqlite.so` SHA256 始終為 `bef44e882e5082ce3e002377b4c63220c3ecaac7cb675d0f647610a86838ab6e`。當下 16 GiB swap 的 inode／fstab、約 36.94 GiB root LV／filesystem、靜態路由、BanIP 24,851 筆與未啟用的服務防火牆前後一致，沒有 pending root 交易，dpkg audit 通過。這裡驗證的是自有 native SQL probe，未把它當作 SpatiaLite／GDAL／MapServer 應用驗收。

完整 driver 報告為開發目錄 `.root-dev/feather-native-report.json`；私有建置 manifest 與失敗診斷在 VM `/var/lib/3wa-server-setup/feather-build/`。這些執行資料不納入來源包。

## 本輪最終完整測試與真實 TTY

Ubuntu root 完整 unittest **309／309 通過、0 skip，582.046 秒**。54 個被測來源／測試／launcher 檔案在執行前後 SHA256 一致，逐一吻合實際部署快照 `2ef8c00e28c54534d1d61aa40ca053466d7583ff9014b80521d6913e37aa6413`。初輪僅一個 POSIX symlink fixture 誤用管理檔案讀取 guard 模擬 PHP，已修正 fixture 並補上明確還原狀態斷言；產品對未知 symlink 的 guard 未放寬。

另一條真正 root SSH TTY 完成 140×40／80×24 操作：

- 寬畫面主區 116 欄、400-cell Braille 圖像不可取得焦點；窄畫面收起圖像。Ctrl+Right／Left 從按鈕焦點切頁正常。
- 真實 catalog 顯示 gdal-bin 未安裝；勾選、完整相依預覽、安裝第一層檢閱後取消，搜尋候選不自行選取。
- 羽山版 CLI／FPM／兩者範圍與 Build／Restore stock 檢閱，寬窄畫面都可取消；未勾選範圍保留現況的文字與計畫一致。
- BanIP 顯示 24,851 筆、手動操作與無排程狀態；未觸發更新。
- Ctrl+Q 正常退出。獨立 guard 證明 dpkg／APT 索引與 sources／PHP 設定／工具配方／activation／BanIP kernel 與完整程式雜湊全部不變，journal 62→62；沒有按 root-confirm。

16 張實際終端 SVG、raw TTY 與完整報告留於 `.root-dev/new-features-tty-1b4ed0e4/`，不納入來源包。最終另核對五個 daemon active／enabled、六頁限定 runtime 驗證、NTP synchronized、john 非特權啟動與安裝拒絕、靜態 `.26/24`／default route、dpkg audit、pip check 與部署檔案 manifest，均通過。產品與這份最終測試快照之間只補文件；未 commit／push／release。

## 2026-10-25 Startup 開機設定擴充

先唯讀盤點 `.26`：systemd 259.5-0ubuntu3.4，自有 unit／腳本／私有狀態皆不存在，rc-local.service 為 loaded／inactive／static、`/etc/rc.local` 不存在。沿用自有 oneshot 設計，沒有修改或建立 rc.local。

原生驗收在真正 systemd 與 ext4 完成，13 個紀錄階段全部通過：

| 項目 | 證據 |
|---|---|
| Save／Plan／Apply | Bash -n 與 systemd-analyze verify 成功；寫入及 enable 後 marker 仍不存在，沒有隱含執行 |
| 開機註冊 | 原生 multi-user.target.wants symlink 指向自有 unit，enable／disable 狀態一致 |
| 明確執行 | 真正 unit 執行一次，marker 證明 UID 0、cwd／HOME 為 /root；再次 start 回 NO_CHANGE、筆數不增加 |
| Active 下配置變更 | 編輯、disable、逐筆還原均保留原 InvocationID，不重跑或停止既有工作 |
| 首次 absent 還原 | 移除配置但保留 active/exited runtime 的 ownership 辨識；再次 Apply／Run 仍不重跑 |
| 外部衝突 | 真實額外 wants link／drop-in 在寫入前拒絕，外部 fixture bytes 未被改動 |
| 失敗補償 | enable 注入一次失敗，完整恢復檔案 bytes、metadata、boot flag，runtime 不變 |
| 腳本失敗 | exit 7 與 1 秒 timeout 分別記錄原生 exit-code／timeout；沒有自動重試或假還原其業務副作用 |
| 配置程序中斷 | 子程序在第一份腳本原子寫入後 os._exit(86)，新的 manager 由持久 journal 還原，未執行腳本 |
| 執行呼叫端消失 | start --no-block 後 os._exit(87)，unit 繼續完成；新的 manager 只確認原次 InvocationID／boot／時間／exit 證據，未第二次 start |

普通交易與結果 log 不含 canary 腳本本文；腳本及草稿維持 root 0700／0600、自有 unit 0644、公共 config 0755。原生 driver 對其他 `/etc` 檔案、SSH／IP／route、fstab／16 GiB swap inode／LV、Feather／BanIP／服務防火牆的快照前後一致。測後僅對本輪新建 nonce unit 做 fixture stop/reset，清除 marker，保存安全預設腳本與草稿：boot disabled、runtime inactive、沒有 pending。

完整 Linux root suite **343／343 通過、0 skip，366.599 秒**；57 個來源／測試／launcher 檔案 SHA256 前後一致，逐一吻合測試部署包 `b26211d3f6b7720093c36f38004a93040108393d4351a556f47bc224b8e3d286`。另含三種 terminal journal／crash 故障回歸，確認 Restore 補償後仍保有原交易的還原資格。

原生結束後，六頁限定 runtime、真正 CLI／Unix FastCGI SQL load_extension()、john Startup run 拒絕、dpkg audit／pip／來源 manifest 均通過。沒有執行 VM reboot；本節證明 boot 註冊與同一 unit 的手動觸發，不能冒稱完整 cold boot 驗證。

開發證據：`.root-dev/startup-native-report.json`、`startup-suite-final.json`、`startup_final.py.log`；VM 完整測試 log 為 root 私有 `/root/3wa-startup-suite.log`。執行資料不納入來源包。

真正 root SSH TTY 也完成 140×40／80×24 驗收：編輯器 Ctrl+左右逐詞移動而不換頁、離開編輯器後從按鈕 Ctrl+左右切頁；一次真實 bracketed paste 加入公開 comment，script／開機旗標／wait network／timeout 草稿在 Refresh 與切頁後保留。Apply、Run、Restore 檢閱全部 Escape；Run 顯示目前已套用的 SHA，沒有執行未套用草稿。UI 真正 Save draft 一次，Load current／saved／template 回填正確，再用第二次 Save 恢復原預設草稿；未使用失敗清理 fallback、未按 root-confirm，Ctrl+Q exit 0。

獨立 guard 的 19 項完整性檢查全部不變：dpkg／APT／PHP 原模組與設定／工具配方／Startup unit-script-owned-runtime／草稿 bytes 與 metadata／netplan／網路／boot ID／LV／swap／BanIP kernel／Feather／程式 SHA。journal **79→79**；只允許同 bytes 原子回存草稿的 inode 變動。27 個程式檔與部署來源相符。7 張真實終端 SVG、206 個觀測 state 與 JSON 報告保存在 `.root-dev/startup-tty-3e052f23/`，不納入來源包。

## 2026-10-25 Startup 真正重開機驗收

使用者另行授權以無害指令正常重開 `.26`、驗證開機執行與 SSH、測後還原。本輪沒有修改產品程式；以既有 StartupManager 的 Plan／Apply／Restore 操作自有 unit，未呼叫 Run once 或手動 start。

- 重開前唯讀基準：原空範本與草稿、boot disabled、runtime inactive、無 pending。私存原 Startup 檔案 bytes 雜湊／owner／mode／xattrs／開機 link，以及其他 `/etc`、靜態 IPv4／路由、16 GiB swap inode／容量、root LV、Feather 與程式雜湊。
- 測試腳本只將 nonce、新 boot ID、UID、cwd、HOME 寫入唯一 root 私有 marker，另輸出一行公開 token 至 systemd journal；timeout 30 秒、wait network On。Apply 交易 `20261025T091308Z-40aa4e1db35d` 完成後 unit 已 enabled，但仍 inactive、marker 不存在；原草稿完全未 Save 或修改。
- 以 `systemctl reboot --no-block` 正常重開。boot ID 從 `a1a95cc2-09cf-4e74-9c70-49202573875d` 變成 `a0d3aa3f-39ce-44f9-83b6-4b9a4ef7b8d4`；SSH 曾不可用，之後全新 pinned SSH transport 登入 root 成功。
- 開機後 marker 恰好一筆，UID 0、cwd／HOME `/root`，boot ID 與新開機一致。unit 為 active/exited、Result success、ExecMainCode 1／ExecMainStatus 0，InvocationID `568314cd5ad94864b285fd6bd72d5efb`。目前開機 journal 中同 token 恰好一筆，`_BOOT_ID` 與 `_SYSTEMD_INVOCATION_ID` 均相符；交易清單只有配置 Apply，沒有手動執行交易。
- Restore 精確恢復原 script／unit／owned／draft／boot link 的 bytes 雜湊與 metadata，boot disabled。產品 Restore 保留已結束 runtime；測試 teardown 另確認 MainPID／ControlPID 0、無 job、cgroup 無程序後，只 stop 本次已證明結束的 canary，再刪除唯一 marker。最終 runtime inactive、無 pending；交易數 **79→81**，只新增本次 Apply 與 Restore。

既有 BanIP 只有手動 runtime、沒有開機持久化，重開後集合與 ip/filter 規則依預期消失。測前另私存經嚴格 members() 驗證的原集合；測後僅在 owned.json 未變、set／temp set／ip-filter 皆不存在且 foreign SHA 相同時，重建原 ipset 與三條規則，先加入 lo／原 SSH ACCEPT，再加入 DROP。恢復後 filter／set／foreign SHA、規則次序與 24,851 筆均等於測前，verify_companion 通過；沒有重新下載來源、改持久化或 flush ruleset。服務來源防火牆仍維持 Off。

11 組受保護快照全部一致；IP 比對排除介面 index 與剩餘 lifetime 等重開可變欄位。最後另驗證 root 與 john 均以獨立新 SSH transport 登入同一新 boot；六頁限定服務 runtime、實際 CLI／Unix FastCGI SQL load_extension()、dpkg audit 均通過。這裡證明正常 VM reboot，沒有宣稱 Hyper-V 匯出備份已完成匯入或離線冷開機還原測試。

開發證據為 `.root-dev/startup-reboot-3c8f7f068b2beb14e9c40ea63731085f/` 的 baseline／prepared／reboot-request／boot-evidence／restored／final-report JSON，以及 `.root-dev/startup-reboot-ssh-return.json`、`startup-reboot-final-ssh.json`。VM 證據目錄為 root 私有 `/root/3wa-startup-reboot-3c8f7f068b2beb14e9c40ea63731085f/`；已刪除測試 marker，私有證據與既有配置備份保留，不納入來源包。

# Ubuntu 26.04 新機驗收

2026-10-29 實戰回饋：PHP 設定只管 FPM、既有 prefork／mod_php 沒有自動遷移、Time 頁不安裝 ntpdate、長安裝缺乏即時狀態，以及防火牆多來源／整份套用說明不足。公司主機無法由此環境連線；這輪先完成產品修正，改用獨立乾淨 VM 驗收。

目前狀態：**V3 在新 Ubuntu 26.04 VM 完整執行 574 項測試，551.243 秒，全部通過、無跳過**。PHP CLI／FPM 設定與 pool 覆寫回復、Apache prefork／mod_php 遷移與還原、loopback HTTP、FTP 被動傳輸、SSH keepalive、ntpdate 可用性、羽山版 PDO／官方 SpatiaLite 共 120,000 筆 workload，以及防火牆兩輪套用／回復已完成下列原生驗收。新 VM 正常重開後，固定 IP、279 份配置與模組紀錄、服務、PHP／HTTP／空間快測及 root／john 新 SSH 再次通過；未發布新 Release。

開發初期的 Windows 全套 523 項（跳過 30 項）及後續個別回歸，屬本機開發證據，不與原生 574 項相加。以下結果只適用本輪新 VM 與指定候選快照，不挪用舊 VM 的歷史成功結果。

## 環境基線

新 VM 主機名稱為 `ubuntu2604`，MAC 為 `00:15:5d:01:6a:05`，初始 DHCP 位址 `192.168.137.181/24`。初次盤點確認 Apache／PHP／vsftpd／ntpsec-ntpdate／SpatiaLite 尚未安裝，`/opt/3waServerSetting`、工具狀態與 `/etc/php` 均不存在。套件安裝後，V2／V3 沿用同一台 VM 重跑，沒有宣稱每一候選都重新安裝作業系統。

這台 VM 的 Netplan 與 `/run/systemd/network/zzzz-dracut-default.network` 共存，產品 Network 回報 `supported=false`、`Custom backend network files require separate integration.`。本輪另以私有一次性程序備份設定、建立 180 秒回復 timer，將 MAC／固定 IP 設為 `192.168.137.26/24`，並由新位址確認 gateway／DNS `192.168.137.1` 後停止 timer。這是獨立的主機準備操作，**不是產品 Network 套用成功**，也未移除 dracut backend。

## 受測開發候選

V3 為 `2026.10.29.001-dev`，基於 `2242ce2aba9027a27b4bddc7555c482914c61628` 的未提交工作樹，`dirty_worktree=true`、`signed=false`、`release_published=false`。包含 145 個 manifest 檔案項目與 9 份鎖定 wheels；實際隔離執行環境為 Python 3.14.4。

- 候選包：`3waServerSetting-20261029-dev-756a820e5662.tar.gz`。
- Archive SHA256：`46f459d95908e22dbea5f36fa500da714a590fb5f8812e41ef8640eedf4385a2`。
- `CANDIDATE.json` SHA256：`756a820e56625645655c5759ae57fba56403711cafa9f1538f0ee62467e24136`。
- VM 來源目錄：`/opt/3waServerSetting-candidate-20261029-756a820e5662/3waServerSetting-20261029-dev`。

候選 manifest 在打包時記錄原生驗收 `PENDING`；後續驗收不回寫受測 archive 或 manifest。本文是其後補記的驗證結果，未將基底 commit 冒充為已提交的變更內容，也未將未簽章測試包稱作公開 Release。

## 完整測試的失敗與修正

| 快照 | 原生結果 | 判定 |
|---|---|---|
| V2 `f41e58ecf4da` | 572 項、539.648 秒；1 failure、4 errors | 全部來自 FTP 舊配方測試 fixture 以 0644 建立私有狀態，觸發 `State input is not private to root`。整輪為失敗，不能將其餘 567 項當作全套通過。 |
| V3 `756a820e5662` | 574 項、551.243 秒；`OK`，無跳過 | Fixture 檔案改為 0600、目錄 0700，並以 POSIX mode／root ownership 斷言驗證；產品 `_trusted_file` 沒有放寬。V3 亦包含新增的 worker 路徑可讀性回歸。 |

V3 VM 完整 unittest log SHA256：`7a8b75c38aae8391994fb1a0081a4f3b2731ea2ccc9bc5c8b91b4d039d6ad4d1`。這是完整 log 的雜湊，不是截取尾端或外層 progress log 的雜湊。

## 已完成的原生服務驗收

| 項目 | 本輪證據與範圍 |
|---|---|
| PHP CLI／FPM | 同時套用兩個 `conf.d/zz-3wa-server-setup.ini`，自訂 `memory_limit=256M`。版本化 CLI 與真正 FPM Unix socket request 分別核對七個欄位、SQLite extensions 與載入的受管 ini；Apply、重複 No-op、還原原本不存在的兩份 drop-in、再 Apply 均通過。原生 CLI／FPM `php.ini` SHA256 前後一致。 |
| Apache handler | 驗收 fixture 明確建立 prefork＋mod_php；這是測試準備，非產品隱含操作。產品 Plan 丟棄後設定／journal 不變；Apply 遷移為 event＋FPM、重複 No-op、Restore 回 prefork＋mod_php、再 Apply 均通過，並核對原生 loaded modules。 |
| 真實 HTTP | 獨立 loopback HTTP fixture 實際取得 `apache2handler → fpm-fcgi → apache2handler → fpm-fcgi`，FPM 回應符合所選 PHP 值。這是驗收 driver 的額外 HTTP 證據；產品本身仍將 HTTP application 標示未驗。臨時 PHP endpoint 已刪除，其餘 webroot 檔案不變。 |
| FTP | 推薦 11 欄 Apply，啟動並設為開機啟動；nonce 測試帳號以 127.0.0.1 PASV 登入、上傳與下載一致。新檔案實測 0755，`/var/log/vsftpd.log` 含測試檔名；測試帳號、群組與 home／資料均已清除。僅驗 loopback，未宣稱外部網路、TLS 或防火牆穿越成功。 |
| SSH keepalive | Apply 後 `sshd -T` 為 `clientaliveinterval 60`、`tcpkeepalive yes`。重複 No-op 不 reload；核對本次 transaction 後還原原 drop-in，再 Apply 保留 60／yes。Driver 的保護斷言確認其他 SSH 全域值、主設定／認證／金鑰與既有 root／john 帳號記錄雜湊不變，daemon 身分與開機狀態不變；服務命令僅 show／reload。另在整機重開前後，分別建立 root／john 新實體 SSH，嚴格 host-key 核對均通過。 |
| ntpdate | `ntpsec-ntpdate` 已安裝，可信本機 `ntpdate` executable 與 help 命令可用。未執行校時，也未把指令可用當作 NTP synchronized 證據。 |
| 羽山版 PDO | 由產品實際編譯並啟用 CLI／FPM，回報 `FEATHER_APPLIED`；原有 nonce extension 的正／負例及兩個 SAPI probe 通過，官方 PDO SQLite `.so` 保留。 |
| 官方 SpatiaLite | PHP 8.5.4／SQLite 3.46.1／SpatiaLite 5.1.0；CLI 與 FPM 各執行 PDO、SQLite3 兩個 driver，每份 POINT／LINESTRING／POLYGON 各 10,000 筆，共四份 30,000 筆報告、總計 120,000 筆，全為 PASS。PDO 報告的實際類別是 `PDO`、loader 為 SQL `load_extension`；另完成獨立 RTree 正反例及 legacy-SQL 快速空間查詢。 |
| 安裝進度 | 原生 driver 已接收到實際 command／phase、elapsed 與 output bytes 事件。輸出量不是網路傳輸量；只有原生 APT `Fetched` 摘要才可標示 downloaded bytes。互動 UI 屬 headless 測試，這份紀錄未宣稱真 TTY 長安裝操作已驗收。 |

SpatiaLite 的主機準備是明確的驗收 fixture：建立 `/var/www/sqlite3_ext/mod_spatialite.so` alias 指向官方 `/usr/lib/x86_64-linux-gnu/mod_spatialite.so`，並在 CLI／FPM 的 `90-3wa-spatialite.ini` 設定 `sqlite3.extension_dir="/usr/lib/x86_64-linux-gnu"`。這些設定保留在 VM，備份紀錄為 `/var/backups/3wa-native-spatialite-af18d3a6c185464c`；**產品沒有自動建立 alias 或套用這兩份 SpatiaLite ini**。

Feather／SpatiaLite driver 142.2 秒完成，另確認官方 PDO／SpatiaLite `.so`、原生 CLI／FPM `php.ini` 與候選來源雜湊未被覆寫；SpatiaLite 準備未再修改 PDO activation aliases，FPM master 未變。暫時 FastCGI wrapper 已清除。PHP／Apache 與 Feather 報告均確認各自驗收期間 boot ID 不變；後續獨立的整機重開結果見下節。

## 驗收邊界

未另外建立「尚未整合的原生 event」首次遷移入口；單元測試與遷移後的 event No-op 不能替代此原生情境。SSH 的個別使用者 Match 值未逐項驗證。重開後 FTP 核對服務與設定持久化，沒有再建立測試帳號重做傳輸；完整傳輸證據來自重開前本輪測試。

## 防火牆與 PHP 負例

防火牆以 HTTP 80／443、MariaDB 3306 兩組服務測試，每組同時保存 `192.168.137.1/32` 與 `198.51.100.7/32`，再整份套用。原生 nft JSON 確認 INPUT policy 為 DROP、服務來源與埠吻合，並保留目前 Windows 來源的 SSH 22 救援及 established／related 規則。第一輪刻意不確認，120 秒 watchdog 獨立回復為 `FW_ROLLED_BACK`；第二輪從套用後新建立的實體 SSH transport 確認為 `CONFIRMED`，再還原為 `FW_RESTORED_INITIAL`。

最後另一條新 SSH 確認自有 table 不存在、active／persistent／saved 均 false、pending 為 null、boot unit disabled，原 draft 不存在的狀態已還原。前後 foreign table／NAT／FORWARD 雜湊與 forwarding sysctl 一致。第一輪有效期間，Windows 綁定來源 `192.168.137.1` 的 HTTP 請求實際取得 200；其他來源拒絕與獨立 established 流量命中計數未實測。

PHP 負例在 pool 暫設 `php_admin_value[memory_limit]=96M`，實測 CLI 256M／FPM 96M，再由產品套用 384M。產品回報 `ROLLED_BACK`，保留 `PHP FPM memory_limit` 與 `/etc/php/8.5/fpm/pool.d/www.conf` 的來源診斷，兩份 managed ini 與 saved recipe 在清除 fixture 前已回復。最後還原 pool 原始 bytes／metadata、reload，CLI／FPM 均恢復 256M，原生 php.ini 與候選原始碼不變。私有 receipt：`/var/backups/3wa-fresh26-fpm-override-28264f6cb17c4a2b/receipt.json`，SHA256 `56f9215a07f1f1fb3bd598fca6cfd6080fba77d5e8c24b955d2cda56be3c9b4c`。

Windows 僅追加 `.181` 已驗證 ECDSA 公鑰給 `.26`，保留 known_hosts 其他 bytes 並備份原檔；沒有 keyscan 或 accept-new。root、john 已分別以一般 `.26` 主機名稱、嚴格 host-key 檢查、無 alias／連線重用建立新 SSH transport，均驗證新 VM MAC `00:15:5d:01:6a:05`。

## 整機重開驗收

在無 pending transaction、防火牆已還原的前提下，對 MAC `00:15:5d:01:6a:05` 的新 VM 執行正常 `systemctl reboot`。Boot ID 由 `496ec737-2fff-446b-8f04-96e2064c620e` 變為 `1b395ea3-b458-4499-a993-f2fd7f13554e`。重開後 root／john 各自建立新的嚴格 SSH 連線；固定 `192.168.137.26/24`、gateway／DNS `192.168.137.1` 維持，Netplan 與 dracut fallback 雜湊不變。

279 份設定與模組紀錄、候選原始碼、服務的 active／boot 狀態、Apache event／FPM 模組與 PHP CLI／FPM 七個設定均和重開前一致；實際 HTTP 回應為 `fpm-fcgi`。Feather nonce、PDO／SQLite3 RTree 與 SpatiaLite 空間快測再次通過，沒有以重開後快測冒充重做 120,000 筆 workload。防火牆保持無自有 table、無 persistence／saved／pending，臨時 FTP 帳號／資料與 PHP probe 已清除。

私有 baseline `/root/3wa-fresh26-final-before.json` SHA256：`456e4d9b94a0a3251951469a240aa82c3a90e646efc95053ee310e4c313f6597`；重開後 `/root/3wa-fresh26-final-after.json` SHA256：`d44dfd4f1accf0775a0ad8ee0b36ba9cd86f05221deb7aa741290b19d0742c20`。第一次 baseline helper 因誤判 PHP 標準優先序符號連結而停止，當時尚未寫 baseline／修改設定；修正私有 helper 後重新完整建立 baseline，未放寬產品 guard。

## 開發證據

以下檔案留在忽略的 `.root-dev/`，不納入公開來源包：

- `fresh26-181-inventory.json`、`fresh26-181-network-read.log`、`fresh26-181-static-ip.log`、`fresh26-181-static-confirm2.log`：新機基線及獨立手動網路準備。
- `fresh26-181-unit-suite-v2.log`、`fresh26-181-unit-suite-v3.log`：完整測試結果；VM 完整 log 分別為 `/root/3wa-fresh26-unittest-20261029.log` 與 `/root/3wa-fresh26-unittest-20261029-v3.log`。
- `fresh26-181-php-apache-v3.log`：PHP／Apache 生命週期與真實 HTTP 報告。
- `fresh26-181-ftp-ssh-v3.log`：FTP／SSH／ntpdate 摘要。VM 私有完整報告為 `/root/3wa-fresh26-ftp-ssh-dbc6a6869fa20e15/report.json`，SHA256 `6bc5dd95d2b56b0dd1b6e483950c76a41936ce8c7c75e3d80ce59bd19d5f31f4`。
- `fresh26-181-feather-spatial-v3.log`：產品編譯／啟用、四份完整 geometry 報告、RTree／SQL 快測與明確的主機設定 fixture 紀錄。
- `fresh26-181-firewall-baseline.log`、`fresh26-181-firewall-r1-*.log`、`fresh26-181-firewall-r2-*.log`、`fresh26-181-firewall-final-status.log`、`fresh26-181-firewall-http.log`：基線、兩輪原生驗收與允許來源 HTTP 證據。
- `fresh26-181-fpm-override-negative.log`：真實 FPM pool 覆寫、產品拒絕／回復與 fixture 清理。
- `fresh26-known-hosts-applied.json`、`fresh26-normal-ssh-before-reboot.json`：僅追加既有主機公鑰及 root／john 一般新 SSH 連線。
- `fresh26-181-reboot-before-v2.log`、`fresh26-181-reboot-request.log`、`fresh26-181-reboot-after.log`、`fresh26-normal-ssh-after-reboot.json`：重開前基線、正常重開、原生持久化與新 SSH 證據。

2026-10-30 使用者授權推送與發布 `v2026.10.30.001`。發布採用上列候選相同的產品與測試原始碼，另補驗收紀錄、安裝指令與 Release 說明；正式包的身分以簽章 manifest 為準，不使用候選包的開發身分。公開資產與舊版更新發現另外核對，不以候選測試替代。

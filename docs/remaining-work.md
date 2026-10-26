# 目前尚未完成的項目

此清單是現有原始碼與驗收範圍的盤點，不會在啟動時自動變更主機。已完成的安裝／推薦設定、防火牆、Swap、LVM、BanIP、套件頁與羽山版 PDO SQLite 以各自驗收文件為準。

| 優先順序 | 後續工作 | 現況 |
|---|---|---|
| 先補 | 一般服務設定／安裝／APT 中斷交易的恢復入口 | 有 journal、失敗補償及 pending 阻擋；程序遭 kill 或主機中斷後，普通 root 交易缺少完整 UI／CLI recovery 操作。防火牆、Swap、LVM、羽山版各自恢復機制不能代替這個入口 |
| 功能 | Startup 相容模式 | 自有 systemd 腳本的設定／儲存／套用／執行／還原已提供；既有 rc.local 仍只觀察，不自動匯入或建立相容入口 |
| 功能 | FTP 完整架站 | 目前設定與 daemon 驗證；尚未建立帳號／chroot／TLS、驗證 PASV 實際傳檔 |
| 功能 | Samba 分享與帳號 | 目前全域設定與 testparm；尚未配置分享／密碼及 Windows 實際讀寫 |
| 功能 | 網站與 GIS 服務 | Apache 敏感檔案規則仍是候選；vhost／HTTPS、MapServer CGI／mapfile、XSendfile 路徑政策尚未由套件頁自動配置 |
| 維運 | Release 後續維護 | CI、簽章發行包、安裝器、明確更新與程式回退見[安裝操作](installation.md)；自動排程更新、簽章 key 輪替及舊版清理尚未提供 |
| 維運 | 備份與記錄保留 | 尚無交易備份／編譯產物／日誌的保留天數與容量清理介面 |
| 驗收 | Hyper-V 匯入還原開機 | 完整匯出與 SHA256 已核對；尚未真的匯入並開機驗證。`.26` 的 Startup 已於 2026-10-25 完成實際 VM reboot／開機執行／新 SSH 連線與測後還原驗證 |
| 已完成 | Network 正式版端到端流程 | Ubuntu 24.04 RC2 四階段原生驗收、Network TTY 寬窄畫面檢閱／取消、公開 `26.001` 五附件核對、公開 `25.001` 真正 TTY 更新及最終公開版固定 `.24` 新 SSH 確認均通過，見[Release 驗收](release-validation.md#20261026001) |
| 已完成 | Ubuntu 26.04 正式 `26.001` 更新後核對 | 公開 `25.001` 更新、root／john CLI、完整簽章 payload／ready 及原 98 檔案／81 筆交易狀態、boot、Swap、LV、BanIP、防火牆、Startup、Feather 唯讀保護核對均通過；最終 remote_writes=0 |
| 整合 | Ubuntu 26.04 額外 backend 網路來源 | `/run/systemd/network/zzzz-dracut-default.network` 觸發 Network 保守門檻；既有固定 `.26` 保留，未接管、刪除或套用。來源整合／遷移需獨立實作與驗證，本輪保留現況 |
| 已完成 | Ubuntu 22.04 原生候選與正式發行 | 原生 Python 3.10 簽章安裝／升版／CLI 回退、120 秒 Network 回復及固定 `.22` 新 SSH 確認通過。`002`／`003` 已公開，兩版各五附件完整核對通過；`003` CI 三平台各 469 項與 smoke 通過。原生完整 461 項屬 RC1，RC3 cloud-init reader 修正另做差異驗證，見[Ubuntu 22.04 驗收](ubuntu22-validation.md) |
| 已完成 | 三台公開版本升版 | 24.04／26.04 公開 `001 → 002 → 003` 與 22.04 `RC3 → 003`，共五次指定 tag 的 Check／Plan／Apply 全部通過，最終均為公開 `003`；新 root／john SSH、完整簽章與主機保護及三台最終純本機補核均通過，見[Release 驗收](release-validation.md)。26.04 保留本輪既有 BanIP unsupported 狀態，未恢復 kernel set 或宣稱 active |
| 驗收 | 預設 latest API 查詢 | 三台預設 CLI 查詢當次遇到 GitHub 匿名 API 限流 HTTP403／remaining0，未取得預設 `UP_TO_DATE`；公開發行、完整附件、官方 latest 下載三檔與指定 tag 升版均已通過，限流後的 CLI latest 查詢另行補驗 |
| 驗收 | 其他平台與壓力 | Ubuntu 22.04／24.04 的安裝／更新與網路功能已獨立驗收，其他服務寫入仍限 Ubuntu 26.04；多 PHP／pool、XFS／Btrfs、Swap 記憶體壓力未驗收 |

VPN／PPP／NAT／FORWARD、磁碟分割區與 PV 成長不在目前受管範圍。使用者選擇測試後關閉服務防火牆，以及建議 Swap 值只載入表單而不自動套用，都不是未完成項目。

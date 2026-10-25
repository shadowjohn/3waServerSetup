# 目前尚未完成的項目

此清單是現有原始碼與驗收範圍的盤點，不會在啟動時自動變更主機。已完成的安裝／推薦設定、防火牆、Swap、LVM、BanIP、套件頁與羽山版 PDO SQLite 以各自驗收文件為準。

| 優先順序 | 後續工作 | 現況 |
|---|---|---|
| 先補 | 一般服務設定／安裝／APT 中斷交易的恢復入口 | 有 journal、失敗補償及 pending 阻擋；程序遭 kill 或主機中斷後，普通 root 交易缺少完整 UI／CLI recovery 操作。防火牆、Swap、LVM、羽山版各自恢復機制不能代替這個入口 |
| 功能 | Startup 相容模式 | 自有 systemd 腳本的設定／儲存／套用／執行／還原已提供；既有 rc.local 仍只觀察，不自動匯入或建立相容入口 |
| 功能 | FTP 完整架站 | 目前設定與 daemon 驗證；尚未建立帳號／chroot／TLS、驗證 PASV 實際傳檔 |
| 功能 | Samba 分享與帳號 | 目前全域設定與 testparm；尚未配置分享／密碼及 Windows 實際讀寫 |
| 功能 | 網站與 GIS 服務 | Apache 敏感檔案規則仍是候選；vhost／HTTPS、MapServer CGI／mapfile、XSendfile 路徑政策尚未由套件頁自動配置 |
| 維運 | 版本發行與 updater | 仍為設計；尚無正式 tag／release／自更新切換 |
| 維運 | 備份與記錄保留 | 尚無交易備份／編譯產物／日誌的保留天數與容量清理介面 |
| 驗收 | Hyper-V 匯入還原開機 | 完整匯出與 SHA256 已核對；尚未真的匯入並開機驗證。`.26` 的 Startup 已於 2026-10-25 完成實際 VM reboot／開機執行／新 SSH 連線與測後還原驗證 |
| 驗收 | 其他平台與壓力 | Ubuntu 22.04／24.04、多 PHP／pool、XFS／Btrfs、Swap 記憶體壓力未驗收 |

VPN／PPP／NAT／FORWARD、磁碟分割區與 PV 成長不在目前受管範圍。使用者選擇測試後關閉服務防火牆，以及建議 Swap 值只載入表單而不自動套用，都不是未完成項目。

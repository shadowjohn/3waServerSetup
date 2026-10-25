# 手動更新 3wa BanIP

Firewall 頁的 BanIP 區可下載並執行一次 [3wa Linux 黑名單腳本](https://3wa.tw/myapache_banip/mybanip.php)。這是獨立的 IPv4 runtime 黑名單，不會啟用服務來源白名單，也不建立 cron 或開機排程。

實際腳本從網站的固定 HTTPS 下載 API 取得。工具保留已審來源原文，核對來源 SHA256；未知版本拒絕執行。執行版 `/root/my_ban_ips.sh` 為 root:root 0700，只將逐行 `tr`／`xargs` 改為等價的 Bash 內建修剪，避免大清單衍生數萬個程序。末行未換行的讀取處理由目前上游版本提供。

執行前列出必要套件；腳本在有時限的 systemd cgroup、私有 tmpfs 暫存目錄執行。第一筆 kernel 修改前，獨立 watchdog 已就緒；loopback 與實際 SSH 來源／埠的救援規則位於黑名單 DROP 前。執行後核對當次下載資料的完整筆數、集合 SHA256 及精確規則，再由新的實體 SSH 連線確認；逾時或失敗會還原受管規則與集合。

```sh
/opt/3waServerSetting/run.sh --banip-status
/opt/3waServerSetting/run.sh --banip-plan
/opt/3waServerSetting/run.sh --banip-update --confirm
# 依結果顯示的指令，從另一條新 SSH 連線確認 transaction-id。
/opt/3waServerSetting/run.sh --banip-confirm TRANSACTION_ID --confirm
```

來源與驗證狀態保存在 `/var/lib/3wa-server-setup/banip/`，交易與備份沿用 root 私有管理目錄。只有精確核對通過的 BanIP companion filter table 可以與服務防火牆共存；未知同名集合、外部修改或其他 filter hooks 會停止操作。IPv6 不受這份 IPv4 黑名單封鎖。

2026-10-25 在 `.26` 原生驗證：24,851 個 IP 全量一致；原 SSH 無法確認，新 SSH 確認成功；隔離 namespace 的真實黑名單來源被 DROP，正常來源 HTTP 200。測試後保留這次 runtime 黑名單，服務來源防火牆維持未啟用。沒有加入自動排程。

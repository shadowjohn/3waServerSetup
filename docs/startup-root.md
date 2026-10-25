# Startup 開機腳本

Root 的 Startup 頁管理工具自有的 Bash 腳本與 `threewa-startup.service`。預設範本只有 shebang、set -e 與說明，開機啟用預設 Off；不會自動搬入既有工作。

腳本編輯器內的 Ctrl+左右保留逐詞移動；移到按鈕焦點後，Ctrl+左右切換頁籤並保留草稿。

| 操作 | 行為 |
|---|---|
| Load template／current／saved | 載入範本、目前套用內容或已存草稿，只改表單 |
| Save draft | 保存 root 私有草稿，不更動 unit 或執行腳本 |
| Review & apply | 檢閱腳本與目標設定，通過 Bash／unit 語法檢查後寫入、daemon-reload 及 enable／disable；不執行 |
| Run once now | 獨立檢閱目前已套用的腳本 SHA，再啟動自有 unit；不執行尚未套用的編輯草稿 |
| Restore / recover | 還原上一筆配置及開機旗標；中斷交易先恢復，執行中斷則只核對原次結果，不自動重跑 |
| Refresh | 更新狀態，保留目前編輯草稿 |

選項為開機啟用、等待 network-online.target 與啟動逾時秒數（1..600，預設 120）。執行身分為 root，工作目錄 `/root`，PATH／HOME／locale 固定；使用乾淨環境、UMask 0077。腳本上限 65536 UTF-8 bytes，拒絕 NUL／BOM，貼入 CRLF 會轉 LF。

unit 使用 [systemd oneshot／RemainAfterExit](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)：同次開機已 active/exited 時，再按 Run once 回 NO_CHANGE，避免重複執行。此時編輯／套用新的內容不會重跑或停止原工作，新的腳本留待後續啟動。network-online 只控制順序，不代表網際網路服務已可用。

Apply、Restore 和失敗補償都不會 start、restart 或 stop 腳本。Restore 只還原配置與 boot enablement，已執行的指令及 background 工作不會因此撤銷。執行失敗／逾時不自動重試；呼叫端中斷後，利用 boot identity、InvocationID、啟動及退出證據確認原次結果，證據不足時保留待恢復狀態。

| 內容 | 路徑 |
|---|---|
| 已套用腳本，root 0700 | `/opt/3waServerSetting/config/startup/startup.sh` |
| 已存草稿，root 0600 | `/opt/3waServerSetting/config/startup/draft.json` |
| 自有 unit | `/etc/systemd/system/threewa-startup.service` |
| Ownership／缺檔後的 runtime 辨識 | `/var/lib/3wa-server-setup/startup/owned.json` |
| 配置備份 | `/opt/3waServerSetting/backups/<transaction-id>/startup.json` |

Startup 子目錄 root 0700，公共 config 目錄權限保留。普通 journal／結果 log 只存 hash、狀態與私有備份參照；腳本輸出留在原生 systemd journal，可用 `journalctl -u threewa-startup.service` 查看。未知同名 unit、drop-in、額外 wants／requires／alias links、外部改檔或正在切換的 unit 都會阻擋修改。既有 rc.local 只顯示狀態，不讀出內容、不修改、不建立相容啟動入口。

CLI 使用完整 JSON 設定檔，欄位為 `schema:1`、`script`、`enabled`、`wait_network`、`timeout_seconds`。status／plan 輸出省略腳本本文；明確 Load 才回傳已存本文。

```sh
/opt/3waServerSetting/run.sh --startup-status
/opt/3waServerSetting/run.sh --startup-load
/opt/3waServerSetting/run.sh --startup-plan --startup-config /root/startup.json
/opt/3waServerSetting/run.sh --startup-save --startup-config /root/startup.json --confirm
/opt/3waServerSetting/run.sh --startup-apply --startup-config /root/startup.json --confirm
/opt/3waServerSetting/run.sh --startup-run --confirm
/opt/3waServerSetting/run.sh --startup-restore --confirm
```

原生 VM、TTY 與完整測試證據見 [Ubuntu 26.04 root 驗收](ubuntu26-root-validation.md)。2026-10-25 已在 `.26` 實際正常重開：無害腳本在新 boot 執行一次，unit／journal／marker 證據吻合，root 與 john 均能建立新 SSH 連線。測後恢復原空範本、草稿與開機停用狀態，移除 marker；這次 reboot 不等同 Hyper-V 備份匯入開機驗證。

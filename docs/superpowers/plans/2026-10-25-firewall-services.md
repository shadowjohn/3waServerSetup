# 2026-10-25 防火牆來源規則與 SSH 救援

使用者已授權實作設定、儲存與套用，並在 `192.168.137.26` 實測；必須保留 SSH 管理入口。本輪延續既有 root 管理流程，不更換主要 checkout、不提交或 push。

## 實作契約

- 舊 schema 1 / `firewall_plan.py` 繼續是 IPv4 離線候選。新的 root 功能使用 schema 2，來源接受 IPv4、IPv6、CIDR 或明示 public；各服務獨立保存來源及埠範圍。
- 原生 nftables 自有 `inet threewa_services` INPUT table，涵蓋雙棧、未列入的入站服務預設拒絕。僅修改自有 table；不執行 `nat.sh`、不清空整份 ruleset、不改 OUTPUT、FORWARD、NAT、轉送或 SSH 設定。
- 預覽必須顯示整機 INPUT 作用範圍、受管服務、來源、實際 SSH 救援來源與埠。保留 loopback、既有連線及必要網路控制流量。不能把 IPv4 白名單誤當 IPv6 也受限制。
- 套用前檢查平台、權限、nft backend、其他防火牆管理者／未知 hooks、轉送及既有規則所有權；未知或不支援的組態停止並說明原因。
- 儲存草稿不套用。套用重新建立預覽指紋，語法檢查、保存舊自有 table，先啟動獨立 systemd 回復計時器，再原子替換自有 table。
- 尚待確認時顯示 `AWAITING_NEW_SSH`、交易 ID、期限與確認指令。原 SSH 工作階段或同一個 TUI 按鈕不能取消回復計時器。新 SSH 連線須核對真實 socket、sshd 工作階段及建立時間；通過後才提交開機持續設定。
- 回復程式與快照存於 root 私有目錄，不依賴目前 TUI、SSH 或 Python 執行緒存活。回復與確認須序列化，避免過期 watchdog 覆蓋已確認設定。開機只載入最後確認版本。

## 平行分工

1. model/compiler：來源／埠驗證、服務 preset、雙棧 nft render、純函式測試。
2. root engine：唯讀 preflight、草稿保存、交易／watchdog／新 SSH 確認、啟動載入及核心測試。
3. TUI/CLI：編輯各服務、Save/Load、review/apply/status、pending 指令、既有鍵盤／窄視窗回歸。
4. 整合：實際 VM 規則與來源檢查、失敗回復、新連線與跨來源 TCP／UDP／IPv6 驗收、文件及來源核對。

## 原生驗收

初始盤點已確認 nft 1.1.6、nf_tables backend、空 ruleset、UFW 設定 inactive、forwarding 皆 0；SSH `.1` → `.26:22`，VM 保有靜態 IP。服務含 IPv6 監聽，必須實測雙棧。

- 不確認的套用：退出原控制程序，獨立 watchdog 到時自動恢復原規則；確認新 SSH 可重連。
- 已確認的套用：使用新 SSH TCP 工作階段，驗證原工作階段不能冒充確認，並核對保存規則與開機 loader。
- 使用 VM 內暫時 namespace/veth 產生獨立來源，測不同服務來源不交叉放行、IPv6 不旁通、TCP/UDP 與埠範圍；不改 eth0 或主機預設路由，測試後清除。
- 驗證現有 HTTP、本機 DB socket、服務狀態、IP／路由及 SSH；完整原生測試與實際 TTY 寬／窄、review cancel、Ctrl+方向鍵。
- 實測期間不替使用者設定永久 DB 密碼；MariaDB 帳號／listener 與防火牆規則為獨立操作。

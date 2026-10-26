# Ubuntu 22.04 原生候選驗收

2026-10-26 在新 Ubuntu 22.04 VM 以原生 Python 3.10.12、systemd 249 與主網卡 `eth0` 驗證。初始 DHCP 為 `192.168.137.176/24`，gateway／DNS 為 `192.168.137.1`。以下原生驗收在私有 RC 階段完成，使用與正式發行相同的受信簽章金鑰；當時尚未公開 Release、push 或改動 main。其他服務設定寫入仍維持 Ubuntu 26.04-only，此 RC 階段未對既有 Ubuntu 24.04／26.04 執行設定寫入。

`v2026.10.26.003-rc.1` 固定於 `1e8bed861f1158783f75de38aeed91a6f378d79b`。真正簽章安裝、獨立版本 venv、九個鎖定 wheels 的離線安裝及 `pip check` 通過；包含 Textual 8.2.8 與支援 Python 3.10 的 platformdirs 4.12.4。該 commit 的完整原生測試共 461 項，耗時 829.282 秒，無 skip，另通過 headless startup／keyboard／resize smoke。

同一來源的 `v2026.10.26.003-rc.2` 已由簽章離線安裝器完成升版；真正產品 CLI 回退至 RC1，再由簽章安裝器恢復 RC2，全數通過。各階段以 root，以及 `runuser` 切換的 john 執行新 CLI 程序，版本、完整 commit 與實體版本目錄一致；真正 root／john 新 SSH 的最終驗證見下段。

`v2026.10.26.003-rc.3` 固定於 cloud-init YAML 解析修正 `247b81be1cdae2282a173c75058649aa2a896a69`，已完成簽章升版。此 commit 補跑 8 項 YAML、43 項 Network backend、6 項 Network CLI 與 headless smoke，並通過實際 Netplan 預檢。完整 461 項屬 RC1 的來源；RC3 的證據為上述差異驗證，未將兩輪相加宣稱同一 commit 已跑完整 469 項。

RC3 已完成真正 Network 基線及 120 秒到期回復：由 DHCP `.176` 暫時套用固定 `.22`，原 SSH 的確認遭拒；未從新連線確認，獨立 guard 到期後回復 DHCP，新 lease 為 `.109`。交易結果為 `NETWORK_ROLLED_BACK`，pending 為空。原 `/etc/netplan/50-cloud-init.yaml` 的 SHA256、inode 與 mode 保持一致；這是原始檔案未變的回復驗證。

最終固定 IP 與外部覆核已 PASS：由 DHCP `.109` 套用 `192.168.137.22/24`，gateway／DNS 均為 `192.168.137.1`，經新的目標 SSH transport 執行產品 CLI 確認，交易為 `APPLIED`／`NETWORK_CONFIRMED`。Netplan 來源已持久儲存，暫存 overlay 已移除、pending 為空；另儲存與實際設定一致、root 擁有且 mode0600 的草稿。root／john 均由新 SSH 連入 `.22`，版本與 RC3 完整 commit 一致。

原始基線的 12 個檔案中，除受管 Netplan 的預期變更，其餘 11 個保持一致；cloud-init 網路專用停用 marker 的 SHA256 亦保持一致，未停用整個 cloud-init。Ubuntu 24.04／26.04 的新 root SSH、boot ID、受保護檔案 stamp、Network 狀態與公開 `26.001` 版本均吻合本輪基線。`.26` 額外 backend 來源仍受保守門檻阻擋，未接管既有固定 IP。

RC 階段 `.22` boot ID 保持一致，尚未進行重新開機驗證。

橋接 `v2026.10.26.002` 與正式 `v2026.10.26.003` 已公開發布。正式 `003` 固定於 `beacfde733345c36f2f13563068d5dbb25dfa0f5`；main CI `40278552252` 與 Release `40279153410` 的 Ubuntu 22.04／24.04／26.04 各 469 項測試及 headless smoke 通過。兩版各五個公開附件均已匿名核對固定簽章、完整 payload／canonical identity、Git 原始位元組及九個鎖定 wheels；詳見 [Release 驗收](release-validation.md)。這些 CI／公開資產證據與前述 RC1 原生 461 項、RC3 差異驗證分開記錄。

Ubuntu 22.04 已經真正產品 CLI 指定 tag 的 Check／Plan／Apply，由 `003-rc.3` 更新至公開 `003`／完整 commit `beacfde733345c36f2f13563068d5dbb25dfa0f5`。新 root／john SSH、實體版本 venv、完整簽章 payload／ready 與公開 manifest／archive／installer 承諾核對通過。23 個關鍵物件及 7 筆原交易、boot、固定 `.22`、路由與 DNS 保持一致，只新增一筆 APPLIED release-update。Ubuntu 24.04／26.04 的公開 `001 → 002 → 003` 四次更新亦全部通過，詳見 [Release 驗收](release-validation.md)。

最終另由新 root／john SSH 完成純本機版本、完整簽章及主機保護補核，`.22` 為正式 `003`。官方 `/latest/download/` 的 installer、manifest 與簽章亦已匿名下載，hash 全部等同已驗公開 `003`。預設 latest CLI 查詢當次遇到 GitHub 匿名 API 額度耗盡（HTTP403、remaining0），未取得三台預設 `UP_TO_DATE` 結果；純本機補核與 latest 下載成功均不列為該 API 查詢通過的證據。

原始私有證據保留於開發 `.root-dev/`：`ubuntu22-assets-v2026.10.26.003-rc.1-native.log`、`ubuntu22-update-native.json`、`ubuntu22-rc3-native.json`、`ubuntu22-network-bfec26102622/state.json`、`ubuntu22-final-fleet-check.json`、`publish-vm22-update-20261026T063504Z-31b308718616.json` 與 `publish-vm22-check-20261026T064735Z-5fb090a1da43.json`，不納入發行包。

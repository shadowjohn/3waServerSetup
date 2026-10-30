# Network IPv6 與 dracut 整合驗收

2026-10-30 開發分支 `3waAI/network-ipv6`，基於 `a86cd84`，以開發進度保存；沒有合併 main 或發布 Release。**原生試用發生 SSH 中斷，尚未完成復原與開關驗收，不可視為可發布版本。**

## 已完成

- IPv6 `preserve`／`auto`／`disabled` 配方與 Network 選單；舊五欄草稿相容，讀取不改檔。
- 標準 dracut fallback 內容、vendor 模板、有效 Netplan backend 與交易依賴檢查；不刪除 fallback，不放行未知 backend。
- `recover` 在 networkd 不健康時跳過當前有效檔查詢，仍固定檔案清單／hash／metadata；確認流程保持嚴格。
- 第一候選在 Ubuntu 26.04 完整 603 項測試通過（575.284 秒，無跳過）。這次結果早於最後的 recovery 邊界修正。
- 第二候選原生 Network 106 項通過（28.170 秒，無跳過），包含上述 recovery 修正。
- 原生 backend 已回報 `supported: true`；原生 headless UI 確認網卡、IPv4／IPv6、IP、gateway、DNS、Save、Apply 全部可操作，沒有按下修改按鈕。

## 原生試用目前狀態

受測新 VM 為 `ubuntu2604`、MAC `00:15:5d:01:6a:05`，試用前固定 `192.168.137.26/24`、gateway／DNS `192.168.137.1`。已保存原 Netplan、配方與 387 個受保護設定項目，並核對現有簽章版本 `v2026.10.30.001` 的 94 個發布／metadata 檔案。

第一次 `off-apply` 原訂刻意不確認，以檢查 120 秒回復。呼叫途中 SSH 回報 `Connection reset`，後續 `.26` SSH 與 ARP 未恢復；DHCP 紀錄候選 `.22`、`.131` 的 SSH 也逾時。使用者從主控台回報 IP 變為 `.36`，兩次有界 SSH 查詢均逾時，Windows 鄰居表仍無對應 MAC；其後主控台 `ifconfig` 畫面已核對 `.36` 與這台 VM 的 MAC 一致。尚未讀到此次交易的最終 journal／回復服務紀錄，不能斷言 IPv6 已關閉、回復成功或失敗的根因。未重複 Apply。

Hyper-V CLI 因權限不足不能查詢；既有 VM 主控台顯示仍在執行、停在登入提示。已請使用者從主控台登入提供 `ip -br -4 address; ip -4 route; systemctl --no-pager --failed`。先復原及判斷原因，再繼續關閉逾時回復、關閉新 SSH 確認、重新開啟新 SSH 確認，最後還原原設定。

## 優先診斷線索（尚未確認命中）

先前 APT 原生紀錄顯示 VM 使用 Netplan `1.2-1ubuntu5.1`。受測 worker 的 `native_run` 對 Netplan 子程序設定 `umask=0077`；Canonical 的 [PR #614](https://github.com/canonical/netplan/pull/614) 已記錄同類問題：Netplan 1.2 生成的 networkd 檔案可因此變成 0600，而非預期的 root:systemd-network 0640，讓非 root 的 networkd 無法讀取。Apply 與 recover 使用同一路徑，這能解釋兩者都無法載入原設定的可能性；尚須實機 `stat`、networkd journal 與版本輸出才能確認本次根因，不能只依上游問題定案。

主控台可再取得 `stat -c '%a %U:%G %n' /run/systemd/network /run/systemd/network/*.network`、`systemctl show systemd-networkd -p User -p Group` 及 networkd journal。候選純函式重建已確認 off 只改 DHCPv6／RA／link-local，沒有刪除原 IPv4 `.26`／gateway／MAC。

失聯後已另補相容修正：只有 Netplan 子程序使用 `umask=0022`，其他子程序、guard unit 與私有檔案仍保留 0077／0600。這不是上述第二候選已驗證的內容；必須在 VM 復原後重新做原生檔案權限與套用／回復驗收。

## 本次交接

- 最新原始碼在 Windows 重新跑 Network 56 項（其中 11 項平台相關測試跳過）、root Network 43 項，均無失敗；`git diff --check` 通過。這不是 Ubuntu 原生套用結果。
- 使用者接著回報 DHCP 位址 `.19`、`.138`。已依授權對 `192.168.137.2–254` 共 253 個位址做一次有界 TCP 22 探測，沒有找到開放的 SSH；未對其他位址執行遠端指令。
- 先不要在失聯 VM 重跑 Network Apply。優先從 Hyper-V 主控台取得目前 IP／route、`/run/systemd/network` 與生成 `.network` 的權限、networkd journal，以及原交易 journal／guard unit 的狀態。
- 舊交易凍結的 worker 仍在 VM 的 `/opt/3waServerSetting-candidate-20261030-93acec4e9c17/3waServerSetting-20261030-network-dev`；更新 Git 原始碼不會替換舊交易 artifact。不可修改 artifact hash 或手動把 journal 改成成功。
- 復原原 `.26` 與原設定後，再以含 `umask` 修正的新候選檢查真實 Netplan 生成檔的 0640／networkd 可讀性、關閉後 120 秒回復、關閉／自動的新 SSH 確認，最後還原原配方與草稿。全部通過後才考慮發布。

## 候選與證據

- 第二候選 manifest：`93acec4e9c1725da5e3f7886d337572dc37a658c1a8ff9aeb3ecfc8e4a912139`；archive SHA256：`39af0c94548bcf1819f4b25e3acb79f1bd8a56bad980d272a34397b22b33191f`。這是未簽章開發包，不是更新器 Release。
- 原 Netplan SHA256：`4f0df4927ca42ece4941669f3cf592883d37b4403ac49bdf90a97c62d2d3baf1`；原始基線保存於 VM 的 `/root/3wa-network-ipv6-native-20261030-v2/`。
- 完整 603 項 log SHA256：`42c65738d4918ca6fd0511b1e450310faff5326372a0a4cf0a7d6dca32cd1694`；第二候選 Network log SHA256：`88d7166320e709374eefe132c050d3622142f2c85409e29718ac68ae976e964d`。
- 私有操作與待復原 checkpoint 保存於忽略的 `.root-dev/network-ipv6-current-state.json`；實機開關、回復、新 SSH 與原設定還原仍未驗收。

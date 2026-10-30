# 主網卡 DHCP／固定 IP 與 IPv6

開發分支的 IPv6 與 Ubuntu 26.04 dracut 整合仍待完成實機套用／回復驗收；目前狀態與已知問題見[驗收與交接紀錄](network-ipv6-validation.md)。

Network 頁獨立管理 Ubuntu 22.04／24.04／26.04 的主 Ethernet 網卡。可選 DHCP 或固定 IPv4 CIDR、gateway、最多三組 DNS，以及 IPv6 政策。套用目前需從 IPv4 SSH 進行，並用新連線確認；服務管理的 Ubuntu 26.04 寫入限制不因此放寬。

DHCP 模式下 IP／gateway 留空；三個 DNS 欄位全空時沿用 DHCP DNS，填入手動 DNS 時停用 DHCPv4 提供的 DNS。固定模式可填 `192.168.137.24/24`；gateway 為選填，同網段才接受。未填 DNS 不會擅自加入公共 DNS。

目前支援明確的單一主 Ethernet、networkd 與單一 Netplan 設定來源。複雜 route、bridge／bond／VLAN／Wi-Fi、NetworkManager、重複或不明來源會顯示停止原因；工具不把不支援的配置改成簡化版本。

## IPv6 政策

- **Preserve current IPv6**：保留目前 DHCPv6、RA、link-local、靜態 IPv6 與 IPv6 route。舊五欄草稿載入時補為 `preserve`，不會因升級或讀取草稿而改設定。
- **Enable IPv6 (automatic)**：啟用 DHCPv6、Router Advertisements 與 IPv6 link-local；保留原有 IPv4 link-local 與靜態 IPv6。是否取得全域 IPv6 或能連外，取決於網段上游。手動 DNS 會同步停用 DHCPv6 DNS，其他 DHCP override 保留；DHCP4／6 不對稱時停止並提示。
- **Disable IPv6 on this NIC**：停用 DHCPv6、RA 與 IPv6 link-local，保留 IPv4 link-local。如果已有靜態 IPv6、gateway6／IPv6 route 或手動 IPv6 DNS，會明確拒絕，避免默默刪除資料或留下無法使用的 resolver。

這是主網卡的 Netplan 政策，不編輯全域 sysctl、GRUB、loopback 或其他網卡。套用與確認都會核對實際 IPv6 位址／路由；關閉時仍有 IPv6 殘留會失敗回復，開啟則確認 kernel 允許 IPv6 且 link-local 已就緒，不將 link-local 當作 Internet 連線成功。原理見 [Netplan IPv6 關閉範例](https://netplan.readthedocs.io/en/stable/netplan-tutorial/#editing-netplan-yaml-files-to-disable-ipv6)與 [YAML 設定](https://netplan.readthedocs.io/en/stable/netplan-yaml/)。

## Ubuntu 26.04 的 dracut fallback

部分安裝會保留 `/run/systemd/network/zzzz-dracut-default.network`。先前版本把它當作自訂 backend，因而回報 `Custom backend network files require separate integration.` 並停用 Network 編輯。

現在只接受內容完全符合已知官方模板、vendor 模板／產生器受 root 控制，而且 `networkctl` 證實目前網卡已由較前順位的 Netplan `.network` 接管的標準 fallback。原檔不刪除、不覆寫；backend 的檔案清單、內容及 metadata 都納入交易依賴，試用期間新增自訂檔、drop-in 或外部改動會停止確認。未知設定與實際仍由 dracut 管理的網卡仍會拒絕。

來源：[dracut 標準 fallback](https://github.com/dracut-ng/dracut/commit/da3353f0)、[systemd network 設定排序](https://www.freedesktop.org/software/systemd/man/latest/systemd.network.html)。

執行中的外部 DHCP／網路 client（含手動啟動的 dhcpcd、dhclient、udhcpc、ConnMan）會阻擋套用；工具不自行停止它們。預覽及獨立套用／確認 worker 都會檢查，避免另一個 client 在固定 IP 生效後又加回 DHCP。這項程序檢查不涵蓋尚未啟動的其他開機網路服務。

## 操作

1. 選網卡、DHCP／Static 與 IPv6 政策，編輯 IP、gateway、DNS。Refresh 只更新觀測，不覆蓋正在編輯的草稿。
2. Save draft 只儲存工具草稿；Load 可重新載入。Apply 另顯示完整設定、來源變更、目標位址與 120 秒回復期限。
3. 套用先使用 `/run/netplan/` 下與原設定同名的暫存候選，原 `/etc/netplan/` 暫不改寫。獨立 systemd guard 負責到期回復，原 SSH 或 UI 消失不會取消保護。
4. 重新 SSH 連到目標新 IP，執行畫面提供的 `--network-confirm TRANSACTION_ID --confirm`。後端驗證真正的新 SSH TCP、管理來源、目標 IP 與網卡狀態後，才持久儲存。

原 SSH、ControlMaster 的新 channel，以及連到殘留舊 IP 的連線都不能確認。TUI 不提供繞過新連線驗證的確認按鈕。未確認就逾時會移除自有暫存候選並重新套用原設定；原設定是 DHCP 時，回復後的 lease IP 可能改變。

換 IP 可能讓原 SSH 在 Apply 回傳前就斷線。此時直接連到新 IP，先執行 `--network-status`，從 `pending.confirmation_command` 取得該次確認指令；不要重複 Apply。獨立回復服務不依賴原終端仍然存活。

cloud-init 網路管理必須已停用，或主機已有可信的 `cloud-init.disabled`；仍由 cloud-init 管理或來源不明時，工具會停止並顯示原因。未知外部修改會保持待處理並停止覆寫。

Ubuntu 安裝器產生的主機若仍啟用 cloud-init 網路重新產生，確認要自行管理這張網卡後，可由 root 新增 `/etc/cloud/cloud.cfg.d/99-disable-network-config.cfg`，內容如下。這只停用網路重新產生，保留 cloud-init 其他模組；不要覆蓋既有檔案。Network 的儲存／套用不會代替這項接管準備。

```yaml
network: {config: disabled}
```

cloud-init 檢查只解析網路相關設定，預設 logging 的 YAML alias 可以保留；網路設定中的 alias、重複鍵與 merge 仍會停止。Netplan 本身維持拒絕 alias 的限制。

## CLI

以 root 使用工具；一般帳號只能查詢狀態。

```sh
/opt/3waServerSetting/run.sh --network-status
/opt/3waServerSetting/run.sh --network-load
/opt/3waServerSetting/run.sh --network-plan --network-config /root/network.json
/opt/3waServerSetting/run.sh --network-save --network-config /root/network.json --confirm
/opt/3waServerSetting/run.sh --network-apply --network-config /root/network.json --confirm
# 下列確認必須從連到新 IP 的全新 SSH 執行：
/opt/3waServerSetting/run.sh --network-confirm TRANSACTION_ID --confirm
# 發生中斷時，先檢查狀態，再明確恢復唯一待處理交易：
/opt/3waServerSetting/run.sh --network-recover --confirm
```

固定 IP 的設定檔範例：

```json
{
  "interface": "eth0",
  "mode": "static",
  "address": "192.168.137.24/24",
  "gateway": "192.168.137.1",
  "dns": ["192.168.137.1"],
  "ipv6": "preserve"
}
```

草稿位於 `/opt/3waServerSetting/config/network/draft.json`，不寫入 `/root/nat.sh`。網路交易使用共用 root 鎖；待確認期間其他主機修改與程式更新都會停止。程式回退只切換程式，不恢復已確認的 IP 設定。

已完成與待補的原生驗收見 [Release／安裝更新驗收](release-validation.md#20261026001)。Ubuntu 24.04 的 Network 驗證不放寬其他服務的 Ubuntu 26.04 寫入限制；語法與 unit test 不代替實際 SSH／網路驗證。

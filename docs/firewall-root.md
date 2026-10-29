# 防火牆：各服務來源、儲存及套用

Ubuntu 26.04 的 root 模式使用 nftables 自有 `inet threewa_services` table，管理主機 INPUT 的 IPv4 與 IPv6。未列入或停用的服務不接受新入站連線；loopback、既有／相關連線、必要網路控制與 SSH 救援規則保留。

本功能不改 NAT、FORWARD、OUTPUT、虛擬機網路或 sshd。若有其他有效防火牆管理者、未知 hooks 或轉送環境，套用會停止，不能用本頁推定 Docker 發布的服務也受保護。舊 `firewall_plan.py` 的 schema 1 與 `nat.sh` 仍為獨立離線工具。

## 使用

在 `/opt/3waServerSetting/run.sh` 的 Firewall 頁，選服務、修改埠及來源，再更新該服務。HTTP/HTTPS、SSH、FTP、FTP 被動埠、SMB 與 MariaDB 各自保有清單；自訂項目可指定 TCP／UDP 及埠範圍。

來源欄使用**半形逗號 `,`** 分隔。例如同一個服務允許內網和一個外部位址，可在同一欄填：

```text
192.168.0.0/16,111.246.98.67/32
```

逗號前後可留空白；空白本身、分號 `;`、全形逗號 `，` 都不是分隔符號。這兩個來源屬於同一份服務規則，**不用每填一個 IP 就 Apply**。畫面會持續顯示來源範例及編輯步驟，填值後也不會消失。

| 來源 | 意義 |
|---|---|
| `192.168.137.1` | 單一 IPv4 位址 |
| `192.168.137.0/24` | 指定 IPv4 網段 |
| `2001:db8:1234::/64` | 指定 IPv6 網段，範例位址須自行換掉 |
| `0.0.0.0/0` | 所有 IPv4 來源 |
| `::/0` | 所有 IPv6 來源 |
| `public` | 所有 IPv4 與 IPv6 來源 |

限制來源模式不可留下空白；CIDR 必須使用網段位址，例如使用 `192.168.0.0/16`，而不是 `192.168.1.5/16`。多個服務不可重疊使用同一 protocol／port，避免一個服務的較寬來源誤放行另一個服務。

建議編輯順序：

1. 若這台主機已有儲存配方，先按 **Load saved**。程式啟動時不會自動載入；畫面會提示 saved recipe **not loaded**，避免把尚未載入的空白草稿當成目前規則。
2. 選服務、填完所有來源與埠、開啟該服務，再按 **Update service draft**。此時只更新畫面記憶體，不改防火牆。
3. 換下一個服務，重複編輯與 **Update service draft**。完成全部服務後，按 **Save recipe** 保存整份配方。
4. 按 **Preview effective rules** 檢查所有服務、來源及 SSH 救援，再按一次 **Review & apply** 套用整份規則。
5. 套用後從新的 SSH 連線執行畫面提供的確認指令，確認成功才會永久生效。詳見下一節。

**Apply 會替換這個程式管理的整份服務規則，不是向目前防火牆追加單筆。** 未列入或停用的服務將拒絕新連線；因此在既有主機上只編輯一個服務前，要先載入並檢查其他服務是否也在草稿內。

Save 只把 root 私有 JSON 草稿保存在 `/opt/3waServerSetting/config/firewall/draft.json`，不更動 kernel 或開機規則，也不產生 `/root/nat.sh`。Load saved 回填的是這份草稿，未必等同正在生效的規則；套用前仍須檢閱。MariaDB 的 listener 與 root@% 開關各自操作；防火牆放行 3306 不會替你開啟資料庫外連。

## 預設封鎖，再開放所需服務

每份套用規則本來就是 IPv4／IPv6 INPUT 預設 DROP，再加入選定服務的允許清單。可以先在草稿中整理好需要的全部開放規則，最後一次套用；不必先套用一次「全部封鎖」。規則以同一次 nftables 交易更新，不會先清空再逐筆加入。

**Deny services draft** 會把所有服務設成停用，只修改草稿。若想從封鎖狀態重建清單，可先按它，再啟用所需服務、逐項 **Update service draft**，最後保存及套用。

若確實要先進入「服務全部封鎖」的狀態，按 **Deny services draft** 後，仍須 **Review & apply**，並從新 SSH 連線確認；接著再編輯要開放的服務並套用第二次。這裡的「全部封鎖」仍保留目前來源的 SSH 救援、既有／相關連線、loopback 及必要網路控制，並不會立即斷開原有連線，也不是封鎖整台主機所有封包。

## SSH 保護與確認

預覽顯示從實際 SSH socket／工作階段辨識的來源與伺服器埠；不採用預設 22 或文件示例代替證據。這個來源會保留救援規則，不會因服務列的 SSH 開關而移除。

確認套用時，程式先啟動 root 私有、由 systemd 監督的獨立 watchdog，再原子替換自有 table。TUI 顯示 `AWAITING_NEW_SSH`、交易 ID 與期限，此時尚未提交開機規則。

請從另一個終端建立新的 SSH TCP 連線，再執行畫面提供的確認指令：

```sh
/opt/3waServerSetting/run.sh --firewall-confirm TRANSACTION_ID --confirm
```

原工作階段或 SSH ControlMaster 的舊 TCP 連線不能確認。程式核對真實 transport inode、來源、埠及建立時間，成功後才保存為開機規則。到期、TUI 退出或原 SSH 中斷不會解除回復保護；未確認會恢復先前自有規則。

```sh
/opt/3waServerSetting/run.sh --firewall-status
/opt/3waServerSetting/run.sh --firewall-plan --config /root/firewall.json
/opt/3waServerSetting/run.sh --firewall-save --config /root/firewall.json --confirm
/opt/3waServerSetting/run.sh --firewall-apply --config /root/firewall.json --confirm
/opt/3waServerSetting/run.sh --firewall-rollback TRANSACTION_ID --confirm
```

設定檔須為 root 控制、不可 group/world write 的 regular file；也可明示 `--config -` 從有界 stdin 讀取。範例：

```json
{
  "schema": 2,
  "services": [
    {"id":"http","enabled":true,"protocol":"tcp","ports":[80,443],"sources":["public"]},
    {"id":"mariadb","enabled":true,"protocol":"tcp","ports":[3306],"sources":["192.168.137.1/32"]}
  ]
}
```

此範例會拒絕其他新入站服務，SSH 救援與固定網路例外仍存在；先檢閱實際計畫再套用。已確認的最近交易可依 ID 還原上一版，逐筆回到首次安裝前的未啟用狀態。若目前 SSH 來源／埠不在上一版的救援規則內，會阻擋還原，避免失去管理入口。手動外部變更也會阻擋覆寫，需先檢查 journal。

目前套用必須在可核對真實 transport 的原生 SSH 工作階段執行；純 console 可保存草稿，但不能用環境變數或假定來源代替 SSH 證明。

## 實測狀態

原生測試記錄完成後列於 [Ubuntu 26.04 root 驗收](ubuntu26-root-validation.md)。使用者指定 `.26` 測試後還原原先未啟用防火牆的狀態；永久啟用仍由使用者選定服務來源並套用。

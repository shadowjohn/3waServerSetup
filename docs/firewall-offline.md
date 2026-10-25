# nat.sh 原檔審查與離線候選工具

2026-10-25 新增的主機套用功能使用獨立 schema 2／nftables，見[防火牆來源與 SSH 保護](firewall-root.md)。本頁仍只描述舊 schema 1 的 IPv4 離線工具，不會產生或執行 `/root/nat.sh`。

2026-10-24透過Library支援的唯讀文字介面完整讀取原附件；未執行原腳本，原文／真實IP未放入repo。本頁只記通用行為。

## 第一版已確認範圍

只做IPv4服務連接埠入站候選，不做VPN、PPP、NAT或FORWARD管理，不遷移或刪除既有路由功能。ping／MQTT／MQTTS專用項目延後；自訂規則仍可填任意有效TCP／UDP port。本輪不是TUI實作，沒有GUI或主機套用功能。

所有服務範本的enabled均為false；原schema1省略enabled的既有規則仍視為明確啟用，保留相容，不將既有使用者設定默默停用。UI未來新增規則一律顯式false，操作者選擇啟用後才加入候選。sources=["public"]只允許該條規則的protocol及目的port／範圍，不放行整台主機；public不可與限定來源混填。指定IP正規化為/32，CIDR須無host bits，空白／空清單／非法IP／IPv6一律拒絕。port接受1..65535整數或遞增start-end範圍，拒絕0、超界、反向及命令字串。

| 常用項目 | 範本與輔助說明 |
|---|---|
| HTTP／HTTPS | TCP80／443，預填public但預設停用，可改限定來源 |
| SSH | TCP22僅常用值，可改實際埠／來源；獨立ssh_rescue仍必填，不能從服務勾選推定已防鎖死 |
| MariaDB | TCP3306，預填文件來源，可改；開埠不建立DB遠端帳號或改listen |
| FTP control | TCP21常用值，需與實際listen_port一致 |
| FTP active | 20僅說明：伺服器主動連至客戶端時來源埠受connect_from_port_20／ftp_data_port控制，不自動開入站20 |
| FTP PASV | 顯式可改範圍60000–60100，僅示例；不能將6xxxx解讀為全部高埠。與pasv_min_port／pasv_max_port及control來源政策一致 |
| Samba現代SMB | 範本只列TCP445，預設停用；實際server listen另探測 |

舊式NetBIOS的TCP139（SMB framing）、UDP137／138（名稱／datagram）依既有客戶端／nmbd需求明確選用，可由自訂規則加入，不隨Samba445自動打開。不是本版AD DC／網域控制器全埠方案。[Samba官方transport](https://www.samba.org/samba/docs/current/man-html/smb.conf.5.html)、[nmbd](https://www.samba.org/samba/docs/current/man-html/nmbd.8.html)

舊式UDP137／138與TCP139用途亦核對[Samba歷史官方埠說明](https://www.samba.org/samba/history/samba-2.2.8.html)；它是舊相容用途證據，不作新版本預開全部埠的理由。

FTP輔助檢查目前為文件契約：離線產生器驗證port範圍與來源，但未連線讀vsftpd、也未自動比對兩份獨立rule是否配對；範本control／PASV使用同一來源，正式UI／套用前必須顯示兩者差異並要求一致或另行明確政策確認。NAT後的PASV公開位址與路由器轉發只提示，不自動設定；僅開21不能證明目錄列舉／傳輸成功。[vsftpd官方設定](https://security.appspot.com/vsftpd/vsftpd_conf.html)

未列新連線由候選最後DROP處理，loopback與SSH救援為顯式例外，ESTABLISHED/RELATED保留；黑名單仍在一般連線允許之前。本版沒有ping開關或所有必要ICMP完整政策承諾，IPv6不支援且不受保護，不能宣稱全機完整網路防護。實際SSH限時回復／第二連線、Linux規則語義與掛接都仍待VM驗證，離線產生不等於已防鎖死。

## 原檔與意圖差異

- filter表全域-F/-X/-Z，INPUT DROP、OUTPUT ACCEPT、FORWARD ACCEPT；會刪除其他工具filter規則。nat清空行被註解，後續MASQUERADE append會在重跑時累積。
- 反覆-I INPUT讓後寫規則排到前面，不能按檔案行序理解優先度。ESTABLISHED/RELATED位於後段且重複，可能被較早的port DROP攔住；沒有一般-i lo允許，僅零散loopback來源設定。部分CIDR有host bits，實際可能涵蓋比操作者以為更大的網段，不能靜默沿用。
- SSH是特定TCP port及來源白名單，沒有限時救援或第二連線確認。HTTP/HTTPS及許多其他應用埠公開，原實作遠超過使用者現在說的80/443公開意圖；逐項選擇遷移，不默默全部公開。
- FTP control來源受限，但被動高埠範圍公開；20／21不能代表完整FTP可用。應與vsftpd實際被動範圍及來源政策匹配。
- 僅IPv4 iptables；無IPv6政策。沒有可執行的ipset黑名單整合；尾端黑名單呼叫為註解，無法據此宣稱已封鎖。
- 會修改ip_forward等sysctl並載入舊conntrack/NAT helper，含PPP forwarding、GRE／ESP／AH與VPN相關port及MASQUERADE；這是實際路由／NAT功能，不能當單純架站INPUT規則刪除。附件未含完整持久化／開機註冊，無法確認reboot會套用。

**已決定：**第一版不管理原VPN／PPP路由與NAT，亦不自動遷移舊公開應用埠。只產生本頁列出的入站候選，不接管原規則、不移除任何舊功能；部署在仍有路由用途的主機時，不能拿候選替換原整機腳本。

## 已交付範圍

nat.sh為離線子工具，不是主TUI launcher；部署位置仍設計為/opt/3waServerSetting/nat.sh，尚未部署。firewall_plan.py只讀指定JSON、驗證並輸出自有THREEWA_INPUT4 chain候選；無subprocess、無apply、無主機探測或寫入。Windows直接執行Python即可，Linux可由sh啟動。

```sh
python3 firewall_plan.py --config config/firewall.example.json --validate
sh nat.sh --config /opt/3waServerSetting/config/firewall.local.json --render
python3 -m unittest discover -s tests -p 'test_firewall_plan.py'
```

範本的來源全部為文件保留網段，不是可直接部署的主機政策。欄位為schema、blacklist_set（已審查IPv4 ipset名稱）、ssh_rescue（實際port與明確CIDR）及rules（protocol、ports、sources）；public明列為公開，其他來源使用標準IPv4 CIDR，host bits不符即拒絕。只接受tcp／udp與有效port／範圍，不自動解讀GRE/VPN或IPv6。既有私人config由部署管理，禁止Git追蹤；範本只作格式示意，不是建議公開FTP／DB。

候選順序：loopback → 指定SSH救援 → IPv4黑名單DROP → INVALID DROP → ESTABLISHED/RELATED → port/source允許 → DROP。SSH救援與loopback是黑名單的明確例外；一般公開服務即使既有連線也先受黑名單判定。救援來源不得0.0.0.0/0，但仍須操作者確認足夠窄、可達且不是範例值；程式不從SSH_CONNECTION猜來源。黑名單set本身不由工具下載／建立，失效／不存在時不可套用。

**候選尚未掛接INPUT，不代表預設拒絕已在主機生效。** 本工具只輸出文字，所以本次沒有變更任何主機規則；候選不是安全可直接restore的增量檔，即使不含-F/-X，預設iptables-restore仍可能清除既有filter規則，禁止直接導入。實際部署需先驗證backend、chain名稱衝突、現有管理者、黑名單set、INPUT掛接優先順序、第二SSH連線與獨立限時回復，再決定持久化與重開機策略。候選語法／核心語義尚未經Linux parser或VM實測；離線字串／輸入測試通過不等於原子套用或自動復原已完成。

輸入上限：JSON1MiB、256個rule、每rule128個port／64個source，展開去重的allow規則最多4096；超過即拒絕，避免小型配置造成大量輸出。救援來源同為最多64筆。沒有預設執行或安裝模式。

後續VM必測：來源允許／拒絕、SSH黑名單例外與其他blacklisted來源、既有連線優先、FTP被動範圍、既有UFW/nft/Docker衝突、重跑掛接唯一、黑名單更新失敗、watchdog及reboot。IPv6、容器與轉送流量需顯式顯示未涵蓋，路由／NAT不能在驗收前宣稱遷移完成。

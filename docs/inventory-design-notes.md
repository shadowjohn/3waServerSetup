# 唯讀盤點轉入的共用配方與驗證契約

2026-10-24僅讀指定本機盤點報告後整理；不複製完整主機盤點、IP、帳號、後端與秘密。來源是掛載檔案，不是Linux runtime證據。Ubuntu22.04.5、Apache event＋PHP8.1 FPM是該份配置線索，不是套件版本／服務載入／其他平台通過證明。enabled項目與available檔的實際連結、載入順序仍須核對。

## MapServer精確判斷

報告所讀配置中，HTTP與HTTPS均有CGI入口且CGI配置含數字map正則與loopback Require ip；HTTPS的FastCGI入口亦見loopback限制，HTTP FastCGI對應location未見同等Require。這是**已讀區塊的策略不對稱**，不是HTTP FastCGI已對外開放的證明：尚未確認完整include／授權合併、enabled真實目標、其他Directory／Location限制、監聽、代理與runtime。HTTPS另有預設map入口，需作獨立路徑檢查，不能只驗/cgi-bin/mapserv。

加入驗收矩陣：HTTP／HTTPS×CGI／FastCGI／預設map入口×本機IPv4／IPv6／外部核准來源；確認Host／SNI、handler、mapfile模式與實際授權，外部拒絕必須辨識是Apache授權而非應用錯誤。本機代理入口另測，不把loopback下游請求當原始客戶端可信。ModSecurity廣泛rule移除須列diff與目的，不能複製為新站預設。

## 防火牆與banip順序精確判斷

報告中的主機nat腳本會flush filter/nat及自訂chain；banip程式使用ipset暫存／swap，並在INPUT檢查／插入set DROP。若先banip插入INPUT規則、再執行會清空filter的nat腳本，**INPUT連接規則可能被移除，即使ipset資料仍存在**；若之後banip又執行，可能重新插入，但是否／何時發生與插入位置未驗證。不能以IP set存在或排程readme宣稱黑名單持續有效，也不能斷言目前已失效或外露。

使用者先前附件版本的nat清空行為與主機報告不同：附件的nat-table flush行被註解，報告的主機版本描述會清nat；兩個來源不能視為相同revision。部署前需由授權runtime比對當前腳本revision、iptables-save規則／順序與set掛接、backend及實際開機／排程順序。現有連線／SSH救援／黑名單優先度依設計明確驗收；重新執行nat或banip後都再檢查連接與去重。原VPN／PPP／NAT遷移仍未核准，不因本次觀察而刪除或套用。

## 其他共用配方（保留選配與容量決策）

- banner讀完include後計算有效來源：主檔Off可蓋早期On，security2條件Full可能蓋Prod；是否品牌覆寫成功要以runtime header／body驗證，不能只看檔案。
- FPM／CLI／舊apache2 SAPI限額分列，apache2 php.ini存在不證明mod_php使用中。目標FPM配置與socket、pool及runtime值獨立驗收；不照抄無限CLI記憶體、大上傳／timeout、worker數或服務MemoryMax。
- 可選安全基底：expose_php Off、TraceEnable Off、明確banner、拒絕秘密／版本控制檔及指定上傳目錄禁腳本；short_open_tag是3WA舊程式相容選項。未知上傳目錄不做全站遞迴改寫。
- XSendFile白名單／大範圍cache／長timeout為主機用途設定，尤其暫存目錄不得當通用信任範圍；需逐站核對敏感內容與必要讀取。
- MariaDB慢查詢路徑與門檻存在不表示enabled；bind註解不等於localhost限定。probe／runtime與持久配置各自驗證，容量值不直接沿用。
- FTP TLS enabled不等於強制加密；被動埠與來源政策要一致。Samba client min protocol不代表server最低協定；guest／valid users／force user交互需實測，不沿用可寫guest配方。
- rc.local內swap啟用與fstab可不同，避免新增第二份；有swapfile或UUID根掛載不足以判斷活動swap／LVM。NTP wants名稱與空設定不代表已同步。screen應用工作只列主機自有選配，不移植到通用開機範本。

## 不可擴充的證據限制

報告明列gaussian-splat配置及root crontab讀取／列舉遭拒；本次未重試、未讀相近路徑或繞過，仍列無法判斷。root排程未知，其他cron/readme或timer名稱不能補成已安裝排程。未重新讀W或其他磁碟，未執行HTTP、SQL、Linux服務／防火牆命令；後續任何runtime檢查須在對應主機獲得授權。

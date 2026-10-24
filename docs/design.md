# 3waServerSetup 設計提案

狀態：待羽山核准；2026-10-24。這份文件只定義設計，沒有授權開始實作或操作 Linux。

## 目標、選型與非目標

從 Git 取得工具後，由 root 在 Linux 終端／SSH 啟動，Tab、Shift+Tab 移動焦點，Enter 執行，方向鍵切換有焦點的分頁列。使用 Python Textual 的 TabbedContent／TabPane 做真正分頁；小螢幕可捲動內容及分頁列，固定顯示目前主機、頁名與待套用數。每頁只顯示常用欄位與目前有效值，保留進階設定給既有系統管理方式。預設不更動任何值。

| 路線 | 優點 | 成本／決定 |
|---|---|---|
| Python Textual（已選） | 真正分頁、狀態與差異預覽、可測鍵盤流程 | 需要 Python 與受控依賴；日後鎖定已驗證版本 |
| shell + dialog（替代） | 終端普及、依賴較少 | 多為選單切換，複雜狀態與回復較難維護；不與 Textual 同時實作 |

非目標：網頁管理台、通用配置管理平台、任意 Linux 發行版、容器／路由器管理、全機安全保證、自動分割磁碟／PV 擴展／縮容、資料庫資料回復、無人值守整機一鍵重設。第一輪不管理網站、憑證或 DNS。沒有背景常駐網路管理服務。

## 終端視覺提案與8bit看板娘（尚未實作）

深色終端底、青綠重點與狀態提示，採終端色彩能力與Textual主題適配；選項、輸入值、來源與錯誤維持高對比。邊框與留白營造機房控制台感，不增加閃爍、滾動干擾或啟動動畫。保留終端既有背景／亮色主題適配選項，不能假定使用者永遠有黑底。

原創角色方向：吃雞排、喝珍奶的8bit技術女孩，保留機房工作外套，以簡單像素塊／字元輪廓呈現，取代拿扳手方案；不沿用現成IP或角色。這是裝飾提案，尚未製作美術資產。只用文字方塊與ANSI顏色能力，不依賴Sixel／Kitty／iTerm圖片協定、特殊字型或SSH圖片傳輸；基本輪廓與標籤可退化為ASCII／純文字。看板娘是非互動區，不加入Tab焦點，不代替功能名稱、狀態或警告。

寬畫面提案：終端有效寬度至少120欄、至少30行時，右側約22欄顯示角色與一行中性提示；主表單保有至少80欄。低於門檻或使用者選擇簡潔模式即收起側欄，不壓縮欄位或縮字。每次resize依實際可用區重新排版；中文字寬／Unicode能力無法可靠判定時允許手動ASCII模式，不憑locale宣稱測量必準。小於最低可用尺寸時顯示簡短尺寸提示及可退出操作，不能將確認鍵藏在螢幕外。

看板娘狀態只輔助情緒，例如待命／檢查中；健康仍用明確文字標籤「驗證通過／配方不同／未安裝／無法判斷／失敗」與圖示／ASCII代號。焦點同時有邊框／游標或反白標記，不能只靠青綠。嚴重錯誤與確認modal優先完整顯示，裝飾區可直接隱藏；不得遮擋diff、密碼輸入、回復路徑、SSH倒數或執行批准。

小表情提案：只有對應檢查當次真實通過、且結果仍有效時，才切換為「先放下珍奶，再比讚」的簡單靜態狀態／少量字元幀；未驗證、過期、失敗或部分成功不比讚，不借裝飾暗示全機健康。尊重減少動態／無動畫選項，無須新增動畫引擎。危險確認與故障畫面不顯搞笑提示或搶焦點表情，保持具體風險與選項優先。

小型ASCII版面示意（角色僅位置與氣氛草圖，不是完成像素畫）：

```text
+ 3waServerSetting  <version>  <commit> -----+---------------------+
| Overview Apache PHP DB Firewall ...      |   [8bit engineer]   |
| PHP / target FPM: <detected>              |       .---.    __   |
| [UNVERIFIED] effective value not checked  |       |o o|   /_/   |
| > session.upload_progress.enabled [Off]  |      /|___|  [tea]  |
|   Source: <managed entry>                |       / |  [snack]  |
|   [Refresh] [Preview] [Apply] [Exit]      |   Ready to inspect  |
+ Tab/Shift+Tab: focus  Enter: activate ----+---------------------+
```

窄畫面移除右欄，只保留產品／版本、可捲動分頁、狀態與表單，焦點順序不因裝飾改變。未來視覺驗收涵蓋寬／窄／resize、Unicode雙寬／ASCII、16色／無色、亮底、鍵盤全程、長錯誤／diff／確認及SSH救援倒數；以實際終端與SSH測試，不以此示意宣稱UI或角色資產已完成。本次僅設計文件更新，未安裝、部署網站或改系統。

## 頁面與受管範圍

工具同時用於首次架站與長期維運。啟動進入唯讀總覽，逐項顯示「未安裝／已裝未設／與使用者選定配方不同／驗證通過／無法判斷」，附偵測時間、證據、配方revision與限制；沒有選定配方時不判定偏離。已裝未設只在受管必要設定有明確缺項時使用，未知／權限不足不能混成未設。配方偏離是中性差異，不能自動叫不安全或錯誤；安全風險另列具體證據。驗證通過指本次執行且通過的檢查及範圍，不能由上次journal推定。啟動只跑有界、無副作用探測，不自動安裝、改檔、修復或啟停服務；主動HTTP／SQL測試等需標示為檢查動作且不寫業務資料。點進項目才提出差異與修正計畫，重新讀取可辨識手動外部修改，保留選擇接受現狀或取消。

各頁底部共用「重新讀取、預覽、套用、還原本次、Exit」。未安裝服務顯示缺少套件，安裝是另外一筆明確確認的操作。啟動與開機啟動為兩個欄位。頁面切換保留草稿；Exit 有未套用草稿時確認丟棄。

「還原本次」只對計畫標示可逆部分啟用；storage grow、DB與套件等不可保證還原的操作改顯示「檢視復原方式」。主動健康測試（暫存DB、測試端點、daemon試啟動）在進入項目後另行觸發，不在啟動總覽執行。驗證結果綁定當次配置指紋、範圍與時間，外部變更後失效。

| 頁 | 常用項目 | 驗證與限制 |
|---|---|---|
| Apache / PHP | Apache 與 PHP-FPM 服務／開機啟動；mpm_event + FPM；隱藏 Apache/PHP 版本；short_open_tag、memory_limit、post_max_size、upload_max_filesize、date.timezone=Asia/Taipei | 探測實際 FPM 版本／pool／socket；多版本需指定目標。驗證 upload ≤ post、數值單位；顯示建議不強制替既有系統調值。mpm_prefork/mod_php 遷移另列會停用模組及服務中斷，確認後才做。apache2ctl configtest、目標 FPM -t，再本機 HTTP/FPM 整合探測；CLI PHP 值不代表 FPM 值 |
| vsftpd | 服務／開機、匿名登入（預設不新增）、本機帳號登入、寫入、chroot、被動埠範圍及宣告位址 | 密碼傳輸風險及 TLS 狀態可見；不默默建立 FTP 帳號或開防火牆。沒有可靠完整離線語法檢查時，標為結構檢查＋受控啟動探測，不能宣稱已通過 daemon 語法驗證 |
| Samba | 服務／開機、具名分享路徑、唯讀／可寫、允許既有帳號 | testparm；不遞迴 chmod/chown，不預設 guest；新增帳號／密碼為獨立確認，不納入設定 diff |
| MariaDB | 服務／開機、監聽位址、既有認證方式；可選建立具名 DB／最小權限帳號及指定來源 | 保留 root socket 驗證選項，不強制改密碼、不提供一鍵 root@%。監聽、帳號 host/grant、防火牆各自預覽確認。先版本支援的設定檢查，再 socket SQL 與目標帳號登入；DDL／帳號變更不承諾交易回復 |
| 時間 | 時區（建議 Asia/Taipei）、同步狀態、現有 daemon 與來源 | 探測 timesyncd／chrony／ntpd 等實際服務；不硬寫 ntpdate、不啟用第二個競爭 daemon。多 daemon 衝突時阻擋寫入。時間跳動另提示工作負載影響 |
| 開機腳本 | 工具自有 systemd oneshot 腳本／unit、開機啟用；rc.local 相容狀態 | 預設管理自有 unit；rc.local 僅顯式選擇後建立相容接法，既有內容保留且避免執行兩次。shell 語法檢查、systemd-analyze verify；預覽不執行任意腳本，立刻執行另確認 |
| 防火牆 | 黑名單更新狀態、IPv4 INPUT 作用範圍、來源白名單草稿、救援來源 | 原nat.sh已審查，已交付離線候選；未部署，第一版排除VPN／NAT |
| 磁碟／Swap | 現有 swap、swapfile 容量／建立／啟用／開機持續；根目錄 LVM 容量／VG free／指定擴容量 | 詳見儲存節；不自動縮容，不預設用完空間 |
| Exit | 結束與未完成交易狀態 | 套用中禁止直接退出；斷線不取消救援 watchdog |

## 模組邊界與資料契約

Apache新增選配[MapServer／EasyMap與banner設計](apache-optional-security.md)：實際active vhost／CGI入口、嚴格MS_MAP_PATTERN與loopback授權、指定目錄權限；banner提供低調／羽山品牌模式，Prod不等於完全移除header，品牌與WAF啟用分開確認。只更新文件，不變更主機。

第一版新增[Apache敏感檔案保護](apache-sensitive-files.md)及離線候選config/apache-sensitive-files.example.conf：指定拒絕.git、.env與.env.*，涵蓋檔名／實體.git目錄／URL三種候選作用域，保留.well-known正常用途；檢查HTTP／HTTPS、Alias／proxy與合併覆寫。合成fixture匹配測試通過，Apache語法與實機行為尚待驗證；須預覽、備份與執行確認，不能以404或banner／port防火牆宣稱完成，也不是完整網站防盜。

PHP常用配方新增session.upload_progress.enabled = Off。總覽分列使用者目標值Off、解析配置讀值與目標FPM實際生效值；未做FPM驗證時明示「未驗證」，CLI值只能作CLI證據。探測實際ini載入順序、conf.d、FPM pool的php_value／php_admin_value及其他適用覆寫；發現衝突時顯示來源與優先順序，禁止只在php.ini追加後宣稱完成。套用僅修改受管設定，已符合且FPM證據有效則no-op；需要reload／restart的影響依既有確認流程。驗證契約涵蓋On→Off、已Off、conf.d／pool覆寫、CLI與FPM不同值、多FPM版本／pool及無法取FPM值；只透過受控本機診斷取得此設定，避免公開phpinfo或保留對外診斷端點。此設定不代表所有PHP漏洞已修復，不替代其他安全驗證。本次只更新設計，不改任何主機php.ini或重啟服務。

時間頁配方明確區分三件事：OS timezone、目標PHP CLI／FPM的date.timezone、NTP時鐘同步。唯讀讀取目前OS時區與能力，選擇Asia/Taipei後預覽；僅經套用確認才使用timedatectl set-timezone Asia/Taipei，之後以date及timedatectl核對時區／offset與狀態。已是Asia/Taipei則no-op，不重寫／不重啟；OS時區套用不代表PHP時區或NTP同步已完成。PHP設定在PHP頁獨立預覽，NTP沿用已探測daemon能力。無systemd／timedated或容器未獲主機時鐘／時區管理能力時顯示限制與唯讀結果，不盲目改宿主機或以檔案覆寫繞過。VM驗收增加三者不同值、OS已符合、套用失敗、缺systemd／容器限制及date/timedatectl結果比對；本次未執行任何時區命令。

MariaDB頁增慢查詢enabled／1秒門檻／檔案路徑／not_using_indexes OFF，分runtime與persisted及10.11新舊變數能力；新增獨立密碼救援說明入口，不提供一鍵救援。詳見[MariaDB操作設計](mariadb-operations.md)，其中敏感日誌收集、帳號／密碼／plugin、停庫救援與權限變更強制要求具體主機／計畫的特定執行批准，原筆記與設計核准都不是執行授權。

本機root免重輸密碼亦見該操作設計：先沿用unix_socket；僅密碼型主機明確選擇且當次確認後，才可安全保存/root/.my.cnf。要求root:root0600、原子寫入／保留未受管內容／symlink檢查／秘密遮罩，本機SELECT 1與CURRENT_USER()驗收，不要求更改socket認證。本次沒有建立、讀取或寫入實際憑證。

Apache 頁另含 mod_xsendfile：探測安裝／載入狀態、XSendFile 開關與具名允許傳檔目錄。預設不新增允許目錄，不授予整個檔案系統；正規化實際路徑並辨識 symlink 逃逸、Apache 執行帳號權限，按 vhost／目標配置套用。以允許目錄內小型測試檔驗證標頭觸發傳檔，並測允許範圍外遭拒；不能只用模組已載入當成功，也不能以遞迴放寬權限解決。

PHP 頁另列 PDO_SQLITE 與 SQLite3 extension 狀態，依 CLI 與目標 FPM 分別顯示 PHP版本、ABI/API、extension_dir、ini載入順序、pdo_sqlite/sqlite3及SQLite runtime版本，避免把兩種extension混為一談。支援兩條明確分開的計畫：發行版匹配套件（只在使用者選擇時安裝，不能自行代替重編需求）；進階 source rebuild（僅編譯核准的extension／SQLite需求，不重新編整套PHP）。

使用者重編配方來源：https://3wa.tw/mypaper/?uid=shadow&mode=view&id=2308 。網址未能讀取，但父層已提供可信配方摘要：在PDO SQLite連線初始化加入 sqlite3_enable_load_extension(H->db, 1)，以啟用SQLite原生loadable extensions。這不是單純缺少pdo_sqlite套件，也未證實是要求新版SQLite。舊例混用PHP8.1、8.2.7RC1及20210902 ABI，不可直接照抄；行號、來源目錄、extension_dir與FPM unit皆依實際探測，不硬寫。

官方替代優先：PHP官方手冊列 Pdo\Sqlite::loadExtension() 自8.4起提供；PHP-8.4官方原始碼顯示它僅於呼叫時啟用C API載入，完成／失敗後關閉，不開SQL load_extension()。先探測實際build是否提供類別／方法及load支援，再於選定應用連線中使用；不能假定所有Ubuntu或所有PHP8.4 build都可用。若既有應用用SELECT load_extension(...)，改用此方法需要應用呼叫調整，工具只提示相容需求，不擅改應用。只有官方方式經需求與VM測試不能滿足時，才提出匹配來源版本的最小patch，另行核准；不自動從apt路線轉成重編。

SQLite官方指出舊配方的sqlite3_enable_load_extension會同時開C API與SQL載入，增加SQL injection取得原生程式執行能力的攻擊面。所有native .so載入屬進階opt-in：只允許核准可信產物與絕對路徑、校驗值、root管理且應用帳號不可寫的目錄；不能使用上傳／分享／暫存目錄或用戶可控檔名。限制於必要應用／FPM pool／連線，不全機預設開啟；既有廣域載入patch亦不自動視為健康配方。SQLite3::loadExtension為另一PHP extension的獨立API，官方要求sqlite3.extension_dir；不能假設這個ini同樣限制PDO的API。具體入口、依賴.so、權限與替換風險須一併預覽。

可信路徑allowlist須由受信任應用包裝層實作並測試，不是PDO API內建沙箱；可執行任意PHP的應用可繞過此包裝。舊版patch不得在所有連線初始化無條件開啟；必須設計並驗證明確opt-in機制，否則阻擋部署。同一FPM master載入extension通常影響多個pool，不能只換.so便宣稱限單一pool；需獨立FPM instance／版本配置或經驗證的連線級開關，再驗證非目標應用沒有擴大能力。

來源套件設計：先讀實際apt來源格式（傳統.list及deb822 .sources）與Ubuntu suite、已安裝PHP套件來源／精確版本；只提出匹配suite與簽章來源的deb-src計畫，不能把jammy加入26.04。apt update／source／build dependencies是後續明確安裝與建置操作，本次不執行。匹配phpize、php-config、headers、module API、NTS/ZTS、架構與目標FPM；缺少精確來源則阻擋編譯，不能用RC來源替換正式套件。

重編計畫必須鎖定對應PHP source/header/phpize/php-config及編譯器、SQLite來源與校驗值、configure選項／patch與授權；分辨系統shared SQLite及自帶SQLite連結，檢查其他extension相依與ABI。先在對應VM建置及staging驗證，不覆寫系統套件所有的 .so；以版本專屬產物及ini切換，保存原extension與設定。若無法避免套件檔案衝突則阻擋並重設部署方案。套件／PHP安全升級可能改ABI或覆蓋載入設定，升級前需重建驗證，不無限鎖住安全更新。CLI與FPM分別做PDO開啟、建表、prepared statement、Unicode／交易；SQLite3做獨立測試。切換失敗恢復舊.so／ini與對應FPM服務，不對既有資料庫做不可逆格式升級；測試用暫存DB。php -m只證明列出模組；需要重新載入時重啟已探測的適當FPM，僅restart Apache不構成FPM切換完成。

載入測試契約：逐Ubuntu／PHP／CLI及FPM記錄版本與實際library；用核准測試.so載入並呼叫它獨有SQL函式，核對結果，不能只檢查php -m或方法存在。驗證未opt-in應用不新增載入呼叫或放寬配置、非法／可寫路徑由部署與應用邊界拒絕、缺依賴／錯ABI／錯.so有清楚失敗；官方API路線確認SQL load_extension仍禁用及新連線不繼承已載入函式。PHP官方API本身不是應用權限隔離或allowlist，已能執行PHP程式的應用可能直接呼叫它；需要嚴格隔離的工作負載須用獨立FPM程序／執行帳號及OS檔案權限等邊界，單一共用pool不能宣稱逐站禁載。舊應用SQL路線與官方API的相容／遷移測試分開，原routing_path/test.php僅為使用者本機驗收入口，未取得內容前不能宣稱已驗證。回復後確認原版PDO功能及載入政策恢復。

已獲窄範圍授權新增tests/sqlite-smoke.php與tests/README.md，不代表整套工具核准。CLI-only、獨立sqlite::memory:連線，無業務相依；PDO prepared/Unicode roundtrip、RTree實際CREATE VIRTUAL TABLE／insert／重疊正反例、SpatiaLite版本及平面多邊形面積各自收集結果。路徑僅CLI顯式傳入，auto/native不自動fallback到legacy SQL；缺能力為UNVERIFIED，已指定路線的載入或斷言錯誤為FAIL。exit 0全過／1有FAIL／2未完整驗證。這是最小smoke子集，不取代上述完整隔離、安全負例或FPM／Ubuntu驗收。

建議目錄：ui/（Textual 表單）、core/（探測、計畫、交易、備份、命令 runner）、modules/（上述服務）、platforms/ubuntu.py（版本能力表）、tests/。先用明確函式與資料類別，不預建外掛框架。

每模組提供 probe → plan → validate → apply → health → restore。probe 回傳實際值、來源檔／有效覆寫順序、能力與警告；讀取失敗是 unknown，不當成預設值。plan 是不可變計畫，包含檔案前後 hash、差異、套件動作、服務影響、驗證、可回復性及獨立確認項目。UI 不執行 shell；runner 使用固定 executable 與 argv、timeout、退出碼及遮罩輸出，不以欄位值拼 shell。root 檢查在啟動時完成；非 root 僅可檢視可讀資訊，不能假裝取得完整快照。

使用 /etc/os-release 的 ID、VERSION_ID 配合 dpkg 套件版本、systemd unit、實際設定檔與執行檔能力探測；只接受 Ubuntu 22.04／24.04／26.04 的已測能力。不得由版本字串推定 PHP socket、MariaDB 語法或 iptables backend。未知版本、衍生版或未知語法先唯讀並說明缺項。PHP、核心與套件精確版本由 VM 證據加入能力表，不預先寫死。

## 套用、持久化與回復

1. 取得單一程序鎖，probe 建立基準；表單值與實際有效值分開。
2. 預覽只列受管項目、安裝需求、重啟及風險；機密遮罩。套用前重新讀取比對 hash，外部變更即使計畫失效，要求重新預覽。
3. 建立 root-only 本機交易目錄 /var/lib/3wa-server-setup/transactions/<id>（0700；檔案0600），保存備份參照、symlink 目標／權限／owner、服務 active/enabled 狀態及交易 journal；原檔存 /opt/3waServerSetting/backups/<id>，權限依部署節不得更寬於原檔。主機資料與備份不寫 Git。
4. 優先自有 drop-in；不支援時只編輯確定的 key／具名區塊，保留註解與未受管項目。遇重複／衝突設定須解析有效值，不直接 append。symlink／非普通檔拒絕盲寫。候選檔在同檔案系統寫入，保留 metadata，原子替換前完成能離線執行的驗證；對需完整配置樹的 validator 建暫存樹，不能把不支援的參數假裝成驗證能力。
5. 依依賴順序逐模組套用，記錄每步完成狀態，再語法驗證、reload（支援才用）／restart、健康檢查。首次失敗停止後續模組；可回復檔案／規則與服務狀態還原並再檢查。回復失敗顯示具體狀態與本機救援路徑，不宣稱成功。
6. journal 支援重啟後辨識半完成交易；重新 probe 判定已完成／需恢復，不盲目重跑有副作用步驟。無差異不寫檔、不重啟、不重複 fstab、規則、分享或 unit。

不同資源沒有全機 ACID 保證。套件安裝、DB／帳號、腳本執行、儲存擴容各自獨立計畫；套件不自動降版／移除，資料不自動逆轉。資料庫操作需另列備份責任；不以還原設定檔宣稱還原資料庫。

失敗自動restore僅適用計畫已標示可逆的步驟；已完成的不可逆或難回復步驟保留「部分成功」證據及復原指引，禁止套用通用restore。密碼草稿只存在記憶體、結束即丟棄；非機密配方與主機實際快照分離。預設備份／去機密journal保留最近10筆且30天；清理前顯示清單，未完成交易及唯一可用復原點不自動刪除，改提示人工處理。主機草稿預設不持久化；選擇本機保存時僅非機密值、0600，亦不得進Git。

## 防火牆與遠端救援

第一版最新範圍已確認：只做服務port入站管制；VPN／PPP／NAT／FORWARD排除，不再列為本版待決。常用範本僅HTTP80、HTTPS443、SSH22、MariaDB3306、FTP21／可調PASV與Samba445，全數預設停用，FTP20只作主動模式說明。ping與MQTT專用項目延後。自訂TCP／UDP port可選僅該規則公開或指定IP／CIDR；未列新連線拒絕，保留loopback／established／related／SSH救援例外。來源與port驗證、Samba舊式NetBIOS選用及FTP限制詳見firewall-offline.md；沒有TUI或apply完成宣稱。

nat.sh已透過唯讀文字取得並審查；原檔含VPN／PPP forwarding與NAT，不能直接以INPUT白名單替代。已按窄範圍授權交付離線nat.sh＋firewall_plan.py與去識別設定範本，只驗證／輸出未掛接的自有IPv4 INPUT候選；不執行iptables／nft／sysctl或部署。詳見[原檔審查與測試限制](firewall-offline.md)。原VPN／NAT與舊公開埠不自動遷移，不宣稱整份原腳本功能已改寫完成。

沿用使用者指定 https://3wa.tw/myapache_banip/mybanip.php 的 IPv4 ipset＋iptables INPUT 黑名單方向。2026-10-24 瀏覽工具無法取得內容，尚未核對下載格式、更新方式、rule 順序與失敗行為；不得直接下載執行。實作前取得內容作靜態審查，資料更新須嚴格解析 IPv4／CIDR、限制大小與筆數、timeout，先建暫存 set 驗證後 swap；更新失敗保留上次良好 set，不先清空現行保護。

先讀 iptables backend、現有 INPUT policy／rule 順序、ipset、UFW／firewalld／nftables 管理者。與既有管理者衝突或 backend 不支援時阻擋套用。只掛工具自有 chain／set、保存原先連接位置，不 flush 他人規則、不改預設 policy；明確設計救援 allow、既有連線及 blacklist 的相對順序。SSH 救援來源由操作者確認 CIDR、實際 sshd port；不能只信 SSH_CONNECTION 或固定22。規則套用前由獨立 systemd timer/service 安排限時回復，顯示倒數；必須從第二條新 SSH 連線確認可進入後才取消 watchdog，原連線存活不算成功。重開機政策與 watchdog 一併驗證。

原nat.sh包含IPv4 INPUT政策及VPN／PPP／NAT功能，審查與未遷移項見firewall-offline.md。IPv6、FORWARD、容器／Docker鏈、NAT不在離線候選保護承諾；頁面持續顯示範圍。來源允許、服務listen、DB遠端帳號及套件安裝是四種獨立動作，彼此不自動連動。

附件已由合規唯讀文字介面取得，不需要Windows本機materialization；原內容未執行也未簽入。現已確認原default policy／flush、規則順序、晚置ESTABLISHED、缺通用loopback、FTP passive公開及VPN／NAT等差異。設定清單明列port、protocol、公開／限定來源及CIDR；21單埠不代表FTP被動模式可用。真實主機IP／憑證／原始設定不進repo。

## 磁碟／Swap

Swap 先讀 /proc/swaps、swapon 輸出、fstab、findmnt、可用空間與檔案系統／核心支援。預覽列出既有 swap 與新檔位置、容量、磁碟保留空間及開機設定。第一版僅支援經 VM 驗證可建非稀疏 swapfile 的本機 ext4/XFS；Btrfs、CoW、網路檔案系統與未知組合只回報限制。新路徑不可覆寫既有檔或 symlink；0600，建立完成且 mkswap/swapon 成功後才加入唯一 fstab 項，最後核對有效 swap。空間不足或不支援即停止並清理本次未啟用產物。

提供可調整的4 GiB（4096 MiB）preset，保留使用者 dd 零填充→0600→mkswap→swapon 的意圖，但不在本次執行。建立、立即啟用、開機持續是獨立選項；只有選開機持續才寫fstab。同名未知檔即使未啟用亦阻擋，不能判斷「未使用」就覆寫；受管既有檔走明確resize流程。

若選立即啟用，須swapon成功才提交fstab；若選「建立＋開機持續、暫不啟用」，完成格式／fstab靜態驗證後可提交，明列「尚未驗證啟用，待重開機或手動檢查」，不能顯示完整健康通過。啟用但不持續則完全不寫fstab。

不無條件 swapoff 既有 swap；新增與啟用分開。既有受管 swapfile 改容量必須另確認，檢查可用 RAM／其他 swap，無安全餘裕則阻擋，優先另建新檔驗證後再處理舊檔。回復僅移除本次受管 fstab／檔案；若 swapoff 失敗（例如記憶體不足），保留啟用檔與清楚狀態，絕不刪除使用中的檔案。

根目錄擴容用 findmnt 定位真正 / 的裝置與檔案系統，以 lsblk、lvs/vgs/pvs 的結構化資料追溯 LV/VG（含 mapper alias），顯示目前 LV、VG free、目標容量與支援的線上 grow。VG free 與磁碟尚未分割空間分列；非 LVM、thin／特殊堆疊、需擴 partition／PV 或不支援的檔案系統先回報限制。第一版只支援已驗證普通 LV 的 ext4／XFS grow。

指定容量為預設；「用完 VG free」必須主動選取並顯示耗盡 VG 的影響，不能預設 +100%FREE 或寫死 /dev/ubuntu-vg/ubuntu-lv。套用前重讀空間與映射、驗證容量及備份／VM snapshot 準備，由第二次風險確認授權。執行以探測到的 LV 為參數，分別記錄 LV extend 與 filesystem grow 結果；即使使用支援的合併命令，也要辨識部分成功。grow 失敗但 LV 已增大時保留較大 LV，回報待修復狀態；絕不自動 lvreduce。擴容／檔案系統 grow 不承諾 rollback，需外部備份或離線修復；df、LV、FS 容量分別驗證。重跑以實際目標容量為準，不能再次加同一增量。

## 部署與確認邊界

版本／自更新另見[更新設計](version-update.md)：tag發版、畫面顯示版本與commit及已驗證平台，明確檢查／選版／確認，固定來源，stage＋validate後才切換；config／backups／主機nat政策保留。程式更新與主機設定migration分開，回退程式不代表回復DB／磁碟／服務。updater尚未實作，也未fetch或執行遠端程式。

### Linux安裝與資料位置（指定設計，尚未建立）

Git儲存庫名稱維持3waServerSetup；Linux安裝根固定 /opt/3waServerSetting。主TUI launcher為 /opt/3waServerSetting/run.sh；/opt/3waServerSetting/nat.sh僅為防火牆子工具，不是主程式入口。啟動run.sh只做有界唯讀探測並顯示現況，不自動安裝依賴、改檔、修復、啟停服務或套用防火牆；缺少執行環境時顯示缺項並停止，不在launcher隱藏bootstrap。Git工作樹與安裝根分離，透過日後核准的部署程序複製已審閱revision的程式，不直接git pull覆寫主機設定。程式／run.sh／nat.sh由root擁有，非管理帳號不可寫；不放在DocumentRoot、Samba／FTP分享或任何網頁可讀路徑。

| 位置 | 用途與權限設計 |
|---|---|
| /opt/3waServerSetting/config/ | 工具自有主機設定、選定配方及私人port/protocol/source CIDR清單；目錄0700、一般設定0600，僅root。Git只放去識別範本，不保存實際IP、帳密或主機值 |
| /opt/3waServerSetting/backups/<transaction-id>/ | 套用前原生設定備份與復原manifest；目錄0700，備份內容權限不得比原檔更寬，亦不授予其他使用者；一般檔採原mode與0600的交集，保留原metadata於受保護manifest供還原。來源ACL／xattr需記錄，備份副本不得繼承更寬ACL；無法安全保存時阻擋套用 |
| /etc 下各服務原生位置 | Apache、PHP-FPM、Samba、fstab及systemd等有效配置維持原生位置，只改受管key／drop-in，不整批搬到 /opt |
| /var/lib/3wa-server-setup/transactions/<id>/ | runtime持久交易journal、配置指紋及備份參照，不重複保存原檔內容；0700／0600。程序中斷後依此定位 /opt 的備份與復原進度 |
| /run/3wa-server-setup/ | 程序鎖與短期狀態，0700／0600；重開機後重建，不作唯一復原證據 |
| /var/log/3wa-server-setup/ | 去機密操作結果與驗證摘要，0700／0600；不記密碼、完整主機配置或原始命令輸出。與備份分別輪替，預設保留30天；未完成交易仍保留其必要journal |

前文交易步驟的原檔備份位置以本節backups為準，/var/lib僅保存journal與參照；備份保留與未完成交易保護規則不變。啟動前檢查安裝資料路徑不是symlink逃逸、非可信使用者不可寫，也未被Web／分享服務映射公開；無法確認時不能聲稱安全。備份可能含密碼，禁止Git追蹤、打包至發行產物或公開服務。日後repo需加入對實際config、backups、journal、log及秘密檔的ignore與發行排除規則，範本另行保留；ignore不是唯一保護，提交／封裝還需檢查。

本次只新增此安裝路徑設計，沒有在Windows或任何Linux主機建立 /opt/3waServerSetting或安裝。repo中的nat.sh只支援離線候選，不是主機部署；附件已透過唯讀文字取得，不重試先前失敗materialization。

整套TUI仍只做設計；獨立PHP smoke、離線防火牆與Apache候選為後續明確授權的已完成元件。使用者另已授權將現有進度提交／push main，不代表核准安裝套件、VM／系統修改或部署。未來部署須從使用者確認的repo revision取得並檢視內容；不curl|sh，不改SSH信任／金鑰／憑證。Python依賴在獨立venv，鎖版與雜湊；bootstrap先顯示缺項、來源與安裝計畫，安裝確認後才使用apt／建立環境。

進入頁面與預覽皆唯讀；一般設定按「套用」確認本頁明細。套件安裝、重啟／中斷、帳號權限、遠端暴露、防火牆與 storage 另有具體風險確認，不捆綁成一鍵全選。密碼只由遮罩輸入／受控 stdin 或 socket 傳遞，不進 argv、環境變數、日誌、diff、例外、Git；若用臨時 credential file 必須0600、限定生命週期並清理。記錄命令類型與結果，不直接記錄可能含秘密的原始 stdout/stderr。備份可能含機密，留 root-only 本機並提供明確清理／保留設定。

## 測試矩陣與驗收

指定唯讀盤點的去識別共用配方與缺口已轉入[盤點設計筆記](inventory-design-notes.md)，包括MapServer HTTP／HTTPS FastCGI授權不對稱、SAPI限額差異、banner覆寫、nat flush與banip掛接時序。只作配置線索，不作runtime或已外露判定；報告被拒讀項目不重試，完整主機盤點不入repo。

Windows用於設計、repo與離線／CLI測試；Linux驗證另由VM工作協調。每台初案2vCPU／4GiB／40GiB動態磁碟，尚未建立；這是待確認的測試規格，不是已驗證容量，不因既有虛擬化工具存在而推定硬體資源或功能可用。

| 平台／順序 | 執行模式 | 必驗證 |
|---|---|---|
| Ubuntu 26.04 第一台 | 完成基本、失敗、重跑、重開機案例後關機 | 所有頁面與實際套件能力，記錄 ISO/revision/kernel/package/backend |
| Ubuntu 24.04 第二台 | 一次只運行一台 | 同矩陣；版本差異不得直接沿用26.04結果 |
| Ubuntu 22.04 第三台 | 一次只運行一台 | 同矩陣；Python/Textual 安裝與終端相容性 |

共通案例：乾淨系統／既有自訂配置／未安裝／服務停用、unknown probe、外部改檔、重複套用無差異、語法錯誤、服務啟動失敗、斷線／程序中斷、回復失敗、權限／磁碟不足、秘密不出現在紀錄。鍵盤從分頁列到欄位／按鈕可全程操作，SSH窄終端與resize可用。整合確認 Apache event+FPM實際處理請求與FPM值、分享權限、DB socket與指定來源帳號、NTP同步與開機腳本只跑一次。

維運總覽驗收：啟動前後配置hash與服務狀態不變；五種狀態及無選定配方皆有案例。外部手改後再次啟動／刷新可看見差異；舊journal成功但現況失敗時不能顯示通過；偏離配方顯示中性差異，點入預覽前不提出自動修復。探測timeout或權限不足明確顯示無法判斷。

防火牆在有 console 救援的 VM：黑名單更新錯誤、SSH 非22、第二連線確認、watchdog 回復、重開機持久化、既有 UFW/nft 衝突；證明 IPv6／FORWARD／容器沒有被誤宣稱涵蓋。nat.sh 到位後才新增來源白名單允許／拒絕與優先順序案例。

Storage：無swap／已有多個swap／fstab已有項／新增後重開機／同名檔或symlink／空間不足／不支援FS／swapoff失敗；普通根LV ext4與XFS、指定容量及主動用完VG free、零VG free、磁碟空間未加入PV、非LVM、LV成功FS失敗與重跑不重加。以 VM snapshot／備份復原驗證難回復案例，不在正式主機試錯。

擴充驗證：4GiB preset與自訂容量，建立但不啟用／啟用但不持續；mod_xsendfile允許／越界／symlink案例；各PHP版本CLI/FPM載入差異、PDO_SQLITE與SQLite3獨立功能、重編ABI不匹配阻擋、切換回復、套件升級後再驗證，以及上述loadable extension正反向契約。

## 待核准與缺件

已選Textual與逐台26.04→24.04→22.04，不需要重選。整套工具待核准，獨立smoke與離線候選已獲窄範圍授權。nat.sh原檔已審查，第一版不管理VPN／NAT、不自動遷移舊公開埠；黑名單來源內容及授權／格式仍待核對。各服務推薦數值、FTP TLS需求及套件版本待VM probe後提供預設建議；未決前保留現值。第一版storage僅普通LV／ext4／XFS及受測swapfile，其他堆疊另案擴充。

參考：Textual TabbedContent 官方文件 https://textual.textualize.io/widgets/tabbed_content/ （2026-10-24已查閱）；黑名單來源為使用者指定網址，尚未成功讀取內容。本文是行為契約，具體 daemon flags／套件能力須在各VM查官方手冊並以實測確認。

PDO／SQLite官方查證（2026-10-24唯讀）：[PHP loadExtension方法及起始版本](https://www.php.net/manual/en/pdo-sqlite.loadextension.php)、[PHP-8.4原始碼之載入與關閉行為](https://github.com/php/php-src/blob/PHP-8.4/ext/pdo_sqlite/pdo_sqlite.c)、[SQLite啟用載入與SQL攻擊面說明](https://www.sqlite.org/c3ref/enable_load_extension.html)、[SQLite3獨立API](https://www.php.net/manual/en/sqlite3.loadextension.php)、[Ubuntu套件來源格式](https://ubuntu.com/server/docs/how-to/software/package-management/)。PHP方法手冊說明尚不完整，方法細節由官方原始碼佐證；VM實作時須固定實際release/tag重新核對，不能把浮動分支當供應鏈鎖定。

## 設計review紀錄

後續授權範圍更新：獨立PHP測試已加入SQLite3 extension的獨立RTree建表／插入／overlap正反例，本機與PDO／SpatiaLite合計四項PASS；Ubuntu／FPM未測。離線防火牆候選已產出，尚未掛接／套用，也不含可執行部署或自動回復；不將其離線測試當主機安全證據。

2026-10-24由獨立reviewer執行可實作性檢查；修正swap選項與fstab驗證語意、不可逆步驟的按鈕／回復限制、主動健康驗證邊界及機密保留策略。後續收到PDO_SQLITE可信配方摘要與nat.sh唯讀內容，已更新目的、官方替代、原規則審查與驗證契約。獨立reviewer複查離線防火牆、SQLite3、Apache選配及MariaDB文件，修正restore可能flush的警告與規則展開上限，未見其他阻擋問題。尚無Linux主機／Ubuntu VM實測通過宣稱。

PDO增補經同reviewer複查，補正allowlist並非API沙箱、同FPM master跨pool及舊patch明確opt-in的限制；沒有新增重大架構決策。

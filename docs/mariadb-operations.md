# MariaDB慢查詢與帳號救援設計筆記

2026-10-24官方文件核對；僅操作設計，不是執行授權。尚未對Ubuntu VM或資料庫驗證。

## 慢查詢

使用者選定配方：啟用、門檻1秒、FILE輸出、log_queries_not_using_indexes=OFF。10.11起新名稱為log_slow_query、log_slow_query_time、log_slow_query_file，對應舊slow_query_log、long_query_time、slow_query_log_file；依實際版本及變數能力選一組，不同時寫新舊別名。OFF僅停止「因未用索引而額外記錄」，不排除本身超過門檻的查詢。仍須探測log_output、filter、rate_limit及min_examined_row_limit，避免誤以為enabled就會記所有慢查詢。[官方slow log](https://mariadb.com/docs/server/server-management/server-monitoring-logs/slow-query-log/slow-query-log-overview)

UI分開顯示runtime global、目前session與持久配置；動態SET GLOBAL不代表重開機保存，也不保證已存在連線的session門檻同步改變。選「立即套用」「開機持續」各自預覽；後者寫入受管server option group。驗收以新連線讀值，再以核准的無業務資料測試查詢確認記錄，經核准重啟後再次讀值。此操作屬MariaDB，不需要FPM或Apache重啟。[官方變數與持久性](https://mariadb.com/docs/server/server-management/variables-and-modes/server-system-variables)

Ubuntu探測實際mariadbd、systemd unit啟動參數、defaults檔及include順序，通常可能涉及/etc/mysql/mariadb.conf.d，但不能當已確認位置；不套用其他發行版的/etc/my.cnf.d。以受管drop-in寫入實際會讀取的群組，偵測其他覆寫來源。讀取print-defaults可能含秘密，解析時遮罩且不將原輸出寫log。[官方配置搜尋與群組](https://mariadb.com/docs/server/server-management/install-and-upgrade-mariadb/configuring-mariadb/configuring-mariadb-with-option-files)

/var/log/mariadb/slow_query.log是可選建議，不是已存在或必然可寫。選定前驗證父目錄／檔案非非可信symlink、實際MariaDB帳號可寫、其他帳號不得讀，並符合AppArmor及unit限制；不為此廣域放寬權限或關閉安全機制。SQL可能含個資、token與密碼，原始慢查詢內容不得入Git或工具操作log，不由總覽直接公開。採套件既有logrotate並比對實際路徑，避免兩套重複輪替；設計輪替大小／保留期、磁碟餘裕、重新開啟log與輪替後寫入驗證。不得只rename後假設daemon已改寫新檔，也不照抄含帳密命令列。[官方log rotation](https://mariadb.com/docs/server/server-management/server-monitoring-logs/rotating-logs-on-unix-and-linux)

## root管理與忘記密碼

後續實作更新：使用者已明確授權本機 root 密碼、root@% 及外連 On／Off，在 Ubuntu 26.04 VM 完成原生驗收。操作與保留條件見 [MariaDB root access](mariadb-root-access.md)；下方救援、密碼檔與其他帳號是分期設計，不代表已實作或附帶授權。

先確認版本及root@localhost的實際驗證方式，嘗試已授權本機socket管理（例如sudo mariadb --protocol=socket）；可管理就不進救援。10.4起可有多個驗證方式，mysql.user是view；不直接UPDATE mysql.user/global_priv，不盲改plugin。需要設密碼時依現有plugin與版本選SET PASSWORD或ALTER USER，先預覽是否保留unix_socket；純socket帳號未必能直接SET PASSWORD，不把切換認證當附帶動作。

只有管理通道都失效、且取得本次特定救援批准，才安排停機窗口、有效備份與本機console；正常停止正確instance，確認退出與datadir／socket／binary版本，不讓兩個server共用資料目錄。臨時skip-grant-tables須同時隔離TCP網路（skip-networking）並限制本機socket存取；此模式本機使用者亦有風險，不持久寫入正常配置。10.4+官方流程在連入後先FLUSH PRIVILEGES再SET PASSWORD，與舊筆記順序不同。不得盲套舊版順序或killall -9；受控正常關閉救援instance、移除臨時override，再由原systemd unit正常啟動。[官方10.4認證與救援](https://mariadb.com/docs/server/security/user-account-management/authentication-from-mariadb-10-4)

驗收核對正常模式未殘留skip-grant-tables／skip-networking臨時參數、原socket管理仍可用、選定密碼方式可用、帳號權限與原監聽政策符合計畫。密碼不進argv、shell／SQL歷史、Git、一般journal或未遮罩log；處理時檢查general／audit／slow log記錄風險，採版本支援的敏感語句保護，不能假設所有外部audit都自動遮罩。未知記錄政策就阻擋密碼操作。

## 本機root免重輸密碼（設計，未操作）

首選既有unix_socket驗證，由OS root以實際可用的mariadb／mysql client走本機socket，不儲存密碼，也不要求改掉socket。探測binary、版本、HOME／option-file搜尋及覆寫、protocol與socket位置；避免client配置把本機驗證轉成TCP。僅有互動client開啟不代表驗證成功。

既有密碼型主機可明確選擇/root/.my.cnf的[client] user=root與password設定；這是持續保存現有秘密的獨立操作，必須當次明確確認，不從整套設計批准推定。透過本機遮罩安全輸入，不要求使用者在聊天貼密碼，不讀取／回顯既有秘密作為探測輸出。不改伺服器帳號、密碼或plugin。顯示client檔會影響哪些工具；不得把[client]憑證暗中套用於遠端主機。

寫入前檢查/root及目標路徑owner、權限、普通檔／symlink與不可信路徑；既有檔案以受控解析保留其他group／註解與未受管設定，重複user/password或未知語法需先釐清，不直接append。秘密值用符合option-file語法的安全編碼，拒絕會造成新key／section注入的輸入。採root-only同目錄暫存檔、0600、fsync及原子替換，避免TOCTOU／symlink覆寫，設定root:root0600並驗證實際metadata；無法保證安全就停止。憑證備份同樣機密、不得比原檔更寬，不進Git或一般備份發行包。

驗證只執行本機唯讀SELECT 1及CURRENT_USER()，確認退出碼、結果與root@localhost身份，另記實際使用binary／socket但不記密碼。client命令不帶明文密碼，option-file參數位置依binary規格；如有--defaults-file需在要求的位置傳入，辨識其他defaults／命令列覆寫，不能測到另一份配置而誤判。一般diff／畫面／log／例外／測試stdout與stderr均遮罩秘密；不輸出print-defaults原文。驗證失敗還原原檔（若原本不存在則移除本次產物），不趁機改認證。已存在且有效的socket路線不建立密碼檔；憑證檔檢查不得宣稱所有client都使用同一配置。[官方option-file規則](https://mariadb.com/docs/server/server-management/install-and-upgrade-mariadb/configuring-mariadb/configuring-mariadb-with-option-files)

測試契約：socket可用no-op、既有密碼型及特殊字元、錯密碼、既有多group與覆寫、非0600／錯owner、symlink、原子寫入失敗與還原、client不存在／不同binary及TCP覆寫，確保輸出／journal無秘密。本次沒有讀取/root/.my.cnf、取得／寫入密碼或修改認證。

## 遠端帳號與批准邊界

指定username、確切host、database與必要操作權限；CREATE USER與GRANT分開預覽，預設不用root、不用%、不給*.*或WITH GRANT OPTION。需TLS與否列為明確連線政策；listen、帳號host及來源防火牆仍獨立。用指定來源正向登入及非允許來源負例、SHOW GRANTS驗證，不能以root本機成功當驗收。[官方遠端連線](https://mariadb.com/docs/server/mariadb-quickstart-guides/mariadb-remote-connection-guide)、[CREATE USER](https://mariadb.com/docs/server/reference/sql-statements/account-management-sql-statements/create-user)、[GRANT](https://mariadb.com/docs/server/reference/sql-statements/account-management-sql-statements/grant)

唯讀偵測不授權執行以上任何變更。啟用慢查詢（敏感資料收集）、新增log目錄／輪替、SET GLOBAL、寫配置、重啟、密碼／plugin／權限及離線救援，都需針對具體主機與計畫取得特定執行批准；不能由核准設計或原筆記推定。無本機SQL或服務操作已執行。

# Apache選配資訊隱藏與MapServer安全配方

2026-10-24設計；本次只查官方文件，不修改主機、安裝模組、chmod/chown、重啟或對外測站。套用安全敏感變更另需指定主機／計畫的執行確認。

## Apache banner：低調／羽山品牌

低調預設提案為ServerSignature Off與ServerTokens Prod：前者抑制Apache產生頁面的footer，後者通常仍送出Apache產品名稱，不等於完全移除Server header。UI將「不露版本」與「完全不送Server header」分開；完全不送為選配目標，必須確認當前Apache／模組／前端代理實際支援，未驗證時顯示不可保證，不能只靠Header unset就宣稱所有回應皆移除。[Apache官方core](https://httpd.apache.org/docs/current/mod/core.html)

羽山品牌字串提案為FeatherMountain WebServer V1.0，可配置但不得注入換行或任意directive。ModSecurity2的SecServerSignature屬global main config，不放VirtualHost；官方要求ServerTokens Full，因其覆寫Apache保存signature的記憶體空間。這是2.x機制，不自動套到libModSecurity3或其他connector。安裝WAF與SecRuleEngine On為獨立變更，不能為改banner預設啟用；沒有有效規則、相容驗證與監測，不宣稱有WAF保護。[ModSecurity官方手冊](https://github.com/owasp-modsecurity/ModSecurity/wiki/Reference-Manual-%28v2.x%29#secserversignature)

安全回退：保留全域Prod／Off基本設定；品牌候選須在隔離VM驗證實際module識別、載入順序及適用條件，Full僅與有效品牌機制配套。IfModule只能處理未載入情況，不能證明覆寫成功。候選語法驗證及HTTP行為驗收失敗不得部署Full；若reload後驗證仍失敗，恢復Prod／Off並驗證，回復失敗則報未解狀態而非完成。必測模組缺失／停用／重排／品牌覆寫失效後不露版本；無法達成此保障則停用品牌選項。

Ubuntu使用探測到的apache2服務、conf-enabled／sites-enabled與include鏈；RHEL httpd／yum筆記不直接移植，不新增本輪支援平台。HEAD與GET、正常頁及Apache產生403／404／受控錯誤頁、HTTP與HTTPS／不同vhost各測header與body；重複header、應用／FPM、反向代理新增或覆寫的Server／X-Powered-By亦分層核對。只在本機與核准測試來源驗證，不使用舊外部測站。不露banner不是修補漏洞或版本升級替代品。

## MapServer／EasyMap（選配）

先辨識實際active HTTP／HTTPS vhost、include、ScriptAlias／handler與URL合併規則，不能只改sites-available下000-default.conf、default-ssl.conf便宣稱有效。目標為真正處理/cgi-bin/mapserv的範圍，避免套到錯誤Directory／Location或其他別名。現有.htaccess內容未知，即使使用者說已進Git，本repo尚未取得它；不能推測其授權、rewrite或保護效果。

保留使用者Map參數意圖：MS_MAP_PATTERN為`^easymap_server/[0-9]+[.]map$`，只允許該相對目錄的數字map檔，不能放寬成任意.map或任意絕對路徑。先依實際MapServer版本確認環境變數、CONFIG與MS_MAPFILE／MS_MAP_NO_PATH／map alias的交互與優先度；MapServer8引入CONFIG，不能只加Apache SetEnv就假定所有部署生效。查實際CGI環境、工作目錄及symlink到真實mapfile的解析；正則匹配字串不是完整檔案系統沙箱。MS_MAP_NO_PATH與alias可作另行核准替代，不能悄悄改掉應用依賴的map=相對值。[官方環境變數](https://mapserver.org/optimization/environment_variables.html)、[官方CONFIG](https://mapserver.org/mapfile/config.html)

Apache Require ip 127.0.0.1 ::1限制已識別的CGI入口，IPv4／IPv6 loopback均納入。它是該入口的來源授權，不保證所有間接路徑被保護；本機reverse proxy／PHP轉呼叫可能讓外部流量呈現loopback。需審查代理路由、可被外部操控的本機請求與mod_remoteip可信代理配置；不可接受任意X-Forwarded-For作loopback證明。若業務透過本機代理入口公開功能，代理必須自行驗證允許操作與參數，並測拒絕繞過CGI限制。[Apache授權](https://httpd.apache.org/docs/current/howto/access.html)、[mod_remoteip](https://httpd.apache.org/docs/current/mod/mod_remoteip.html)

資料夾權限為明確選配：目標/var/www/cgi-bin/easymap_server可配置，owner／group依實際執行帳號與管理群組選擇，不硬寫john。2770 setgid目錄／0660資料檔僅對確認清單套用，先列原owner/group/mode/ACL與diff、既有檔及新檔需求；服務需哪些讀／寫權與管理者寫入要逐一確認。新檔mode受umask／ACL影響，不能以setgid宣稱0660自動繼承。拒絕跨symlink／掛載點及無關檔案遞迴更改，不把mapserv CGI可執行檔改0660；備份metadata並驗證服務必要讀取／寫入，權限變更可回復範圍另列。

官方查證補充：MapServer8的CONFIG ENV優先，未設相應項目時可fallback Web server環境；MAPS alias及MS_MAP_NO_PATH會改map=參數解析。亦須核對MS_MAP_BAD_PATTERN（7.6.3起）等限制，不能為讓舊路徑通過便移除防護。官方網站本次直接開啟回403，已讀取[官方文件原始碼CONFIG](https://github.com/MapServer/MapServer-documentation/blob/main/en/mapfile/config.txt)與[環境變數](https://github.com/MapServer/MapServer-documentation/blob/main/en/optimization/environment_variables.txt)佐證；main可能含開發版內容，實作前固定實際release對應文件重新核對。

驗證契約：讀原值→差異→備份→Apache configtest→核准reload／restart→本機IPv4/IPv6允許且CGI功能正確／外部直連拒絕，涵蓋HTTP/HTTPS、Host/SNI與別名；合法數字map可讀、非數字／上層路徑／絕對路徑／編碼與symlink逃逸負例。檢查Apache是拒絕，不能把MapServer自身錯誤當外部已封鎖；本機代理外部入口另測，.htaccess取得後才審其實際效果。Ubuntu／MapServer版本尚未VM驗證，不能宣稱已安全套用。

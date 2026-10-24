# 第一版Apache敏感檔案保護候選

僅離線候選：[config/apache-sensitive-files.example.conf](../config/apache-sensitive-files.example.conf)。未安裝至Apache、未reload／restart、未對正式站發請求；沒有讀取真實.env、Git設定或憑證，沒有刪檔。

## 範圍與官方依據

Apache2.4：FilesMatch按檔名拒絕.env、.env.*及.git檔（含worktree pointer）；DirectoryMatch按實體目錄路徑拒絕巢狀.git及其內容；LocationMatch按URL路徑補充拒絕敏感segment（含proxy URL）。使用不分大小寫的明確名稱，不封全部dotfiles，.well-known／ACME不匹配。這僅保留該用途可被其他政策允許，不表示自動grant ACME。

官方區分filesystem與webspace：Alias目標需檢查實體路徑規則，代理與應用自行讀檔／改名傳回內容不能只靠FilesMatch；其他URL映射、rewrite、path-info、編碼與symlink亦須逐入口測。候選不保證所有間接路徑受保護。[官方作用域與合併](https://httpd.apache.org/docs/2.4/sections.html)、[FilesMatch／DirectoryMatch／LocationMatch](https://httpd.apache.org/docs/2.4/mod/core.html)

各區塊AuthMerging Off讓該匹配區塊的Require all denied取代前面授權，不代表它會永遠勝過後續Location／If／vhost等合併。global與HTTP／HTTPS active vhost的include及授權均需盤點，不能只改sites-available或在main放一次就宣稱全站有效；必要時為各目標vhost放置受管include並核對後續覆寫。需mod_authz_core，不能包IfModule而在缺模組時靜默放行。[官方AuthMerging](https://httpd.apache.org/docs/2.4/mod/mod_authz_core.html#authmerging)

## 安全套用與runtime驗收（尚未執行）

讀既有有效配置與handler／vhost清單→預覽新增區塊、衝突與影響→備份原檔及metadata→在完整候選樹執行Apache configtest→針對具體主機取得套用／reload確認→驗證→失敗還原受管變更。原站已有相近保護時只顯示差異，不重複追加；不讀敏感檔內容來判定是否應保護。

用隔離VM的全合成DocumentRoot／Alias／測試proxy建立**確實存在**且只含公開測試marker的.env、.env.local、巢狀.git/config及.git pointer；同時建立普通檔與.well-known/acme-challenge測試檔。HTTP／HTTPS、各Host／SNI、GET／HEAD、巢狀路徑、大小寫、編碼、Alias、proxy與相反授權覆寫逐項驗收：敏感路徑須由Apache授權拒絕且不返回marker，正常檔與ACME仍可用。確認測試資源確實存在、非其他錯誤頁造成失敗；不存在路徑404不能當防護證據。測後只清理該合成fixture，不碰真實網站資料。

本機tests/test_apache_sensitive.py只用Python正則檢查候選中三種pattern與合成存在檔路徑，並檢查明確deny區塊；**不是Apache語法／PCRE／merge或HTTP行為驗證**。12個Python測試（含既有防火牆10個）已通過；受限Temp fixture寫入曾拒絕，經明確僅合成測試的較高權限重跑通過，沒有調整主機安全設定。Ubuntu／Apache configtest與上述HTTP整合矩陣仍待驗證。

這是防止敏感部署檔透過指定HTTP入口洩漏，不是防止完整網站被複製；公開HTML／JS／圖片仍可被下載。一般port防火牆與banner隱藏不提供此檔案授權，也不代替更新、應用授權或秘密管理。敏感資料原則上仍應放在非Web可服務位置，這份候選是額外防護，不是安全放進DocumentRoot的許可。

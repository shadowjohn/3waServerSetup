# 第一版唯讀TUI本地里程碑

此文件保留早期唯讀版的驗收範圍。後續已完成 root 功能開發並另獲 VM 實測授權；目前操作見 [Root 管理](root-management.md)及[Ubuntu 驗收](ubuntu26-root-validation.md)。一般帳號仍維持唯讀模式。

本輪原checkout使用codex/textual-readonly-v1，未修改D:的新checkout，未commit／push、部署或連VM。使用者後續明確授權專案venv依賴，Textual8.2.8已安裝，實際PTY啟動／退出及headless鍵盤／resize驗證完成；這不是Linux主機或系統套用驗收。沒有系統套用API或Apply按鈕。

## 啟動

Python3.10+；互動模式需要Textual8.2.8及其相依。依後續窄範圍批准已在工作樹.venv安裝，未改系統Python或VM；來源與hash核對見[依賴檢查](textual-source-check.md)，完整版本／hash見requirements-tui.lock。launcher不自行bootstrap。Windows本工作樹直接使用`.venv\Scripts\python.exe -m server_setting`，或該venv的python執行下列命令。

```sh
python -m server_setting --text
python -m server_setting --status-json
python -m server_setting --version
# 依賴已存在時：
python -m server_setting
python -m server_setting --smoke
sh run.sh
```

Linux run.sh優先既有.venv/bin/python，否則python3；Windows直接python。缺Textual明示UNAVAILABLE、exit2，保留--text／--plain／--status-json，不下載依賴。TERM=dumb亦使用純ASCII文字狀態退化，沒有互動表單；--ascii使用ASCII標籤與角色，但Textual可能仍繪Unicode邊框，完全無Unicode終端需--plain。

## 已實作的介面程式

Overview、Apache、PHP-FPM、FTP、Samba、MariaDB、Time、Startup、Disk/Swap、Firewall、SQLite分頁。Tab／Shift+Tab移動焦點、Enter操作按鈕、Ctrl+左右切頁、Ctrl+R重新讀取、Ctrl+Q退出；角色非互動不搶焦點。寬度120×高度30以上顯示原創雞排珍奶ASCII技術女孩，窄畫面收起，沒有假成功比讚。退出直接丟棄記憶體草稿，無任何保存或套用。

視覺修正：正常彩色終端改用server_setting/mascot.py手繪20×40像素資料，以Rich前景／背景與Unicode上半格呈現20欄×20行；有臉、頭髮、衣服、手持雞排與珍奶，不再以文字標籤冒充看板娘。右欄仍24欄且不取焦點，140欄下主表單至少100欄，80欄自動收起。明確--ascii、NO_COLOR或ASCII stdout編碼使用無色ASCII退化；完全低能力終端使用--plain。無圖片協議、Web服務、額外監聽port或動畫引擎，不把角色姿態當健康證據。

修正後[實際Rich compositor畫面PNG](previews/tui-pixel-girl-wide.png)、[向量畫面](previews/tui-pixel-girl-wide.svg)、[窄畫面](previews/tui-pixel-girl-narrow.svg)。PNG由實際compositor文字／色彩格柵化，已目視核對；不是PuTTY原生截圖，使用者參考附件未在此executor讀取。Library預覽ID：libfile_dbb103aa733c8191a5658ab7429d2164。完整28項unittest、headless smoke、compileall與pip check通過；Ubuntu部署／PuTTY實際字型渲染未由本任務重測。

常用表單為草稿目標，預覽以目前選定配置讀值作純diff，UNKNOWN明列，檢查port／PASV上下限、PHP限額單位與upload≤post、單行輸入。配置相同只說NO DIFFERENCE，不表示健康通過。Startup／Storage此輪只有狀態與限制說明，尚無建立swap、LVM或開機腳本表單／操作。服務啟停／開機、帳號／分享／DB認證亦未實作，沒有以空表單假裝完成。

Firewall直接重用firewall_plan.py；可選服務預填、自訂TCP／UDP、port／範圍、公開或指定IPv4/CIDR、顯式啟用後加入記憶體草稿、預覽與reset。所有預設服務仍停用。ssh_rescue目前沿用範本文字佔位，UI不提供部署／SSH救援配置或掛接，輸出仍不可直接導入iptables-restore，IPv6未涵蓋。Apache敏感檔按鈕讀取既有候選，不改/etc。SQLite明確按鈕才呼叫既有CLI memory smoke，不自動跑、也不接受native模組路徑；SpatiaLite預期UNVERIFIED，完整native smoke仍走原CLI自行明確指定。

## 唯讀探測能力與限制

只探測本機，不接受遠端host、SSH或任意命令。Windows所有Linux模組明示UNAVAILABLE。Linux讀os-release，支援目標22.04／24.04／26.04只表示目標平台，不表示已測；其他發行版顯示warning。固定argv allowlist只含systemctl show、timedatectl show與findmnt查詢，timeout4秒，不啟停服務。服務unit不存在僅說UNIT NOT FOUND，不推定套件未安裝。

單一FPM版本讀取php.ini指定key；多版本要求另選目標，不假定第一個。conf.d／pool覆寫與有效FPM值未解析，仍標UNVERIFIED。vsftpd／Samba只選定配置key，include與share scope未解析；MariaDB不讀/root/.my.cnf、不連DB、不讀秘密。Apache只讀service狀態，尚未解析完整include鏈／banner有效值。Time讀時區與同步旗標，未完整探daemon；Storage僅活動swap數與根FS容量，無LVM mapping驗證。啟動不讀任意rc.local或執行腳本。所有OBSERVED僅觀察證據，無PASS宣稱。

## 實際驗證與下一步

已跑的UI驗證：Textual headless --smoke成功；2個pilot測試覆蓋Enter預覽、Tab焦點、自訂UDP公開單埠／空來源拒絕、Ctrl+左右切頁、140×40→80×24→140×40側欄收合。修正resize公開事件先於App快取尺寸更新造成的舊尺寸判斷；測試在切頁後等待layout，避免點擊尚未顯示的按鈕。實際PTY啟動成功，Tab與Ctrl+Q正常退出。TERM=dumb的純文字fallback亦驗證。Windows的Linux狀態仍顯示UNAVAILABLE，沒有假PASS。

core10項與既有離線12項加UI pilot2項，完整24項全部通過、無skip；涵蓋純diff、未知值、命令白名單、PHP prepend／append停用及合成fixture。compileall、PHP語法與pip check通過。新安全CLI memory smoke的SQLite3／PDO／RTree通過，無模組的SpatiaLite為UNVERIFIED／exit2；外部SpatiaLite DLL重跑被自動批准審查拒絕後未重試，未用先前測試冒充本輪結果。版本顯示本地dev與base commit，未發布。

實際Textual headless渲染畫面：[寬總覽](previews/tui-overview-wide.svg)、[PHP預覽](previews/tui-php-preview.svg)、[窄畫面](previews/tui-narrow.svg)。這些為Windows本機真實渲染（Linux欄位UNAVAILABLE），不是Linux驗證或美術完成宣稱。

下一步：在另行核准的Linux測試環境驗證唯讀probe的真實配置／版本／權限失敗行為，補足FPM conf.d／pool與服務能力；本輪沒有連VM或碰備份。Startup／Storage表單、完整有效值解析、任何系統套用及部署仍未完成／未授權。

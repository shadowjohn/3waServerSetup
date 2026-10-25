# Read-only Textual TUI Implementation Plan

**Goal:** 可啟動分頁、唯讀本機狀態與草稿預覽；不提供系統套用。

**Architecture:** server_setting/probes.py只讀allowlist命令／檔案；model.py保存欄位與純預覽；app.py Textual UI整合既有候選／PHP smoke。launcher不安裝、不連網。

**Constraints:** 原checkout獨立分支codex/textual-readonly-v1，新D:checkout不編輯。無VM／SSH／安裝／push／系統套用，無秘密憑證讀取。缺Textual本機依賴時回報啟動限制，不偷安裝。

- [x] stdlib tests先涵蓋Windows unavailable、Linux偵測失敗／有效值、未知設定不算健康、純diff、既有firewall與Apache候選整合。
- [x] 實作probe與model，驗證命令allowlist、timeout及不輸出秘密配置。
- [x] Textual Header/Footer、TabbedContent、表單／預覽、寬窄ASCII角色、Exit、唯讀refresh worker程式已寫；SQLite明確按鍵才跑本機memory smoke，不自動載入native module。UI執行仍未驗證。
- [x] run.sh／Python launcher檢查依賴與CLI --status-json／--smoke；不bootstrap。
- [x] 完整unittest、PHP／shell語法、純文字／JSON launcher smoke。結果22 passed、1 skipped；--smoke因缺Textual exit2，不是UI成功。
- [x] 後續使用者明確批准專案venv必要依賴；官方來源／hash／公告核對後安裝Textual8.2.8，未改系統Python。Textual smoke與pilot2項、實際PTY啟動／退出完成。
- [x] 更新使用說明，獨立review完成，修正PHP prepend／append風險及平台warning顯示；未commit／push。

最終本輪結果：24項unittest全部通過，無skip；pip check、compileall、PHP語法通過。修正resize事件新尺寸先於App快取更新的側欄錯誤。寬／窄畫面SVG位於docs/previews。安全無module CLI smoke3項通過，SpatiaLite未驗證；外部DLL重跑遭自動審查拒絕，未重試或繞過。未VM／部署／系統套用／commit／push。

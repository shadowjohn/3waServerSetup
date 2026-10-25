# Release 與更新 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 發布第一個可驗證安裝、升版、程式回退的 Release。
**Architecture:** 固定來源、SSH 簽章 manifest、白名單封包與離線 wheels；版本目錄加 current 指標，共用主機交易鎖，資料留在版本外。
**Tech Stack:** Python stdlib、OpenSSH、Textual、GitHub Actions、locked wheels。
**Spec:** docs/release-distribution.md

## Global Constraints

- 沿用使用者授權，從最新 main 的隔離分支實作，驗證後合回 main、push／tag／publish。
- 不新增未核准第三方 Python 依賴；發布簽章用 OpenSSH 與專用 key。私鑰不可進 Git、log 或 artifact。
- 不擴大既有服務 root write gate；24.04 installer 驗證與服務驗證分別標記。
- `.26` 不重開、不改業務設定；每次僅跑針對新變更所需的檢查。

## Review Focus

- 先驗證來源再執行下載程式，解壓不信任 symlink／traversal／額外檔案。
- update 與 root 交易互斥，舊 console 不能跨版繼續寫入。
- legacy migrate、切換／回退中斷保持可啟動，資料與原 schema 不變。
- 無網路、缺依賴、驗證失敗不破壞舊版；普通啟動無網路。
- 公開版本／commit 與真正 payload 一致；不能以簽名或 CI 代替 native 驗收。

### Task 1: 發行包、bootstrap 與 CI
- [ ] 實作 build_release.py、install.sh template、CI／Release workflow 與 meaningful package／installer 測試。
- [ ] 嚴格 manifest schema、白名單、locked wheels、SHA256／signer namespace；從 tag 產生可用 install.sh。
- [ ] Workflow pin Actions SHA，測試成功才簽署及發布；只有發版 job 有 contents write。

### Task 2: 安裝／更新／回退核心
- [ ] ReleaseManager、standalone installer API、真正簽章與惡意 archive／中斷測試。
- [ ] legacy baseline、venv、非特權 smoke、共用 lock／pending、current 原子切換、回退及恢復。
- [ ] Stable launcher 與相容 worker 入口、舊 console gating。

### Task 3: 版本識別、CLI、TUI
- [ ] 無 Git 讀 RELEASE.json，development fallback 保留。
- [ ] 更新獨立互斥 CLI、檢閱頁、成功退出舊 UI、普通啟動不網路。
- [ ] CLI／UI meaningful mock tests、未授權操作拒絕、錯誤與狀態顯示。

### Task 4: 獨立 review 與實測
- [ ] 整合前獨立跨檔審查；完整 suite、manifest／source proof。
- [ ] `.26` signed fixture：fresh install、legacy migrate、升版／失敗回指／回退，保留全部資料及 runtime。
- [ ] 真 TTY、非 root、舊 console stale；使用者 24 VM ready 後驗證 installer／updater。

### Task 5: 發布與公開回讀
- [ ] 合回 main、提交 push、打 v2026.10.25.001 tag、觀察 Action 完成。
- [ ] 公開 Release 資產簽章／SHA／commit 回讀，透過真實公開來源在 `.26` 安裝／更新檢查。
- [ ] 文件更新已驗證平台、安裝及更新指令、回退邊界。

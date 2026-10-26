# Release／安裝更新驗收

## 2026.10.25.001

公開 tag `v2026.10.25.001` 固定於 `798ed46899ee9cd3cf30a79bec277300cd3db133`。Ubuntu 24.04／26.04 CI 各通過 397 項測試與 headless smoke；正式發布的五個附件已匿名下載，核對固定公鑰簽章、SHA256SUMS、archive／逐檔 hash、Git tag commit 與原始碼。公開程式檔也逐一吻合先前原生受測候選包。

首輪 Actions 在容器內遇到 Git 目錄擁有者檢查，尚未建包／發布即停止。後續 workflow 從 main 接受既有 tag，固定該 tag commit 執行 CI、建包、簽章與發布；只為精確 workspace 提供 safe.directory，發布前再次核對遠端 commit。原 tag 未移動，也沒有覆寫 Release 附件。

Ubuntu 26.04 的真實簽章候選驗收包含新目錄安裝、獨立 venv、非特權 smoke、升版、回退、smoke 故障保留舊版、切換後程序中斷恢復、既有 flat 安裝轉版及 legacy 快照回退。最後再透過真正公開 GitHub 來源升到正式版。原 98 個受保護檔案、81 筆既有交易、boot ID、16 GiB Swap inode／fstab、root LV、BanIP、服務防火牆及 Startup 狀態維持一致；六頁限定服務與 Feather CLI／FPM 真正 SQL load_extension() 驗證通過。

Ubuntu 24.04 新 VM 從公開 `install.sh` 安裝首包，需要時由安裝器準備 Python venv 等先決套件；鎖定 wheels 隨包離線安裝。root／john 的真實 launcher version、完整 commit、update-status 與版本 venv 均通過，原 IP、路由、Netplan、fstab、SSH 公鑰及 boot ID 不變。這項較晚完成的驗收不回寫既有不可變附件；首包 manifest 的 validated_platforms 仍只列 ubuntu26.04。

真正 SSH TTY 驗證 root 的 Check、寬窄畫面 update／rollback 檢閱取消、Ctrl+方向鍵切頁，以及 john 唯讀控制與 140×40／80×24 操作；沒有按主機設定確認。第一輪 john 截圖因 resize 後維持畫面外焦點而讓私有 harness 等待失敗，完整性 guard 通過；改以真實 Tab／Shift+Tab 重新進入焦點後，補跑唯讀操作通過，未修改產品或放寬截圖可見性條件。

原生私有報告、raw TTY、SVG 與工作資料留在開發 `.root-dev/` 及各 VM 的 root 私有證據目錄，不納入發行包。一般程式回退只還原程式與 venv，不恢復主機設定／資料，也不代表 Hyper-V 備份已匯入開機驗證。

## 2026.10.26.001

公開 tag `v2026.10.26.001` 固定於 `4814a124223b6c5786828996ee8acd8859be4149`。Release `40225849332` 的來源解析、Ubuntu 24.04／26.04 各 450 項測試與 headless smoke、建包及發布全部通過。五個公開附件已匿名下載，核對固定公鑰簽章、SHA256SUMS、archive／逐檔 hash、Git tag 完整 commit 與原始碼；公開 runtime 也逐檔吻合原生受測 RC2。

RC2 runtime 來源為 `2c2085a27e8b41a34234e198db1aed7ea5b50a81`。Ubuntu 24.04 的簽章候選 `v2026.10.26.001-rc.2` 已完成 c6 四階段原生驗收，全部 PASS，證據收集沒有警告；後續文件與發行更新沒有改動這份受測 runtime。

Ubuntu 24.04 已由公開 `25.001` 完成首次安裝，再安裝簽章候選 `26.001-rc.1`；root／john 的實際版本、完整 commit、獨立 venv 及 update-status 通過，canonical previous 指標保留原公開 `25.001`，新舊兩份簽章 payload 均逐檔核對。候選安裝的私有驗收腳本曾誤讀公開輸出已過濾的 directory 欄位；安裝成功後改以唯讀方式補驗，沒有重裝、回退或改寫交易。該次 218 項完整基線僅存在 RAM，未保存，不能宣稱其完整前後比對通過；補驗只涵蓋先前獨立保存的 Netplan、fstab、root／john SSH 公鑰、IPv4、路由與 boot ID。

早期固定 IP 試驗發現手動 dhcpcd 與 networkd 同時管理 eth0，候選生效後又加入原 DHCP 位址，嚴格的單一 IPv4 檢查拒絕確認並到期回復。確認程序身份與來源後正常結束該 client，未改開機服務政策；RC2 新增有界程序衝突檢查，在預覽、獨立套用與確認前拒絕外部 client，工具不自行終止程序，安全回復仍可執行。先前私有工具也曾把舊 SSH 斷線、DHCP 換 lease 或啟動逾時誤判為失敗；原失敗證據保留，修正工具後重新完成 c6 全套驗收，未再修改 `2c2085a` runtime。

c6 從 DHCP `.220` 建立完整基線，包含完整 `/etc`、自有三組 DNS 草稿等 1693 個受保護物件、九筆原交易及 Swap。未確認的固定 `.24` 試用真正等待 120 秒，由 PID1 管理的獨立 root guard 自動回復為 DHCP `.181`；新物理 SSH `.24` 曾成功連線，原套用 caller 的確認遭拒。回復後暫存候選移除、pending 為空，原 63-byte Netplan SHA256 `8870a4e4717ea997cd9611c4e2e9093f265d68ffbe0392c76fddca25329091b6`、metadata、全基線及原交易保持一致。

接續由 `.181` 套用固定 `.24`，真正新目標 SSH 執行產品 CLI 確認，驗證 Netplan 持久儲存、無暫存候選、無 pending；再由固定 `.24` 回復 DHCP，新 lease 為 `.125`，重新 SSH 至 `.125` 確認。DHCP 來源最後為語意相同的 canonical JSON，root 擁有、mode0600，不宣稱還原原 YAML 位元組。兩階段 root／john 真實 SSH 版本均吻合 RC2 完整 commit；除受管 Netplan 的預期變更，其餘保護物件、九筆原交易及 Swap inode／大小保持一致。全程 boot ID `00da1bda-d50a-41a6-b1df-54b6c5dda6fe` 未變，沒有重開機驗收。

真正 root SSH TTY 已在 140×40／80×24 操作 Network：填入固定 IP、gateway 與三組 DNS，Refresh 保留草稿，兩種尺寸的檢閱均以 Escape 取消；從 Storage 按鈕 Ctrl+Right 進入 Network，以及 Network／Firewall 往返通過。沒有按 Save 或主機套用確認，正常退出後程式、草稿、Netplan、cloud-init、SSH、IP／路由與 boot ID 完整性核對全數通過。私有 observer 曾無法序列化未選取的 Select 值，另一輪測試誤找其他頁面的按鈕；修正測試工具後重跑通過，產品 runtime 不變。

Ubuntu 24.04 已由真正 root SSH TTY 從公開 `25.001` 更新到公開 `26.001`：Check 核對公開簽章、目標 tag 與完整 commit，Review 後只送出一次外部程式更新確認。舊 App 自行正常退出，結果為 UPDATED／restart_required；新 root／john SSH 的 CLI 版本、update-status、簽章來源與實體版本 venv 均吻合公開 commit。主機設定、網路、boot ID 及原交易完整性核對通過，只新增一筆 APPLIED／UPDATED 的程式更新交易。

最終公開版本已從 DHCP `.125` 套用 `192.168.137.24/24`，gateway 為 `192.168.137.1`，DNS 僅 `192.168.137.1`；由新目標 SSH 執行產品 CLI 確認，驗證來源持久儲存、無暫存候選、無 pending，並儲存與實際設定一致的單 DNS 草稿。新 root／john SSH、完整簽章 payload 及其餘主機保護檢查均通過，十四筆原交易與 Swap 保持不變。另從 `.24` 讀取 Network 狀態／草稿並透過真正 HTTPS GitHub 檢查更新，得到 UP_TO_DATE、signature_verified=true 與完整 commit `4814a124223b6c5786828996ee8acd8859be4149`。全程 boot ID 未變，沒有重開機。

Ubuntu 26.04 已在約 50.179 秒內由公開 `25.001` 更新至 `26.001`，root／john 的真實 CLI、實體版本 venv 與完整公開 commit 均通過，新 john SSH 版本也再次核對成功。更新後完整簽章 manifest、逐檔 hash 與 release-ready 驗證通過；唯讀檢查確認原 98 個檔案 stamp、81 筆原交易狀態、Startup 完整狀態、16 GiB Swap inode／大小、root LV、BanIP、服務防火牆及 Feather 狀態均與基線一致。boot ID `a0d3aa3f-39ce-44f9-83b6-4b9a4ef7b8d4` 未變，最終驗證的 remote_writes 為 0，沒有重啟或關閉 VM。

該 Ubuntu 26.04 主機的 Network 唯讀檢查回報 supported=false，原因精確為 `Custom backend network files require separate integration.`：除 `10-netplan-eth0.network`（235 bytes）外，另有 `/run/systemd/network/zzzz-dracut-default.network`（131 bytes）。保守門檻因此停止，未自動接管、刪除或套用；既有 `192.168.137.26/24` 與主機設定保留。本輪完整 Network 原生寫入端到端驗收在 Ubuntu 24.04 完成，不把這台 Ubuntu 26.04 的額外 backend 現況列為已支援或已寫入；來源整合與遷移需獨立實作與驗證。

此 `26.001` 已發布包的 validated_platforms 列出 Ubuntu 24.04／26.04。Ubuntu 24.04 的原生範圍限於程式安裝、更新與 Network；其他服務管理寫入仍只開放已驗證的 Ubuntu 26.04。這個平台標記沒有放寬各功能的獨立 gate，也沒有回寫既有 `25.001` tag 或附件。此版尚未新增 Ubuntu 22.04 安裝器或主機寫入支援。

## 2026.10.26.002／003

橋接公開 tag [`v2026.10.26.002`](https://github.com/shadowjohn/3waServerSetup/releases/tag/v2026.10.26.002) 固定於 `6601ef4cec1f4b3d7ef6050496c6528a3f094da2`，manifest 保留 Python `>=3.12` 與 Ubuntu 24.04／26.04。Release `40278207651` 的 Ubuntu 24.04／26.04 各 454 項測試、headless smoke、建包及發布全部通過。

正式公開 tag [`v2026.10.26.003`](https://github.com/shadowjohn/3waServerSetup/releases/tag/v2026.10.26.003) 固定於 `beacfde733345c36f2f13563068d5dbb25dfa0f5`，manifest 為 Python `>=3.10` 與 Ubuntu 22.04／24.04／26.04。main CI `40278552252` 及 Release `40279153410` 的三平台各 469 項測試與 headless smoke 全部通過，Release 的來源解析、建包及發布亦全部成功。其他服務管理寫入仍限 Ubuntu 26.04。

兩版各五個公開附件均已匿名下載並通過完整核對：固定公鑰簽章、SHA256SUMS、archive 與逐檔 hash、完整 payload 路徑集合、canonical `RELEASE.json`、公開 tag 完整 commit、與該 commit 的 Git 原始位元組／九個鎖定 wheels 一致，以及由該來源重製的 installer。main 正常推送，未 force-push；既有 tag／附件未移動或覆寫。

公開舊版 `26.001` 在 Ubuntu 26.04 真正執行唯讀 update-check 指定 `003`，實際得到 `INCOMPATIBLE_RELEASE`，未變更主機。這證實舊更新器需要先指定 `002` 橋接、結束舊 console 並重開，才能更新到 `003`；流程與指令見[安裝操作](installation.md#舊版更新器的相容性)，沒有自動橋接。

三台共五次指定 tag 的公開更新均已通過真正產品 CLI Check／Plan／Apply，最終全部為公開 `26.003`／commit `beacfde733345c36f2f13563068d5dbb25dfa0f5`：

| VM | 公開更新路徑 | 保護結果 |
|---|---|---|
| Ubuntu 22.04 `.22` | `003-rc.3 → 003` | 23 個關鍵物件、7 筆原交易保持一致，新增 1 筆 APPLIED release-update |
| Ubuntu 24.04 `.24` | `001 → 002 → 003` | 23 個關鍵物件、15 筆原交易保持一致，新增 2 筆 APPLIED release-update |
| Ubuntu 26.04 `.26` | `001 → 002 → 003` | 121 個關鍵物件含原 98 個保護路徑、89 筆本輪原交易保持一致，新增 2 筆 APPLIED release-update |

每次升版後以新 root／john SSH 核對版本、完整 commit、實體版本 venv、完整簽章 payload／ready，以及公開 manifest、archive、installer 的 hash 承諾。這五次程式升版期間，三台的 boot、IP、路由、DNS、關鍵檔案 hash／mode／uid／gid、儲存草稿及受保護服務狀態均與各自本輪基線一致；原交易的 ID／status／kind／module 保持不變，新增交易全為已終結的 release-update。

五次更新後另以新 root／john SSH 完成三台純本機補核，均為正式 `003`，完整簽章與本輪主機保護再次通過。這項補核涵蓋實體版本 venv、CLI version／update-status 及主機狀態，未重新送出 update-apply。

指定 tag 更新完成後，三台預設 latest CLI 補驗因真正 GitHub 匿名 API 限流而未全數完成。Ubuntu 24.04 查詢回報 `DOWNLOAD_FAILED`；進一步診斷回應為 HTTP403、limit60／remaining0，該次回應列出的重設時間為 `1792999085`（2026-10-26 15:18:05，Asia/Taipei）。因此未取得完整的三台預設 latest `UP_TO_DATE` 證據，純本機補核也不代表通過這項 API 查詢。

官方 `/latest/download/` 路徑已另行匿名下載 `install.sh`、manifest 與簽章，三檔 hash 全部等同已驗證的公開 `003` 附件。公開發行、latest 下載路徑、完整附件驗證與五次實際升版均已完成；限流後的預設 CLI latest 查詢仍待補驗。

Ubuntu 26.04 的本輪基線觀察到 BanIP 非持久 kernel set 缺失，status 為 unsupported、pending 為空；後續保護此既有狀態，沒有恢復 blacklist 或宣稱 active。原 98 個保護路徑取自既有紀錄，但以本輪即時 stamp／boot 建立新基線，沒有拿先前驗收的 boot ID 當成本輪前置條件。

私有證據包括 `.root-dev/public-ubuntu22-v2026.10.26.002-5d5b3ed5c954/proof.json`、`public-ubuntu22-v2026.10.26.003-3b06a87777f1/proof.json`、`public-latest-download-46e1ddb4b646/proof.json`、`old001-rejects003-vm26.json` 與 `latest-api-diagnostic.json`。五次原生更新 receipt 為 `publish-vm24-update-20261026T062557Z-98bc88f80d5a.json`、`publish-vm24-update-20261026T063137Z-0caf7870f96b.json`、`publish-vm26-update-20261026T063137Z-b0a947b63831.json`、`publish-vm26-update-20261026T063504Z-bdd1d00df286.json`、`publish-vm22-update-20261026T063504Z-31b308718616.json`。

最終純本機補核 receipt 為 `publish-vm22-check-20261026T064735Z-5fb090a1da43.json`、`publish-vm24-check-20261026T064735Z-914b762f2576.json`、`publish-vm26-check-20261026T064735Z-95ab0323fdd8.json`；上述私有證據均不納入發行包。

## 2026.10.27.001：SpatiaLite 驗收入口與舊版直接更新

正式 tag `v2026.10.27.001` 指向 `520b55d629b8427de74fcd82740f5a0c8025559f`，於 2026-10-27 00:33（Asia/Taipei）公開。main CI `40359399380` 與 Release `40359398839` 成功；Release 的 Ubuntu 22.04／24.04／26.04 各完成 485 項及 headless smoke。CI 現安裝 PHP CLI，新增入口／公式測試有真正執行；這與 `.26` 的原生 120,000 筆 CLI／FPM 驗收分別記錄，不互相替代。本機 Windows 全套另為 485 項、0 失敗、29 個平台條件略過。

本版加入官方 `libsqlite3-mod-spatialite` 套件選項；完整驗收沿用 `tests/sqlite-smoke.php --acceptance`，原快速檢查保持原行為。新說明置於 `docs/sqlite-smoke.md`，standalone wrapper 留在 Git source，未新增舊 updater 不接受的測試路徑，也未放寬更新器的封包邊界。本地候選已用原 `002`、`003` manifest validator 直接接受；公開後再用真正 `003` 預覽新版本。主機 symlink／PHP ini 不由程式更新自動套用，GDAL 僅收錄比較結果，未安裝新轉換參數。

五個正式附件已重新匿名下載，pinned SSH signature、SHA256SUMS、archive 與完整 Git source 路徑集合／逐檔 bytes、9 個 locked wheels、canonical RELEASE.json、由原 source 重建的 installer 均通過。公開 archive SHA256 為 `7047008213381988ddabd6949aeb5ffe9f2e4ff80a98f3c198bc8753a02ce702`；沒有私有 helper、key、venv 或主機備份入包。證據：ignored `.root-dev/public-ubuntu22-v2026.10.27.001-b0f3f8a7c766/proof.json`。

`.26` 仍保留 `v2026.10.26.003`，root 與獨立 john SSH 皆確認原版本。舊版的預設 latest check 回傳 `UPDATE_AVAILABLE`，明確 tag 的 plan 指向 `27.001`、完整來源 commit 與已驗證簽章。227 個受保護物件、101 筆交易、boot／IP、FPM、兩份 SpatiaLite ini、symlink 與 Feather 等狀態前後一致；明確 VM 寫入 0，未呼叫 Apply，讓使用者自行測試升版。BanIP 保留原本 kernel set 缺失的 unsupported 狀態，未宣稱恢復。

本輪只驗證 `.26` 的公開更新偵測／預覽，沒有把它記成實際升版，也沒有推定 `.22`／`.24` 已執行本版。私有證據：`.root-dev/release26-readonly-baseline-377ce40f6aa4.json`、`release26-readonly-check-447ac774542d.json`；SpatiaLite 新入口的真正四項驗收見 [PHP SpatiaLite](spatialite-acceptance.md#2026-10-27-發行入口相容驗收)。

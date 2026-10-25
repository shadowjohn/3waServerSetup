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

已發布包的 validated_platforms 列出 Ubuntu 24.04／26.04。Ubuntu 24.04 的原生範圍限於程式安裝、更新與 Network；其他服務管理寫入仍只開放已驗證的 Ubuntu 26.04。這個平台標記沒有放寬各功能的獨立 gate，也沒有回寫既有 `25.001` tag 或附件。Ubuntu 22.04 仍延後，未新增安裝器或主機寫入支援。

# Release／安裝更新驗收

## 2026.10.25.001

公開 tag `v2026.10.25.001` 固定於 `798ed46899ee9cd3cf30a79bec277300cd3db133`。Ubuntu 24.04／26.04 CI 各通過 397 項測試與 headless smoke；正式發布的五個附件已匿名下載，核對固定公鑰簽章、SHA256SUMS、archive／逐檔 hash、Git tag commit 與原始碼。公開程式檔也逐一吻合先前原生受測候選包。

首輪 Actions 在容器內遇到 Git 目錄擁有者檢查，尚未建包／發布即停止。後續 workflow 從 main 接受既有 tag，固定該 tag commit 執行 CI、建包、簽章與發布；只為精確 workspace 提供 safe.directory，發布前再次核對遠端 commit。原 tag 未移動，也沒有覆寫 Release 附件。

Ubuntu 26.04 的真實簽章候選驗收包含新目錄安裝、獨立 venv、非特權 smoke、升版、回退、smoke 故障保留舊版、切換後程序中斷恢復、既有 flat 安裝轉版及 legacy 快照回退。最後再透過真正公開 GitHub 來源升到正式版。原 98 個受保護檔案、81 筆既有交易、boot ID、16 GiB Swap inode／fstab、root LV、BanIP、服務防火牆及 Startup 狀態維持一致；六頁限定服務與 Feather CLI／FPM 真正 SQL load_extension() 驗證通過。

Ubuntu 24.04 新 VM 從公開 `install.sh` 安裝首包，需要時由安裝器準備 Python venv 等先決套件；鎖定 wheels 隨包離線安裝。root／john 的真實 launcher version、完整 commit、update-status 與版本 venv 均通過，原 IP、路由、Netplan、fstab、SSH 公鑰及 boot ID 不變。這項較晚完成的驗收不回寫既有不可變附件；首包 manifest 的 validated_platforms 仍只列 ubuntu26.04。

真正 SSH TTY 驗證 root 的 Check、寬窄畫面 update／rollback 檢閱取消、Ctrl+方向鍵切頁，以及 john 唯讀控制與 140×40／80×24 操作；沒有按主機設定確認。第一輪 john 截圖因 resize 後維持畫面外焦點而讓私有 harness 等待失敗，完整性 guard 通過；改以真實 Tab／Shift+Tab 重新進入焦點後，補跑唯讀操作通過，未修改產品或放寬截圖可見性條件。

原生私有報告、raw TTY、SVG 與工作資料留在開發 `.root-dev/` 及各 VM 的 root 私有證據目錄，不納入發行包。一般程式回退只還原程式與 venv，不恢復主機設定／資料，也不代表 Hyper-V 備份已匯入開機驗證。

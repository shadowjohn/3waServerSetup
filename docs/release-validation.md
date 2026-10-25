# Release／安裝更新驗收

## 2026.10.25.001

公開 tag `v2026.10.25.001` 固定於 `798ed46899ee9cd3cf30a79bec277300cd3db133`。Ubuntu 24.04／26.04 CI 各通過 397 項測試與 headless smoke；正式發布的五個附件已匿名下載，核對固定公鑰簽章、SHA256SUMS、archive／逐檔 hash、Git tag commit 與原始碼。公開程式檔也逐一吻合先前原生受測候選包。

首輪 Actions 在容器內遇到 Git 目錄擁有者檢查，尚未建包／發布即停止。後續 workflow 從 main 接受既有 tag，固定該 tag commit 執行 CI、建包、簽章與發布；只為精確 workspace 提供 safe.directory，發布前再次核對遠端 commit。原 tag 未移動，也沒有覆寫 Release 附件。

Ubuntu 26.04 的真實簽章候選驗收包含新目錄安裝、獨立 venv、非特權 smoke、升版、回退、smoke 故障保留舊版、切換後程序中斷恢復、既有 flat 安裝轉版及 legacy 快照回退。最後再透過真正公開 GitHub 來源升到正式版。原 98 個受保護檔案、81 筆既有交易、boot ID、16 GiB Swap inode／fstab、root LV、BanIP、服務防火牆及 Startup 狀態維持一致；六頁限定服務與 Feather CLI／FPM 真正 SQL load_extension() 驗證通過。

Ubuntu 24.04 新 VM 從公開 `install.sh` 安裝首包，需要時由安裝器準備 Python venv 等先決套件；鎖定 wheels 隨包離線安裝。root／john 的真實 launcher version、完整 commit、update-status 與版本 venv 均通過，原 IP、路由、Netplan、fstab、SSH 公鑰及 boot ID 不變。這項較晚完成的驗收不回寫既有不可變附件；首包 manifest 的 validated_platforms 仍只列 ubuntu26.04。

真正 SSH TTY 驗證 root 的 Check、寬窄畫面 update／rollback 檢閱取消、Ctrl+方向鍵切頁，以及 john 唯讀控制與 140×40／80×24 操作；沒有按主機設定確認。第一輪 john 截圖因 resize 後維持畫面外焦點而讓私有 harness 等待失敗，完整性 guard 通過；改以真實 Tab／Shift+Tab 重新進入焦點後，補跑唯讀操作通過，未修改產品或放寬截圖可見性條件。

原生私有報告、raw TTY、SVG 與工作資料留在開發 `.root-dev/` 及各 VM 的 root 私有證據目錄，不納入發行包。一般程式回退只還原程式與 venv，不恢復主機設定／資料，也不代表 Hyper-V 備份已匯入開機驗證。

## 2026.10.26.001

初版 Network commit `b2dc7111a09668653a4edd0edbfd61d374b512c9` 的 CI `40213922576` 在 Ubuntu 24.04／26.04 各通過 441 項測試與 headless smoke。原生測試後另補外部 DHCP client 衝突檢查；正式 tag 尚未建立，最終 runtime 仍須重新驗證，正式包以實際 tag 的完整 commit 建置、驗簽及核對程式檔案。

Ubuntu 24.04 已由公開 `25.001` 完成首次安裝，再安裝簽章候選 `26.001-rc.1`；root／john 的實際版本、完整 commit、獨立 venv 及 update-status 通過，canonical previous 指標保留原公開 `25.001`，新舊兩份簽章 payload 均逐檔核對。候選安裝的私有驗收腳本曾誤讀公開輸出已過濾的 directory 欄位；安裝成功後改以唯讀方式補驗，沒有重裝、回退或改寫交易。該次 218 項完整基線僅存在 RAM，未保存，不能宣稱其完整前後比對通過；補驗只涵蓋先前獨立保存的 Netplan、fstab、root／john SSH 公鑰、IPv4、路由與 boot ID。

Network 原生試用另外先保存完整主機基線。v2 timeout 驗收已通過真正 120 秒未確認試用與 watchdog 自動回復，完整 `/etc` 基線、原 63-byte Netplan 來源與 metadata、既有受保護物件及原交易保持不變，暫存候選移除，回到原 DHCP。Windows 與 Ubuntu 26.04 對新 `.24:22` 的 TCP 探測通過；v2 也已從新物理 SSH 連到 `.24` 並完成認證。原套用 caller 的確認遭拒，獨立 guard 為 PID1 管理的 root 程序。新連線及認證成功不等同於已完成產品確認或持久儲存。

固定 IP 確認試驗另外抓到手動 dhcpcd 與 networkd 同時管理 eth0：候選生效數秒後，dhcpcd 又加入原 DHCP 位址並改寫預設路由，嚴格的單一 IPv4 檢查拒絕確認，120 秒後成功回復。已依程序 executable、啟動時間、工作階段及日誌確認來源，正常結束該 client，未改開機服務政策；networkd 隨後取得不同的 DHCP lease `.22`，以原主機金鑰與 MAC 核對同一台 Ubuntu 24.04，SSH 可連。原 Netplan、1692 項受保護物件與六筆原交易保持不變。修正版新增有界程序檢查，在預覽、獨立套用與確認前拒絕外部 client；工具不終止程序，安全回復仍可執行。

待補驗收：從新目標 SSH 執行產品確認並驗證 Netplan 持久儲存；透過產品回復 DHCP 並由新 SSH 確認；完成試用期間狀態觀測的分析。目前不宣告 Network 整體驗收完成，確認及回復驗收完成前不建立正式 tag。發布後另補五個公開附件的簽章／hash／原始碼核對，以及由公開 `25.001` 真正 TTY 經 Check、Review、單次程式更新確認、自動退出及新版本 root／john CLI 的完整升級證據；目前不列為已完成。

下一個正式包的 validated_platforms 將列 Ubuntu 24.04／26.04。Ubuntu 24.04 的原生範圍限於程式安裝、更新與 Network；其他服務管理寫入仍只開放已驗證的 Ubuntu 26.04。這個平台標記不會放寬各功能的獨立 gate，也不回寫既有 `25.001` tag 或附件。

# BanIP 下載、執行與服務防火牆共存

使用者明確要求將 https://3wa.tw/myapache_banip/mybanip.php 的 Linux `my_ban_ips.sh` 加入工具，並在 `.26` 下載執行一次。前一輪服務防火牆、Swap、LVM 已完成 232 項測試與原生／TTY 驗收；這筆新增黑名單操作不重新啟用服務白名單政策，不加 cron 或開機排程。

## 已讀來源

- 網頁實際下載連結：`https://3wa.tw/myapache_banip/api.php?mode=download_script&type=linux`。
- 原腳本 2584 bytes，SHA256 `2fc1b6592fd881a13bb34236ed8408ffc2a60e4e610a2c355ad9789aaa89dd99`。只建立 hash:ip IPv4 ipset `blacklist_new`、原子 swap `blacklist`，確保一條 INPUT DROP，不改預設 policy、不 flush 他人規則。
- 清單 `https://3wa.tw/myapache_banip/data/banip_csv.txt` 此次唯讀樣本 353497 bytes、24851 個唯一有效 IPv4，沒有 private/loopback／目前 SSH 來源。網站內容會更新，執行當次以實際清單驗證。
- 清單末行沒 newline，原 Bash while loop 會漏最後一筆。執行版只將唯一 `while IFS= read -r line; do` 改成 `while IFS= read -r line || [ -n "$line" ]; do`；保留原文／原 SHA，另記執行版 2602 bytes SHA `8974de314b76b925d6d50325b475ad827602cd49d28fbf3b3d780276897bb8a6`。不修改外部網站。

## 操作契約

- Root／Ubuntu 26.04，只接受固定 HTTPS 來源與已審原腳本 SHA。新版本變更須重新檢閱，不執行未知下載內容。
- 下載原文保存在 root 私有資料目錄；執行版 `/root/my_ban_ips.sh` root:root 0700。既有不同檔案不覆寫。缺少必要 ipset／curl 依賴由明確計畫列出固定套件安裝。
- 在 systemd 有界 cgroup 與私有小型 tmpfs `/tmp` 執行，避免原腳本固定暫存路徑遭符號連結或其他使用者干擾；不直接 curl pipe 到 shell。
- 第一個 kernel 變更前，獨立 systemd watchdog 已啟動並確認 ready。先準備 exact DROP 與空集合，將 loopback／實際 SSH 來源及埠的救援規則放在 DROP 前；原腳本查到 DROP 就不會重新插到救援前。
- 黑名單原子替換；執行後核對實際清單、完整 membership count／SHA、唯一規則及救援順序。以新實體 SSH 工作階段確認；原連線不能自行解除回復。
- 失敗／逾時僅回復本工具的 set／rules 與原狀；不清空整個 filter 表或覆寫外部修改。未知 `blacklist`／`blacklist_new` 或同名 policy 衝突停止。
- 透過私有完整 attestation 辨識 iptables-nft 的 companion filter table；服務防火牆僅在精確 owner／規則／policy 驗證通過時容許共存，未知 foreign hooks 仍拒絕。
- IPv4 黑名單不宣稱封鎖 IPv6；與服務 INPUT default-deny 為獨立操作。此次依使用者明確新指令保留 runtime 黑名單生效，服務防火牆仍保持未啟用。

## 驗收

固定原文／執行版 SHA、無 newline 完整讀取、unknown download 拒絕、先 guard 後 mutation、舊 SSH 拒 confirm、依賴與 actor gate、更新失敗恢復與外部修改拒絕、companions 共存。原生執行一次，核對 ipset 全量與一條規則、新 SSH 連線；以隔離 namespace 黑名單來源 HTTP 拒絕／正常來源允許；不變更 eth0、SSH、Swap 或 root LV。不自動排程。

實作時上游已更新為 2663 bytes（SHA256 `88f0b6b63c1545816e1475b732b4050220db58618ddff8608fd862dc81212036`），包含末行修正；重新檢閱後固定新 pin。實際執行版僅將每行 `tr`／`xargs` 改成 Bash builtin 修剪，2726 bytes（SHA256 `1fae776d2be394e531fc6f051f74b79c8ef8823211b986e60f3c9d79926cfc0c`）。原生完整 24,851 筆、新 SSH 確認與 namespace 真封鎖均通過，詳見 [Ubuntu 驗收](../../ubuntu26-root-validation.md)。上方最初來源數字保留作設計當時紀錄。

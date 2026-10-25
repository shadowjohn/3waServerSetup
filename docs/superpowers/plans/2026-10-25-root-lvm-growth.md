# 2026-10-25 根目錄 LVM 擴容

使用者要求讓工具提供 `lvextend -r -l +100%FREE /dev/ubuntu-vg/ubuntu-lv` 的便利操作。本輪新增根 LV 偵測、指定 GiB 增量／使用全部 VG free、檢閱與原生試算／套用，不自動串接 swap 建立。

- 僅支援原生 Ubuntu 26.04、可寫 ext4 根目錄、普通 public linear LV、非加密／MD／thin／snapshot 堆疊。比對 findmnt 的 block major/minor、LVM 報表及實際 block node，不硬編碼 ubuntu-vg。
- 顯示目前 LV、filesystem、VG free、目標容量及無法自動縮回。只消耗現有 VG extents，不新增 PV、不修改 partition 或虛擬磁碟。
- 使用 `lvextend --resizefs`，執行已檢閱的絕對 extent 數，避免預覽後 VG 新增空間被 `+100%FREE` 一併吃掉。套用前重新比對幾何與身分。
- `--test` 只驗證 LVM 配置，不搭配 resizefs，不以此宣稱檔案系統已擴容。
- 每次操作 journal 記錄原大小、目標及 UUID；完成後重新讀 LVM 與 ext4 block geometry。若 LV 已大但 filesystem 未完成，明示部分完成，提供僅 `resize2fs` 的 recovery，禁止重跑增量。
- 使用共享 root 交易鎖與 pending 防護；不提供 lvreduce 或「還原縮容」。

實測先對現有 root LV 跑原生唯讀盤點／試算，再用 root 私有 loop image 建立獨立測試 VG/LV/ext4，透過同一交易引擎驗證實際擴容與 filesystem recovery。測試裝置與 root LV 明確分開，結束核對 loop backing file 後清除。真正根 LV 擴容為另外一筆使用者檢閱的不可逆操作。

初始 VM：根 LV `/dev/ubuntu-vg/ubuntu-lv`，19834863616 bytes；VG free 同為 19834863616 bytes，4729 extents，每 extent 4194304 bytes。根目錄 ext4，PV `/dev/sda3`。

參考：[lvextend(8)](https://www.man7.org/linux/man-pages/man8/lvextend.8.html)、[Red Hat linear LV management](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/configuring_and_managing_logical_volumes/basic-logical-volume-management)。

# Swap 容量、啟用與開機設定

Disk / Swap 頁可管理 `/swapfile` 或已安裝的 `/swap.img`。首次讀取保留現有檔名與容量；`Load recommended` 只把建議值 `16384 MiB`（16 GiB）載入草稿，不直接建立或擴大檔案。

三個目標分開設定：容量 MiB、立即啟用、開機啟用。Save／Load 只處理配方；Review & apply 列出實際檔案、現況、目標及選定的 fstab 條目，再確認執行。若現有檔案大小不是整 MiB，未調整容量時保留原始 bytes，不因顯示值而偷偷重建。

開機持續使用 `/etc/fstab`，以唯一的 `/swapfile none swap sw,nofail 0 0` 或 `/swap.img` 條目管理；保留其他行、原有換行與 metadata。不新增 rc.local 流程。其他 swap 檔或分割區不會被一併 swapoff。

## 調整流程

- Ubuntu 26.04、root、本機 ext4；不自動支援 Btrfs／CoW、網路檔案系統或未知 swap units。
- 已存在的檔案須 root:root 0600、單一 link、有效 swap signature、可證明配置，並有 kernel／fstab／工具私有紀錄可核對；未知檔與 symlink 停止。
- 先在同一檔案系統建立完整零填充、0600 的候選，需容納整個新容量並額外保留 1 GiB。即使縮小，也不先刪原檔湊空間。
- swapoff 前重新檢查 `MemAvailable` 必須大於該 swap 已用量加 256 MiB。失敗時保留原檔；不使用 `swapoff -a`。
- 原 inode 保留到新檔、kernel 啟用與 fstab 驗證成功；成功後清除已停用的原檔，避免長期佔空間與保留舊 RAM 內容。
- 失敗可依交易 ID 恢復尚保留的原 inode。若 swapoff 失敗，保留使用中的檔案，明示需恢復，不刪除它。

成功調整容量後不保留舊 swap 資料；要改回舊容量，重新預覽該容量即可。未啟用的檔案僅能確認 signature／配置／fstab，實際 kernel 啟用需在選擇啟用時另外驗證。

## CLI

```sh
/opt/3waServerSetting/run.sh --swap-status
/opt/3waServerSetting/run.sh --swap-status --swap-path /swap.img
/opt/3waServerSetting/run.sh --swap-plan --swap-path /swap.img --size-mib 16384 --swap-active on --swap-persistent on
/opt/3waServerSetting/run.sh --swap-save --swap-path /swap.img --size-mib 16384 --swap-active on --swap-persistent on --confirm
/opt/3waServerSetting/run.sh --swap-apply --swap-path /swap.img --size-mib 16384 --swap-active on --swap-persistent on --confirm
/opt/3waServerSetting/run.sh --swap-recover TRANSACTION_ID --confirm
```

配方位於 `/opt/3waServerSetting/config/swap/draft.json`；所有權紀錄位於同一私有目錄的 `owned.json`，不更動公共範本目錄權限。journal 與小檔設定備份沿用 root 管理交易目錄，不備份整份 swap paging data。

`.26` 原有 `/swap.img` 3813 MiB 保留；16 GiB 建議值需要足夠磁碟空間。根 LV 可另用 [LVM 擴容](root-lvm.md) 增加現有 VG 空間，兩個動作各自檢閱，不自動串接。

參考：[swapon(8)](https://man7.org/linux/man-pages/man8/swapon.8.html)、[systemd.swap](https://www.freedesktop.org/software/systemd/man/latest/systemd.swap.html)。原生驗收見 [Ubuntu 26.04 root 驗收](ubuntu26-root-validation.md)。

# 根目錄 LVM 擴容

Disk / Swap 頁會讀取根目錄實際 LV、檔案系統大小與 VG 剩餘空間，提供「增加指定 GiB」與「使用全部 VG 剩餘空間」。不硬編碼 ubuntu-vg，也不自動修改虛擬磁碟、partition 或 PV。

這對應使用者慣用的操作：

```sh
lvextend -r -l +100%FREE /dev/ubuntu-vg/ubuntu-lv
```

工具實際執行 `lvextend --resizefs --extents <已檢閱的絕對 extent 數> <實際根 LV>`；若預覽後 VG 改變則拒絕，避免 `+100%FREE` 順便耗掉未檢閱的新空間。

支援 Ubuntu 26.04、可寫 ext4 根目錄及普通 public linear LV。thin、snapshot、striped、加密／MD／其他疊層或未知身分只回報限制。`findmnt` 的 major/minor、實際 block node、LV/VG UUID、PV 與原生 LVM 報表都須一致。

擴容無法用設定還原縮回。Review 明列不可逆、目前／目標容量與確切命令，確認後才執行。完成後重新核對 LV 與 ext4 block geometry；若 LV 已變大但 filesystem 失敗，保留 journal 並顯示 recovery 指令，僅補 `resize2fs`，不重跑增量。

```sh
/opt/3waServerSetting/run.sh --lvm-status
/opt/3waServerSetting/run.sh --lvm-plan --grow-mode all
/opt/3waServerSetting/run.sh --lvm-plan --grow-mode add --add-gib 8
/opt/3waServerSetting/run.sh --lvm-check --grow-mode all --confirm
/opt/3waServerSetting/run.sh --lvm-apply --grow-mode all --confirm
/opt/3waServerSetting/run.sh --lvm-recover TRANSACTION_ID --confirm
```

`--lvm-check` 使用原生 LVM `--test` 並排除 `--resizefs`，不更動容量，也不代表檔案系統擴容已驗證。若 VG 沒剩餘 extents，使用全部空間會回報 no-op。

`.26` 初始根 LV 約 18.47 GiB，VG 另有約 18.47 GiB 可用。根 LV 先做唯讀盤點與試算；真正擴容以獨立 loop VG/LV/ext4 驗證，原生結果見 [Ubuntu 26.04 root 驗收](ubuntu26-root-validation.md)。

參考：[lvextend(8)](https://www.man7.org/linux/man-pages/man8/lvextend.8.html)。

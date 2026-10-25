# MariaDB root 密碼與外連

Ubuntu 26.04 的 MariaDB 頁增加獨立的 **Root access** 區塊。這次依使用者明確選擇管理 `root@'%'`；來源限制由既有防火牆處理，工具不修改防火牆。

在 VM 以 root 執行 `/opt/3waServerSetting/run.sh`，切到 MariaDB：

1. 於 New root password／Confirm password 輸入新密碼；留白表示保留目前密碼。
2. 選擇遠端設定 Keep／On／Off。
3. 按 Apply root access，檢閱後確認。

密碼欄遮蔽顯示，送出檢閱前即清空。檢閱只顯示「是否改密碼」，沒有明文或認證 hash。

| 選擇 | 行為 |
| --- | --- |
| Keep | 保留監聽設定、遠端帳號是否存在、lock 與 grants。若輸入新密碼，只更新本機 root 及已存在且受支援的 root@% 密碼 |
| On | 綁定 IPv4 0.0.0.0:3306，準備並啟用 root@%，授予 ALL PRIVILEGES ON *.* WITH GRANT OPTION。先準備 locked 帳號及網路，驗證後最後解鎖 |
| Off | 先鎖定 root@%，監聽改回 127.0.0.1:3306 並重啟服務；保留本機 TCP 與 socket 管理。只關閉外連不需重新輸入密碼 |

本機 `root@localhost` 保留 unix_socket，並設定／更新 mysql_native_password 備用認證。On 未輸入新密碼時，可重用已設定的本機 native 密碼；尚未設定則需先輸入。狀態 API 分別判斷外連、密碼是否已設定與完整管理權限；畫面列出監聽、帳號 lock 與本機密碼狀態，不把空密碼或未知認證方式誤當成「外連已關閉」。

網路設定獨立寫入 `/etc/mysql/mariadb.conf.d/99-3wa-server-access.cnf`。原本慢查詢三欄的配方仍用既有檔案，彼此不覆寫。

## 密碼與失敗處理

密碼不放 argv、環境變數、配方、預覽、工具日誌或設定備份。MariaDB 本身仍保存原生認證資料。管理 SQL 經 stdin，敏感語句送出前先確認 session 的 general／slow／binary logging guard；存在 active audit plugin 時拒絕操作。新密碼用 0600 匿名 memfd 傳入本機 TCP client，避免 socket 免密登入被當成密碼驗證。

發生已處理的失敗會以記憶體內原始認證補償，並還原網路檔案；交易記錄只有非秘密摘要。一般 **Restore last 不會還原 root 密碼、帳號或這份 access 網路設定**。若程序被中斷，需透過保留的本機 root socket 檢查私有交易記錄及重新設定，不會從磁碟讀回舊密碼。

目前限定 native Ubuntu MariaDB 單一 instance、標準 unit、3306 TCP、bind-address 為 127.0.0.1 或 0.0.0.0。socket-only、Galera、自訂 unit／未知監聽與不支援的認證方式在修改前拒絕。

## CLI

```sh
./run.sh --mariadb-security-plan --remote on --change-root-password
./run.sh --mariadb-security-apply --remote on --change-root-password --confirm
./run.sh --mariadb-security-apply --remote off --confirm
```

密碼操作在真實終端遮蔽輸入兩次；不接受明文密碼參數。自動化可明確加 `--password-stdin`，讀取一行最多 4096 UTF-8 bytes；不要把實際密碼寫入 shell 歷史或來源檔。

原生規則參照 [MariaDB 多重認證](https://mariadb.com/docs/server/security/user-account-management/authentication-from-mariadb-10-4)、[ALTER USER](https://mariadb.com/docs/server/reference/sql-statements/account-management-sql-statements/alter-user) 與 [帳號鎖定](https://mariadb.com/docs/server/security/user-account-management/account-locking)。

## VM 驗收

2026-10-25 已在 192.168.137.26 使用臨時隨機密碼驗證：

- Windows PHP client 真正連入，CURRENT_USER() 為 root@%，SELECT 1 成功；錯密碼 1045 拒絕。
- 新密碼輪替後舊密碼拒絕、新密碼成功；保留 unix_socket 管理。
- Off 鎖定 root@% 並切回 loopback，Windows 外部連線 2002 拒絕；Keep 可保留 locked 帳號與其他使用者的 wide listener。
- 密碼驗證故障注入後，原本密碼仍可遠端登入，補償成功。
- 啟用原生 general log 進行測試，掃描工具 journal／配方／備份與原生 general／slow log 共 100 個檔案，未找到測試明文密碼或其認證 hash。

測試完成已恢復原生 socket 管理、loopback、沒有 root@% 及沒有正式 root native 密碼的初始狀態。臨時測試密碼沒有留作登入憑證；請在介面輸入自己的正式密碼。

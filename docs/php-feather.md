# 羽山版 PDO SQLite

`Install packages` 頁下方提供獨立的羽山版編譯與啟用區。目標是讓既有 PDO 程式直接透過 SQL `SELECT load_extension(...)` 載入 SQLite 原生 `.so`，不需在應用程式加入新的 PDO constructor option。

Root 可分別勾選 CLI、FPM，先檢閱再執行編譯與啟用。未勾選的範圍保留現況。只支援目前已核對的 Ubuntu 26.04、單一 PHP 8.5 NTS／amd64 ABI 20250925 與單一 www FPM pool；不猜測其他 PHP／pool 的路徑。

編譯使用與已安裝 PHP 套件完全相同的 Ubuntu source package。從主機既有 Ubuntu archive／簽章 keyring 建立私有 deb-src cache，驗證 APT Sources、dsc 與 tar SHA256；不修改系統 sources。缺少的固定開發套件會列入預覽。來源解壓、phpize、configure、make 與原生 SQL probe 均由 `threewa-php-build` 系統帳號在無網路、有時限的 systemd cgroup 執行。

模組加入 root 控制的 `pdo_sqlite.feather_sql_load_extension`（`PHP_INI_SYSTEM`），預設 Off；僅選定的 SAPI 啟用時設 On。產物存於 `/opt/3waServerSetting/modules/feather-pdo-sqlite/<hash>/`，原 `/usr/lib/php/20250925/pdo_sqlite.so` 保留不覆寫。CLI／FPM 各自切換受管 `20-pdo_sqlite.ini`，FPM 經原生 config test、reload 與 Unix FastCGI probe 驗證。

驗證會編譯自有 SQLite extension，再由真正的 SQL load_extension() 載入及呼叫其中函式；另外檢查 gate Off／官方模組拒絕載入、一般 PDO 操作與新連線未繼承已載入函式。狀態不以 method_exists() 代替 SQL 載入驗收。

```sh
/opt/3waServerSetting/run.sh --php-feather-status
/opt/3waServerSetting/run.sh --php-feather-plan --feather-cli on --feather-fpm on
/opt/3waServerSetting/run.sh --php-feather-apply --feather-cli on --feather-fpm on --confirm
/opt/3waServerSetting/run.sh --php-feather-restore --confirm
```

`Restore stock` 恢復第一次啟用前的官方 ini alias，之後多次編譯也保留這個 baseline。建置／啟用失敗與主動還原分別處理；開發套件、系統 builder 帳號與已驗證版本產物不因 ini 還原而移除。PHP 套件／ABI 更新後要重新編譯，不能直接沿用舊 `.so`。

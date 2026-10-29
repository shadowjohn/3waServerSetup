# PHP SpatiaLite 雙驅動驗收

SpatiaLite 使用 Ubuntu 官方 `libsqlite3-mod-spatialite`，`spatialite-bin` 是另一個 CLI 套件。`Install packages` 的 GIS 區可勾選原生載入模組；套件安裝完成與 PHP 能載入模組分別驗證。

Linux 使用 `.so`。本次 Ubuntu 26.04 amd64 的官方模組為 `/usr/lib/x86_64-linux-gnu/mod_spatialite.so`；依使用者指定，建立 root 管理的 `/var/www/sqlite3_ext`，其中 `mod_spatialite.so` symlink 指向官方套件的同名路徑。套件更新仍由 APT 管理，不複製舊主機的二進位檔，也不將 Windows `.dll` 改名使用。不同架構請依 `dpkg -L libsqlite3-mod-spatialite` 確認本機檔案。

兩種載入方式各有前提：

- PDO 保留 `new PDO('sqlite::memory:')` 與 SQL `SELECT load_extension('/var/www/sqlite3_ext/mod_spatialite.so')`。此需求仍使用已啟用的[羽山版 PDO SQLite](php-feather.md)，不改成 `Pdo\Sqlite` 或其他載入 API。
- SQLite3 保留 `new SQLite3(':memory:')` 與 `$db->loadExtension('mod_spatialite.so')`。CLI 與 FPM 各自須設定 `sqlite3.extension_dir="/usr/lib/x86_64-linux-gnu"`；這是系統層級設定，不能靠應用程式 `ini_set()` 補上。`sqlite3.defensive` 保持 On。設定限制見 [PHP 官方說明](https://www.php.net/manual/en/sqlite3.configuration.php)。

SQLite3 官方 loader 先解析檔案實體路徑，再檢查是否位於 extension_dir 之內。把 extension_dir 設為 `/var/www/sqlite3_ext` 時，symlink 解析後位於 `/usr/lib/...`，會收到 `Unable to open extensions outside the defined directory`。因此 PDO 使用上面的穩定 alias，SQLite3 的目錄設定使用套件實體目錄，兩者載入同一個已驗證模組；不修改或放寬官方 loader。實作見 [PHP 8.5 SQLite3 原始碼](https://github.com/php/php-src/blob/PHP-8.5/ext/sqlite3/sqlite3.c#L411-L420)。

`tests/sqlite-smoke.php --acceptance` 依兩份使用者範例建立獨立的 memory DB。保留 PDO 的三項 PRAGMA、`InitSpatialMetadata(1)`、`AddGeometryColumn`，POINT 使用原始 SQL，LINESTRING／POLYGON 使用 prepared statement；座標與奇偶交替的 2／5 點線段沿用範例。每種幾何分別在交易中插入 10,000 筆，檢查全部資料的非 NULL 數、型別、SRID 4326、XY／二維及空間欄位 metadata。沒有網站、既有資料庫或道路資料相依。

```sh
php tests/sqlite-smoke.php --acceptance --module=/var/www/sqlite3_ext/mod_spatialite.so
# 需要分開診斷時：
php tests/sqlite-smoke.php --acceptance --module=/var/www/sqlite3_ext/mod_spatialite.so --driver=pdo
php tests/sqlite-smoke.php --acceptance --module=/var/www/sqlite3_ext/mod_spatialite.so --driver=sqlite3
```

每個 driver 成功時為 30,000 筆；預設 `all` 合計 60,000 筆。結果為 JSON，結束碼 0 表示全部 PASS、1 表示 FAIL、2 表示 UNVERIFIED。沒有指定可信本機模組時不載入原生程式碼；不改 PHP 設定、不自行安裝，也不因 PDO 失敗而跳過 SQLite3。此檔案的直接 HTTP 入口拒絕請求。

發行包沿用既有 `tests/sqlite-smoke.php` 路徑，讓已安裝的 `2026.10.26.003` 更新器可直接驗證、更新。沒有 `--acceptance` 時維持原快速檢查。Git 原始碼另保留 `tests/spatialite-acceptance.php` wrapper，呼叫相同實作；它不額外放入發行包。套件頁只負責安裝官方模組，下面的 VM 目錄／ini 設定不會由程式更新自動套用。

SQLite 頁的 `Run PDO + RTree` 不載入原生模組；無模組時顯示 SpatiaLite UNVERIFIED。root 模式另提供明確指定可信模組的 `Test SQL + spatial` 與 `Full 30k / driver`，分別使用既有快速 SQL 路線及上述完整驗收入口。CLI 通過不代表 FPM 通過，兩者必須分開實跑。新主機操作次序與錯誤對照見 [SQLite 驗收流程](sqlite-smoke.md)。

## Ubuntu 26.04 實測

2026-10-26，在 `.26` 的 PHP 8.5.4 NTS／ABI 20250925，使用官方 `libsqlite3-mod-spatialite` 5.1.0-3ubuntu2。套件檔案 `dpkg -V` 無差異；實際載入 SpatiaLite 5.1.0、SQLite 3.46.1。

| 執行環境 | PDO SQL load_extension | SQLite3::loadExtension |
|---|---|---|
| 一般帳號 john CLI | PASS，30,000 筆 | PASS，30,000 筆 |
| www FPM／Unix FastCGI | PASS，30,000 筆 | PASS，30,000 筆 |

四項各使用獨立 memory DB，合計 120,000 筆；每項三個交易、三種幾何各 10,000 筆，全量非 NULL／型別／SRID／XY／空間 metadata、座標樣本及 LINESTRING 點數分布均通過。PDO 類別實際為 `PDO`。FPM 使用 `/run` 中暫存 wrapper 與已存在的 Unix socket，未建立網站路由；沒有以 CLI 推定 FPM，也沒有藉 request 或命令列覆寫模組目錄、gate 或 defensive 來取得 PASS。

修正前，兩環境的 PDO 已通過，SQLite3 因目錄為空而 FAIL；第一輪將目錄設到 symlink 所在處時，SQLite3 也依上述官方限制真實 FAIL。保留這些結果後，只將兩份新 ini 的設定改為 APT 實體目錄，原驗收檔不變，才得到四項 PASS。另以獨立、process-only 官方 PDO 模組驗證原始 SQL 路線，回報 `not authorized`，因此保留既有羽山版 PDO；沒有替換官方 `.so`。

持續保留的 VM 設定：root:root 0755 `/var/www/sqlite3_ext` 與指向 APT 路徑的 `mod_spatialite.so` symlink；CLI／FPM 各一份 root:root 0644 `/etc/php/8.5/{cli,fpm}/conf.d/90-3wa-spatialite.ini`，內容僅 `sqlite3.extension_dir="/usr/lib/x86_64-linux-gnu"`。FPM 通過設定檢查後 graceful reload；PDO gate 與 SQLite3 defensive 均維持 1。boot ID、IP、原 PDO／SQLite3 ini alias、Feather active 狀態與 FPM active／enabled／MainPID 前後一致，未重開 VM。

root 私有備份／receipt 位於 `/var/backups/3wa-spatialite-79d4b108e32f/` 與 `/var/backups/3wa-spatialite-4d8e1a69c203/`；前者記錄初始不存在的檔案，後者保存本次兩 ini 的舊內容。開發證據位於 ignored `.root-dev/spatialite-acceptance-b1036e5a8d72/`，最終報告為 `probe-after-42808e2bdd4b.json`；原失敗報告保留。此驗收不代表其他 GIS 函式、Ubuntu 22.04／24.04 或 PHP ABI 已通過。

## 2026-10-27 發行入口相容驗收

發版前將相同 workload 函式原樣搬入既有 `tests/sqlite-smoke.php`，以 `--acceptance` 明確選擇，未改原快速檢查流程。獨立 wrapper 留在 Git 原始碼；發行包只需原已允許的 PHP 路徑。測試說明改放 `docs/sqlite-smoke.md`，同樣符合舊更新器允許的文件路徑，沒有放寬 updater 的封包邊界。

新入口已再次於 `.26` 真正執行：john CLI 及 Unix FastCGI 的 PDO／SQLite3 四項全 PASS、合計 120,000 筆。CLI 執行的正是發行入口 `sqlite-smoke.php --acceptance`，FPM 使用相同函式；原主機 ini、symlink、官方模組及 PDO gate 保留。證據：ignored `.root-dev/spatialite-acceptance-91e7c3bd4a60/probe-after-8dddc97c22d4.json`。

另用舊 `v2026.10.26.002`、`v2026.10.26.003` 的原始 manifest 驗證器，直接接受本地候選包；抽出單一 PHP 檔後，help 及缺模組 UNVERIFIED 入口亦可獨立執行。此項是未簽章本地候選的相容性檢查，公開 Release 的簽章及下載驗證另以發行紀錄為準。

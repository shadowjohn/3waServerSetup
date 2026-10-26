# PDO SQLite / RTree / SpatiaLite CLI smoke test

不依賴網站、routing_config.php、道路資料或現有DB。各項使用獨立 sqlite::memory: 連線；不改PRAGMA、PHP設定或系統。RTree即使SpatiaLite失敗也會驗證。

```sh
php tests/sqlite-smoke.php --help
php tests/sqlite-smoke.php --module=/trusted/absolute/path/mod_spatialite.so
# 僅測試已完成核准補丁的舊PDO，不會幫忙開啟載入功能：
php tests/sqlite-smoke.php --module=/trusted/absolute/path/mod_spatialite.so --loader=legacy-sql
```

auto/native使用PHP 8.4+ Pdo\Sqlite::loadExtension；方法不可用時報UNVERIFIED，不自動退到SQL載入。模組選擇參數置於腳本名稱之後。只載入你確認可信的本機原生模組，CLI操作者提供路徑視為本次測試授權；腳本不提供路徑沙箱。不可部署為HTTP服務，非CLI請求直接403且不處理路徑。CLI錯誤可能含本機路徑，請勿公開貼出未檢查的輸出。

PASS為實際SQL斷言通過；UNVERIFIED表示缺driver／未指定模組／缺官方API等無法驗證；指定路線後載入錯誤、錯路徑或SQL斷言不符為FAIL。結束碼0全部通過、1有失敗、2尚有無法驗證（且沒有失敗）。RTree測實際虛擬表、插入、兩筆重疊結果與不重疊反例；SpatiaLite測版本及平面多邊形面積12，不代表全部GIS功能正確。

初版本機Windows檢查結果另於交付回報。後續 Ubuntu 26.04 CLI SQLite／RTree，以及羽山版真正 Linux .so／Unix FastCGI 驗證見 [root 驗收](ubuntu26-root-validation.md)；Ubuntu 22.04／24.04 仍未驗收。此腳本只測CLI；不能用CLI通過推定FPM通過，也不會安裝、編譯或重啟任何服務。

2026-10-24本機隔離結果：PHP 8.3.36／SQLite 3.54.1，明確legacy-sql載入既有可信MS4W模組，SpatiaLite 5.1.0；PDO、RTree、SpatiaLite三項PASS且exit 0。無module exit 2；無ini（無driver）exit 2；不存在module exit 1；舊PHP用auto且提供有效module為UNVERIFIED、exit 2。所有分支均未更動設定。PHP8.4 native API路線仍未實跑。

使用者「php check rtree」參考附件已透過Library唯讀文字介面完整檢視，未執行附件且未完成本機materialization。現有腳本包含PDO driver探測、記憶體DB與實際RTree建表，並額外驗證插入／overlap正反例；不改弱成只看ENABLE_RTREE。compile option可作診斷資訊，不能當主要通過條件。附件較早的一行echo／三元式有優先序歧義，後面的加括號版本才適合參考；不直接複製或執行原片段。

已獲授權補齊SQLite3 RTree：獨立memory連線執行建表／insert／overlap正反例，結果與PDO分列；任一缺失不阻止其他測試。最新本機結果SQLite3 RTree／PDO SQLite／PDO RTree／SpatiaLite四項PASS，legacy exit0；-n四項UNVERIFIED exit2。

2026-10-25 Ubuntu 26.04 的[羽山版編譯與啟用流程](php-feather.md)另以自有 nonce SQLite `.so`，實際驗證既有 PDO 的 SQL load_extension()：CLI／FPM 成功，gate Off／官方模組拒絕，官方還原及失敗補償成功。自有測試 extension 不提供 SpatiaLite 函式，不能拿它代入本腳本當作 SpatiaLite 驗收；當時未完成 SpatiaLite 實際載入測試。

2026-10-26 新增 `sqlite-smoke.php --acceptance`，依使用者提供的原始兩種寫法：plain PDO + SQL load_extension()、SQLite3::loadExtension()，各建立三種幾何、各 10,000 筆。Ubuntu 26.04 的一般帳號 CLI 及真正 Unix FastCGI 四項均 PASS，合計 120,000 筆；使用官方 APT SpatiaLite、保留羽山版 PDO。設定方式、symlink 的 SQLite3 限制與完整驗收範圍見 [PHP SpatiaLite 驗收](spatialite-acceptance.md)。此結果不改變上方快速 smoke 的 auto/native 路線邊界。

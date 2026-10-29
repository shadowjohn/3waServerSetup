# PDO SQLite / RTree / SpatiaLite CLI smoke test

## 新主機最短驗收流程

在程式 `Install packages` 勾選 PHP SQLite 與 `libsqlite3-mod-spatialite`；再於羽山版 PDO SQLite 區選 CLI、FPM，檢閱編譯／啟用。套件裝好、編譯成功與應用 SQL 可用是三個不同的檢查，下面不以套件已安裝當作成功。

1. 到 SQLite 頁按 `Run PDO + RTree`。PDO 讀寫、PDO RTree、SQLite3 RTree 三項都要 PASS；未選模組的 SpatiaLite UNVERIFIED 是預期結果。
2. root 模式填可信本機 `mod_spatialite.so` 絕對路徑（預設 `/var/www/sqlite3_ext/mod_spatialite.so`，也可用官方套件實體路徑），按 `Test SQL + spatial`。此路線使用 SQL `load_extension()`，測空間函式與面積正反例，不會為了通過而臨時開啟 gate。
3. 按 `Full 30k / driver`，實際執行兩份使用者範例的 POINT、LINESTRING、POLYGON，各 driver 各 30,000 筆。兩個 driver 都 PASS 才算完整 CLI 驗收。

同樣可直接執行：

```sh
cd /opt/3waServerSetting/current
php --ini
php tests/sqlite-smoke.php
php tests/sqlite-smoke.php --module=/var/www/sqlite3_ext/mod_spatialite.so --loader=legacy-sql
php tests/sqlite-smoke.php --acceptance --module=/var/www/sqlite3_ext/mod_spatialite.so
```

用 `dpkg -L libsqlite3-mod-spatialite` 確認實體檔位置；若沒有建立 `/var/www/sqlite3_ext` alias，直接代入查到的 `.so`。**CLI 與 FPM 各自需要設定**：PDO SQL 路線須已啟用羽山版 `pdo_sqlite.feather_sql_load_extension=1`；SQLite3 的 `sqlite3.extension_dir` 須指向官方套件實體目錄，例如 amd64 的 `/usr/lib/x86_64-linux-gnu`，symlink 目錄不能取代實體目錄。詳細限制見 [SpatiaLite 設定與雙驅動驗收](spatialite-acceptance.md)。程式更新不會自動建立 alias 或修改此設定。

| 結果 | 判讀與下一步 |
|---|---|
| PDO／RTree PASS | 真正建表、寫入與查詢成功；尚未證明 SpatiaLite 或 FPM |
| PDO `not authorized` | SQL 載入路線未啟用；檢查此 SAPI 是否實際載入羽山模組與 gate |
| SQLite3 `outside the defined directory` | 檢查 `sqlite3.extension_dir` 與 symlink 解析後的實體目錄 |
| 缺 `.so` 或 driver | 先補官方套件／PHP driver，再重跑，不能把 UNVERIFIED 當 PASS |
| CLI PASS，網站仍失敗 | 到 PHP 頁套用並檢查獨立 FPM 驗證；Apache 頁另檢閱 event + FPM handler，網站路由另測 |

完整測試使用記憶體 DB，會執行原生程式碼與資料插入，需要時間；不寫入應用資料庫。只選擇可信的本機模組，root UI 另檢查實體檔、alias 與父目錄的擁有者／寫入權限。

### 獨立 FPM 驗收

上述按鈕及 `php` 命令只驗 CLI。FPM 設定套用成功後，可以用 root 在 Unix socket 上執行相同完整 workload，無須建立公開網站測試頁。以下使用發行包既有 Python 與 FastCGI helper；只建立暫存 PHP wrapper，完成或失敗均清理，不覆寫任何 PHP 設定。

```sh
cd /opt/3waServerSetting/current
/opt/3waServerSetting/current/.venv/bin/python - <<'PY'
import json, os, socket, tempfile
from pathlib import Path
from server_setting.root_actions import RootManager
from server_setting.root_recipes import _fpm, _trusted_fpm_path
from server_setting.root_fpm import _exchange
from server_setting.diagnostics import _trusted_spatialite_module

core = RootManager()
core._gate('php')
target = _fpm(core.host)
module = _trusted_spatialite_module('/var/www/sqlite3_ext/mod_spatialite.so')
library = (Path.cwd() / 'tests/sqlite-smoke.php').resolve(strict=True)
_trusted_fpm_path(core.host, library)
source = ("<?php\ndefine('THREEWA_SQLITE_LIBRARY_ONLY', true);\nrequire "
          + json.dumps(str(library)) + ";\n$module=" + json.dumps(module) + ";\n"
          + "header('Content-Type: application/json');\n"
          + "echo json_encode(['sapi'=>PHP_SAPI,'values'=>['results'=>["
          + "spatialite_acceptance('pdo',$module),spatialite_acceptance('sqlite3',$module)]]]);\n")
directory = Path(tempfile.mkdtemp(prefix='3wa-spatialite-fpm-', dir='/run'))
script = directory / 'probe.php'
try:
    os.chmod(directory, 0o711)
    script.write_text(source, encoding='utf-8')
    os.chmod(script, 0o644)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(300)
        connection.connect(target['socket'])
        report = _exchange(connection, str(script))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    passed = (report.get('sapi') == 'fpm-fcgi'
              and len(report['values'].get('results', [])) == 2
              and all(row.get('status') == 'PASS' for row in report['values']['results']))
    raise SystemExit(0 if passed else 1)
finally:
    script.unlink(missing_ok=True)
    directory.rmdir()
PY
```

預期 `sapi=fpm-fcgi`，兩個 `results` 分別為 PDO、SQLite3，皆 PASS、各 30,000 筆；任一缺 driver、gate、模組或設定有誤即失敗。wrapper 不使用 `ini_set()` 或 FastCGI `PHP_VALUE` 偷補目標值。這項仍不等同 Apache/vhost/網站應用的 HTTP 驗收。

## 快速檢查參數與限制

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

# APT 套件勾選安裝

Root 的 `Install packages` 頁獨立於 `System updates`。進入後顯示目前安裝版本與 APT candidate；尚未安裝的套件可以勾選，一次預覽及安裝。已安裝項目僅顯示，不提供移除操作。重新整理不會執行 apt update。

| 分組 | 內容 |
|---|---|
| GIS 工具 | PROJ、GDAL、GEOS、SpatiaLite CLI、`libsqlite3-mod-spatialite` 原生載入模組 |
| MapServer | `cgi-mapserver`、`mapserver-bin`，包含 mapserv |
| Python GIS | GDAL、pyproj、Shapely、GeoPandas 的 Ubuntu 系統 Python 套件 |
| GIS 編譯套件 | PROJ、GDAL、GEOS、SpatiaLite 開發標頭 |
| 基礎工具 | ntpdate、vim、make、screen、wget、ipset、iptables、curl、net-tools、tree、git、rsync、zip、unzip、htop、ncdu、lsof、bind9-dnsutils、tcpdump、jq、ca-certificates、bash-completion |
| PHP／Apache | GD、mbstring、EXIF、JSON、PDO、MySQL、SQLite、zip、xml、XSendfile |

`ntpdate` 對應 Ubuntu 26.04 的 `ntpsec-ntpdate`，安裝不會自行校時。PHP JSON 是 PHP 8 內建功能；EXIF／PDO 查核實際版本的 `php8.5-common` provider。官方 PDO SQLite 由 `php8.5-sqlite3` 提供；[支援 SQL load_extension() 的羽山版](php-feather.md) 使用下方獨立編譯／啟用區，勾選官方套件不等同編譯羽山版。

`libsqlite3-mod-spatialite` 提供 PHP PDO／SQLite3 可載入的官方 `mod_spatialite.so`，不以 `spatialite-bin` 或開發標頭代替。套件頁不修改 PHP 的 extension_dir；兩份使用者範例的設定與完整 30,000 筆驗收見 [PHP SpatiaLite](spatialite-acceptance.md)。

安裝先模擬相依套件、下載量及磁碟量，再檢閱確認。安裝使用 `apt-get --no-upgrade --no-remove`，保留修改過的 conffile；若相依關係需要升級既有套件，須先到 `System updates` 處理。來源與套件狀態變動會使舊預覽失效。可以搜尋額外套件名稱，不能輸入 URL、deb 檔、版本指定或 apt 命令選項。

```sh
/opt/3waServerSetting/run.sh --packages-catalog
/opt/3waServerSetting/run.sh --packages-search 'gdal'
/opt/3waServerSetting/run.sh --packages-plan screen wget
/opt/3waServerSetting/run.sh --packages-install screen wget --confirm
```

安裝成功只證明 dpkg 套件及版本符合預覽；PHP 的 CLI／FPM 載入狀態、MapServer vhost／CGI／mapfile、XSendfile 開放目錄仍需另外設定驗證。套件安裝不受設定還原解除，也不會自動開放防火牆。

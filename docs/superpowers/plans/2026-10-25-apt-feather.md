# APT 勾選安裝與羽山版 PDO SQLite

使用者要求獨立 APT 安裝區：入頁辨識已裝／未裝、勾選批次安裝，不提供移除；包含 GIS、PHP／Apache XSendfile、基礎工具。使用者另明確確認羽山版必須支援 SQL `load_extension()`，保留原應用程式 PDO 使用方式。

沿用 RootManager 的 actor gate、鎖、fingerprint、私有交易及結果記錄。一般 APT 與自訂 C 模組編譯為獨立操作；不把官方 php-sqlite3 安裝成功當成自訂版完成。

1. PackageManager：固定分類與 provider、APT 搜尋、dpkg／candidate 狀態；只安裝選取項目與相依，不升級、不移除。預覽與實際命令一致，索引或套件狀態改變拒絕舊預覽。
2. FeatherBuilder：精確 Ubuntu PHP source／dev／ABI 配對、私有 signed deb-src cache、SHA256 檢查、無登入 builder 與 bounded systemd cgroup。僅編譯 ext/pdo_sqlite，不執行 make install，不覆寫官方模組。
3. 羽山版 root `PHP_INI_SYSTEM` gate 預設 Off；選定 CLI／FPM 啟用時 On。原應用程式無新增 PDO option。產物包含可重現身分與自有 native SQL probe。
4. FeatherSQLiteManager：CLI／FPM 分別選擇，未選保留；原生 ini alias 專用 adapter、FPM test／reload、真正 SQL load_extension() 驗收。失敗恢復前一個工作版本；主動 Restore stock 恢復第一次官方 baseline。中斷交易必須可辨識並先恢復。
5. UI／CLI：APT 勾選頁與獨立羽山版區，所有寫入先檢閱。root TTY 140／80 欄、Ctrl+左右、取消不寫入。

驗證以 focused tests、Ubuntu 完整 suite 及 `.26` 實際 source fetch／編譯／CLI／Unix FastCGI／還原分開記錄；GIS 大套件先原生模擬，不擅自整批安裝。保留當下使用者已調整的 root LV 約 36.94 GiB、16 GiB /swap.img、靜態 IP 與 SSH。未授權 commit／push／release，不執行 VM reboot。

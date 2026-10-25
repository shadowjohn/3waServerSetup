# 3waServerSetup

3waServerSetting Linux終端架站／維運工具。Ubuntu 26.04 的 root 模式提供 Apache、PHP-FPM、vsftpd、Samba、MariaDB 與時區的安裝、推薦設定、自訂配方儲存／套用及最近設定還原，另有 Startup 開機腳本、APT 更新／升級、套件勾選安裝、羽山版 PDO SQLite、雙棧防火牆、BanIP、Swap 與根 LV 擴容；操作方式見[Root 管理](docs/root-management.md)。一般帳號維持[唯讀Textual介面](docs/tui-readonly.md)。

可先執行 `python -m server_setting --text` 或 `--status-json`；依賴已存在時執行 `python -m server_setting`／`sh run.sh`。啟動只觀察本機；root 修改須從服務頁檢閱確認，或使用明確的 `--root-* ... --confirm`。launcher 不自動安裝依賴或連線其他主機。

MariaDB 新增獨立的 [root 密碼與外連 On／Off](docs/mariadb-root-access.md)，依使用者選擇支援 root@%。右側圖像改為[使用者提供的 3wa 原圖](docs/previews/3wa-logo-source.png)，以黑白 Braille 點陣呈現，保留既有 20×20 終端尺寸與窄畫面退化。

新主機使用 GitHub Release 的安裝器；開發才 clone。安裝、明確檢查更新與程式回退見[安裝與更新操作](docs/installation.md)。版號採 `YYYY.MM.DD.NNN`，例如 `2026.10.25.001`；發布包附固定公鑰驗證的簽章 manifest、完整檔案 hash 與鎖定 wheels。啟動不自動連網更新，主機設定／備份／編譯模組留在版本目錄外。

- [總設計與8bit看板娘提案](docs/design.md)
- [防火牆來源與 SSH 回復保護](docs/firewall-root.md)、[Swap 容量／啟停／fstab](docs/swap-root.md)、[根 LV 擴容](docs/root-lvm.md)
- [APT 套件勾選安裝](docs/apt-install.md)、[羽山版 PDO SQLite 編譯／啟用／還原](docs/php-feather.md)、[手動更新 BanIP](docs/banip-root.md)
- [Startup 開機腳本：設定／儲存／套用／立即執行／還原](docs/startup-root.md)
- [尚未完成的功能與驗收](docs/remaining-work.md)
- [Release 安裝與更新規格](docs/release-distribution.md)
- [離線防火牆候選](docs/firewall-offline.md)：只支援IPv4 INPUT，無apply；不可直接導入iptables-restore。
- [PDO SQLite／SQLite3 RTree／SpatiaLite CLI測試](tests/README.md)
- [Apache敏感檔案候選](docs/apache-sensitive-files.md)：合成匹配測試已跑，Apache runtime尚未驗證。
- [Apache banner／MapServer選配](docs/apache-optional-security.md)、[MariaDB操作設計](docs/mariadb-operations.md)、[版本更新設計](docs/version-update.md)

舊 schema 1 離線防火牆範本預設服務全部停用，來源使用文件示例網段。新的 root 功能使用 schema 2 與 nftables inet INPUT，同時限制 IPv4／IPv6，每個服務各自指定來源；儲存草稿不啟用，套用後須經新 SSH 連線確認。不得提交實際 config、備份與秘密；VPN／PPP／NAT／FORWARD／容器保護不在受管範圍。

```sh
python3 firewall_plan.py --config config/firewall.example.json --validate
python3 -m unittest discover -s tests
php -l tests/sqlite-smoke.php
```

Root 與 VM 的驗收範圍見 [Ubuntu 26.04 root 驗收](docs/ubuntu26-root-validation.md)。先前本機 PHP8.3.36、SQLite3.54.1 及 SpatiaLite5.1.0 的 legacy-sql 四項測試曾通過；本輪羽山版使用自有 SQLite native probe 驗證，SpatiaLite 套件與實際應用另行驗收。Ubuntu22.04／24.04 寫入、PHP8.4 native 路線、自更新仍未完成。Linux部署根為 `/opt/3waServerSetting`；`nat.sh` 仍僅離線子工具，不會寫入 `/root/nat.sh`。

# 3waServerSetup

3waServerSetting Linux終端架站／維運工具的設計與已完成離線元件。**完整Textual TUI、安裝器、自更新及主機套用功能尚未實作。**

- [總設計與8bit看板娘提案](docs/design.md)
- [離線防火牆候選](docs/firewall-offline.md)：只支援IPv4 INPUT，無apply；不可直接導入iptables-restore。
- [PDO SQLite／SQLite3 RTree／SpatiaLite CLI測試](tests/README.md)
- [Apache敏感檔案候選](docs/apache-sensitive-files.md)：合成匹配測試已跑，Apache runtime尚未驗證。
- [Apache banner／MapServer選配](docs/apache-optional-security.md)、[MariaDB操作設計](docs/mariadb-operations.md)、[版本更新設計](docs/version-update.md)

防火牆範本預設服務全部停用；來源使用文件示例網段。不得直接部署原主機規則或提交實際config、備份與秘密。第一版不管理VPN／PPP／NAT，未涵蓋IPv6、FORWARD或容器保護。

```sh
python3 firewall_plan.py --config config/firewall.example.json --validate
python3 -m unittest discover -s tests
php -l tests/sqlite-smoke.php
```

目前驗證：Windows離線12個Python測試通過；本機PHP8.3.36、SQLite3.54.1及SpatiaLite5.1.0，明確legacy-sql路線四項PASS。Ubuntu22.04／24.04／26.04、FPM、PHP8.4 native路線、Apache configtest／HTTP與Linux防火牆掛接尚未驗證。Linux安裝根規劃為/opt/3waServerSetting，run.sh主入口尚未建立；nat.sh目前僅為離線子工具。

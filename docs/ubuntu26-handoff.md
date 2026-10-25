# Ubuntu 26.04唯讀驗收交接

這是早期唯讀測試包的交接記錄。後續 root 開發與 VM 操作已另獲使用者授權；新版本請看 [Root 管理](root-management.md)，不使用這兩份 john 測試目錄執行 root 程式。

此包為本地未發布開發快照，base commit b37d776，不是新tag／main版本。Windows測試不代表Ubuntu通過。本任務SSH解析到受禁止的deny wrapper，未使用其他路徑、協定或library連線；請由已被允許的新專案接手傳檔與驗證，不重新設定登入、IP或VM。

## 最短操作（已允許的新專案／終端）

先在一般使用者獨立目錄接收壓縮包與同名.sha256；依外部提供的SHA256核對後再解壓。不要放進/opt、既有網站或現有Git checkout，不用root，不動乾淨備份。

```sh
sha256sum -c 3waServerSetting-readonly-ubuntu26.tar.gz.sha256
mkdir -p "$HOME/3wa-readonly-test"
tar -xzf 3waServerSetting-readonly-ubuntu26.tar.gz -C "$HOME/3wa-readonly-test"
cd "$HOME/3wa-readonly-test/3waServerSetting-readonly"
sha256sum -c SHA256SUMS
sh tests/ubuntu-readonly-check.sh
.venv/bin/python -m server_setting
```

若該目錄已存在，選另一個新名稱，不覆蓋既有測試。`SHA256SUMS`只列內容完整性；包本身未簽章，外部checksum應經可信交接核對，不能單靠包內manifest確認發布者。

驗收腳本先拒絕root、確認Ubuntu26.04，僅在包內.venv安裝官方PyPI hash鎖定wheel並跑唯讀tests／Textual smoke／status-json；沒有sudo、apt、sshd、網路／防火牆設定或主機Apply。若python3、venv、pip、來源連線或hash驗證缺失／失敗，停止回報，不自行裝系統套件、不換鏡像或調安全設定。依賴hash曾在Windows查證，Linux仍須獨立安裝／pip check與UI驗證。

## 要回報的實際證據

- 一般帳號身分與Ubuntu版本符合；script遇任何權限／服務缺失要保留UNKNOWN／UNAVAILABLE，不升root繞過。
- validation-results/tests.txt與ui-smoke.txt的完整結果。測試含Linux合成probe與實際本機唯讀probe，不能把合成案例算成主機健康。
- status.json中服務觀察、OS release、時區／同步旗標、活動swap數及根FS；單FPM的php.ini選定欄位只是配置證據，conf.d／pool／有效FPM值仍未驗證。沒有資料庫登入或秘密檔讀取。
- 實際終端Tab／Shift+Tab、Enter預覽、Ctrl+左右切頁、Ctrl+R刷新、Ctrl+Q退出；寬畫面角色顯示、縮窄收起。Windows已有對應pilot，不代替這裡的Linux終端結果。
- Firewall只加入記憶體草稿／產生候選，不導入iptables-restore；Apache僅顯示候選，不寫/etc或reload。SQLite UI只跑無native模組memory smoke，SpatiaLite未指定模組應為UNVERIFIED，不載入外部DLL／.so以追求全PASS。

validation-results可能含本機配置／路徑，留本機、回報前去識別，不進Git或新測試包。這輪沒有root系統套用、安裝系統套件、網路／firewall／sshd／帳號變更、VM／備份操作或push授權。

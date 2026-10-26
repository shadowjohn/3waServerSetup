# 安裝、更新與程式回退

使用主機下載 GitHub Release 即可，不需要 Git、GitHub CLI 或 GitHub 登入。開發／除錯才使用 clone。正式版號採 `YYYY.MM.DD.NNN`，例如 `2026.10.25.001`；同日下一版為 `.002`，tag 加 `v` 前綴。

## 首次安裝

以下以 root 在 `/root` 執行。若主機尚無 curl／CA 憑證，先執行 `apt-get update`、`apt-get install curl ca-certificates`。

```sh
cd /root
curl --fail --location --proto '=https' --proto-redir '=https' --tlsv1.2 \
  https://github.com/shadowjohn/3waServerSetup/releases/download/v2026.10.27.001/install.sh \
  --output install-3wa.sh
# 先檢閱安裝器，再執行：
sh ./install-3wa.sh --release v2026.10.27.001
/opt/3waServerSetting/run.sh
```

新主機直接使用 `v2026.10.27.001` 的 `install.sh`，支援 Ubuntu 22.04／24.04／26.04。指定其他版本時，下載該 tag 附件中的 `install.sh`，並將 `--release` 改為相同 tag。安裝器需要時會準備 Python 3、venv、OpenSSH client、curl 及 CA 憑證；業務服務的安裝仍由工具頁面檢閱操作。

安裝器包含固定發布公鑰，先驗 manifest 的 SSH 簽章、安裝器自身及 archive／逐檔 hash，才執行包內程式。Python 依賴從隨包 wheelhouse 依鎖檔離線安裝，不使用系統 pip。首次執行安裝器仍需操作者確認下載來源；之後不會靜默更換信任公鑰。

Installer 接受 Ubuntu 22.04／24.04／26.04，使用發行版內建 Python 3.10 以上及獨立 venv；不替換系統 Python。已驗證範圍以 Release 的 `validated_platforms` 及[實測紀錄](release-validation.md)為準。安裝／更新能力和服務管理分開，服務寫入目前仍只開放已驗證的 Ubuntu 26.04。

## 取得更新

使用 `2026.10.25.001`／`2026.10.26.001` 者，先完成下節的指定版本橋接。完成橋接或已從 `2026.10.26.003` 安裝後，可進入 **Software update**：Check releases 顯示 latest 版本、commit 與變更；Review & update 經確認後才下載驗證、建立獨立 venv 並切換。成功後退出舊畫面，重新執行 `run.sh`。

```sh
/opt/3waServerSetting/run.sh --version
/opt/3waServerSetting/run.sh --update-status
/opt/3waServerSetting/run.sh --update-check
/opt/3waServerSetting/run.sh --update-plan --release-tag v2026.10.27.001
/opt/3waServerSetting/run.sh --update-apply --release-tag v2026.10.27.001 --confirm
```

一般啟動及 status 不連網。無網路、簽章／hash 不符、資料 schema 不相容或仍有主機設定交易時，不切換程式。進行中的舊版 console 不得跨版繼續修改主機，需重新開啟。

`2026.10.26.003` 可直接更新到本版，不需增加橋接。新增的 [SpatiaLite 完整驗收](spatialite-acceptance.md)沿用原有 `tests/sqlite-smoke.php --acceptance` 入口；安裝／更新程式不會自行安裝 SpatiaLite、建立模組 symlink 或修改 PHP ini，主機配置與程式版本分開操作。GDAL 的 3827／3828 這版提供比較紀錄，尚未套用新參數。

### 舊版更新器的相容性

`2026.10.25.001`、`2026.10.26.001` 的更新器只認得 Python `>=3.12` 與 Ubuntu 24／26 的 manifest。直接選取 latest `v2026.10.27.001` 會被拒絕，程式與主機設定保持原狀；更新器不會自動選擇橋接包。

舊版先以 root 指定橋接 `v2026.10.26.002`。該包來源為 `6601ef4cec1f4b3d7ef6050496c6528a3f094da2`，manifest 仍保留 Python `>=3.12` 與 Ubuntu 24／26，更新器已能辨識下一版的 metadata：

```sh
/opt/3waServerSetting/run.sh --update-plan --release-tag v2026.10.26.002
/opt/3waServerSetting/run.sh --update-apply --release-tag v2026.10.26.002 --confirm
```

關閉舊版 console，重新啟動程式；用 `--version` 確認為 `2026.10.26.002` 後，再指定正式版 `v2026.10.27.001`：

```sh
/opt/3waServerSetting/run.sh --version
/opt/3waServerSetting/run.sh --update-plan --release-tag v2026.10.27.001
/opt/3waServerSetting/run.sh --update-apply --release-tag v2026.10.27.001 --confirm
```

完成橋接後也可使用 Software update 的 latest 流程。各版本實測與公開發布驗證以 [Release 驗收](release-validation.md)及 [Ubuntu 22.04 驗收](ubuntu22-validation.md)為準。

新版仍能辨識舊 manifest，但 Python 3.10 主機會拒絕安裝或回退到宣告 `>=3.12` 的包。一般 status 可以唯讀顯示該版本，不會因此執行它。

## 回退與中斷

Software update 的 **Rollback / recover** 或下列命令，只切回上一個完整、相容的本機程式與 venv：

```sh
/opt/3waServerSetting/run.sh --update-rollback --confirm
```

回退不恢復已套用的服務設定、不移除 APT 套件，也不回復 DB、磁碟或 Swap。首次安裝／移轉若中斷且 launcher 尚未就緒，可重跑同一份已驗證安裝器；它會辨識自己的未完成交易，先恢復再繼續，不要求舊版具備新 CLI。

程式版本位於 `/opt/3waServerSetting/releases/`，`current` 指向正在使用的版本。config、backups、modules 與 `/var/lib/3wa-server-setup` 留在版本外。既有 flat 安裝先保留可啟動的 legacy 程式快照；若發現未知 package 資料、外部 symlink 或 dirty Git checkout，會停止而不捨棄修改。

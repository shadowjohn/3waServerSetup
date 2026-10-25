# 安裝、更新與程式回退

使用主機下載 GitHub Release 即可，不需要 Git、GitHub CLI 或 GitHub 登入。開發／除錯才使用 clone。正式版號採 `YYYY.MM.DD.NNN`，例如 `2026.10.25.001`；同日下一版為 `.002`，tag 加 `v` 前綴。

## 首次安裝

以下以 root 在 `/root` 執行。若主機尚無 curl／CA 憑證，先執行 `apt-get update`、`apt-get install curl ca-certificates`。

```sh
cd /root
curl --fail --location --proto '=https' --proto-redir '=https' --tlsv1.2 \
  https://github.com/shadowjohn/3waServerSetup/releases/latest/download/install.sh \
  --output install-3wa.sh
# 先檢閱安裝器，再執行：
sh ./install-3wa.sh
/opt/3waServerSetting/run.sh
```

指定版本時，下載該 tag 附件中的 `install.sh`，再執行 `sh ./install-3wa.sh --release v2026.10.25.001`。安裝器需要時會準備 Python 3、venv、OpenSSH client、curl 及 CA 憑證；業務服務的安裝仍由工具頁面檢閱操作。

安裝器包含固定發布公鑰，先驗 manifest 的 SSH 簽章、安裝器自身及 archive／逐檔 hash，才執行包內程式。Python 依賴從隨包 wheelhouse 依鎖檔離線安裝，不使用系統 pip。首次執行安裝器仍需操作者確認下載來源；之後不會靜默更換信任公鑰。

Installer 接受 Ubuntu 24.04／26.04；已驗證範圍以 Release 的 `validated_platforms` 及[實測紀錄](ubuntu26-root-validation.md)為準。安裝／更新能力和服務管理分開，服務寫入目前仍只開放已驗證的 Ubuntu 26.04。

## 取得更新

重新開啟工具後進入 **Software update**：Check releases 顯示版本、commit 與變更；Review & update 經確認後才下載驗證、建立獨立 venv 並切換。成功後退出舊畫面，重新執行 `run.sh`。

```sh
/opt/3waServerSetting/run.sh --version
/opt/3waServerSetting/run.sh --update-status
/opt/3waServerSetting/run.sh --update-check
/opt/3waServerSetting/run.sh --update-plan --release-tag v2026.10.25.001
/opt/3waServerSetting/run.sh --update-apply --release-tag v2026.10.25.001 --confirm
```

一般啟動及 status 不連網。無網路、簽章／hash 不符、資料 schema 不相容或仍有主機設定交易時，不切換程式。進行中的舊版 console 不得跨版繼續修改主機，需重新開啟。

## 回退與中斷

Software update 的 **Rollback / recover** 或下列命令，只切回上一個完整、相容的本機程式與 venv：

```sh
/opt/3waServerSetting/run.sh --update-rollback --confirm
```

回退不恢復已套用的服務設定、不移除 APT 套件，也不回復 DB、磁碟或 Swap。首次安裝／移轉若中斷且 launcher 尚未就緒，可重跑同一份已驗證安裝器；它會辨識自己的未完成交易，先恢復再繼續，不要求舊版具備新 CLI。

程式版本位於 `/opt/3waServerSetting/releases/`，`current` 指向正在使用的版本。config、backups、modules 與 `/var/lib/3wa-server-setup` 留在版本外。既有 flat 安裝先保留可啟動的 legacy 程式快照；若發現未知 package 資料、外部 symlink 或 dirty Git checkout，會停止而不捨棄修改。

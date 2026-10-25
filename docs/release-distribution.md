# Release 安裝與更新規格

2026-10-25 使用者核准 CI、Release、新機安裝、明確檢查更新／升級與程式回退，先在 Ubuntu 26.04 驗證；24.04 VM 由使用者準備，完成實測後才增加對應驗證標記。開發採 clone，使用主機不需 Git 或 GitHub 登入。

## 契約

- 固定 repo `shadowjohn/3waServerSetup`，版號依使用者指定為 `YYYY.MM.DD.NNN`，正式 tag 加 `v` 前綴；日期必須有效，NNN 為三位 `001`..`999`，同日遞增。可有 `-rc.N`；不從啟動路徑自動連網。
- Actions 在 main／PR 執行測試；v tag 的測試及建置通過後發布 Release。第一個正式版本為 `v2026.10.25.001`；更新依日期、流水號與 rc 階段比較，不用字串排序。
- 專用 Ed25519 發布 key 只在本機 keys 與 Actions secret；repo／主機只放 `release/allowed_signers`。OpenSSH identity `3waServerSetup-release`，namespace `3wa-release@3wa.tw`。manifest 簽章提供來源驗證；SHA256 提供內容完整性，不互相替代。首次 bootstrap 由使用者信任下載的安裝器，之後公鑰固定，不靜默換 key。
- 資產：`3waServerSetting-v<VERSION>.tar.gz`、`release-manifest.json`、`release-manifest.json.sig`、`SHA256SUMS`、`install.sh`。安裝器含固定公鑰，先驗簽、再驗 archive／逐檔 hash，才執行其中程式。
- external manifest schema 1：`product=3waServerSetting`、`repository`、`version`（不含 v）、`tag`、完整 `commit`、`data_schema=1`、`python_requires=>=3.12`、`validated_platforms`、`archive={name,sha256,size}`、`installer={name,sha256,size}`、`files={relative_path:{sha256,size,mode}}`。archive 不含 external manifest／signature，payload 頂層固定 `3waServerSetting/`，內含 `RELEASE.json`（上述版本、commit、platform、data_schema 身分）。
- payload 白名單：程式、launcher、鎖檔、config examples、必要 PHP smoke、文件／license、`release/allowed_signers`、wheelhouse。禁止主機設定實例、backups、modules、`.root-dev`、Git、交易、key、venv。
- 只使用鎖定且 hash 驗證的 wheel，內附 wheelhouse；版本 venv 以 `--no-index --find-links ... --require-hashes --only-binary=:all: --no-deps` 建立。
- 安裝／更新平台先接受 Ubuntu 24.04／26.04，驗證標記依實測；既有服務寫入 gate 仍只有 Ubuntu 26.04，不因 installer 支援 24.04 而放寬。

## 目錄與操作

- `/opt/3waServerSetting/run.sh` 為穩定入口，`current` 指向 `releases/<version>-<commit12>`。`server_setting` 相容入口讓既有固定 Feather worker path 可用，並以目前版本解析，不覆寫服務／資料。
- 每版本擁有獨立 `.venv`。config／backups／modules 及 `/var/lib/3wa-server-setup` 留在版本外，不能被 installer 或 rollback 刪除、遷移或恢復。
- 現有 flat VM 安裝要保留可啟動的 legacy 程式快照，建立前版記錄，再轉版；dirty Git checkout 拒絕自動轉移，不 stash／reset／clean。首次移轉遇到未知檔案或 symlink 衝突即停止。
- Check：明確讀 GitHub 最新 stable 或指定 tag，驗證 manifest，顯示 notes／版本／commit／下載量。Status 只讀本機。
- Apply：先 stage 到工具目錄、檢查 archive 路徑／types／大小／hash／簽章與相容性，建立 venv，以非特權唯讀 smoke 驗證；取得共用 root transaction lock、檢查 pending，再原子切 current。失敗保留舊 current；中斷要可辨識及恢復，不任意重跑服務操作。
- Rollback：只切回本機上一個完整且相容的程式版本／venv；驗證內容、共用鎖與 pending 同樣適用。不回復 APT、DB、LVM、Swap 或任何服務設定。
- 已執行的舊版 root console 在 current 變更後必須停止接受修改，請使用者重開；更新頁成功後退出舊 TUI。launcher 啟動不連網、不更新。

## 實作介面（共用）

`server_setting/release_update.py` 為可獨立由 Python 執行的 stdlib 模組，bootstrap 不要求 Textual。

```python
class ReleaseError(RuntimeError):  # .code
    pass

class ReleaseManager:
    def __init__(self, root=None, fetcher=None, runner=None): ...
    def status(self) -> dict: ...  # supported, managed, installed, previous, pending, detail
    def check(self, tag=None) -> dict: ...  # status, current, release, notes, fingerprint
    def plan(self, tag=None) -> dict: ...  # public reviewed pinned plan
    def apply(self, plan) -> dict: ...  # UPDATED/NO_CHANGE, restart_required
    def rollback_plan(self) -> dict: ...
    def rollback(self, plan) -> dict: ...  # UPDATE_ROLLED_BACK
```

standalone entry：`python3 release_update.py --install-local ARCHIVE --manifest FILE --signature FILE --signers FILE [--root DIR] --confirm`；先重驗簽、hash、staging trust，bootstrap 不直接以 root 跑未驗證內容。允許 fixture 自訂 root，正式預設為 `/opt/3waServerSetting`；root 服務路徑不隨測試 prefix 變更。

主 CLI：`--update-status`／`--update-check`／`--update-plan`／`--update-apply --confirm`／`--update-rollback --confirm`，選版如 `--release-tag v2026.10.25.001`；與其他 root／text 模式互斥。TUI 獨立 Software update 頁，Check／Apply／Rollback 明確檢閱，非 root 只能查詢。

## 驗收

真實簽章成功／錯 key／錯 namespace／改包、archive traversal／symlink／多餘檔案／過量、wheel 缺失、下載中斷、相容性錯誤、pending／busy、current stale、切換中斷、smoke 失敗、legacy 移轉、無 Git 版本顯示、更新與回退、普通啟動不連網、舊版 root console 拒絕後續寫入。`.26` 保留 SSH、完整 config／backup／modules／交易、16 GiB swap／LV、Feather SQL 與 BanIP；安裝／更新不安裝業務套件、不改 firewall 或 reboot。

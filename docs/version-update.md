# 版本與自更新設計

2026-10-25 使用者已核准 CI、Release、首次安裝、明確更新與程式回退，本輪依 [Release 安裝與更新規格](release-distribution.md) 實作。Repo 名稱 `3waServerSetup`，安裝名稱／根目錄 `3waServerSetting`／`/opt/3waServerSetting`，主入口 `run.sh` 不變。下方保留設計原則，實際版本與驗收以發行說明及操作文件為準。

範圍更新：第一版已明確排除VPN／PPP／NAT管理，不再等待其遷移決策；下文保留私人舊nat資料的要求仍適用，更新程式不可覆蓋或自動遷移原整機規則。

## 版本與來源

依使用者指定採日期版號 `YYYY.MM.DD.NNN`，tag 如 `v2026.10.25.001`；日期有效、三位流水號由001起，同日依序002／003，預發版可加-rc.N。版本比較依日期、流水號與rc階段，不依字串排序；資料不相容性另外由manifest的data_schema與相容性檢查管理。release manifest記錄版本、完整commit、設定schema相容範圍、Python／依賴鎖定資訊、檔案清單及hash、實際驗證平台與測試摘要。UI顯示安裝版本、commit 與來源；不得把支援目標22.04／24.04／26.04寫成已通過。Ubuntu 26.04 的服務、Feather CLI／FPM 及真重開已完成驗證，見[原生紀錄](ubuntu26-root-validation.md)；24.04 的安裝／更新與服務管理需分別實測標記。

固定來源git@github.com:shadowjohn/3waServerSetup.git；可選相同repo的公開HTTPS來源https://github.com/shadowjohn/3waServerSetup作不需私鑰的分發，不接受UI任意URL或不明鏡像。保留既有SSH信任與金鑰；認證／host key失敗即停止，不自動接受新信任或建立金鑰。HTTPS亦需有效TLS驗證。tag名稱可能被移動，選版時須固定完整commit並在下載後再次比對，不能只憑tag文字認定內容未變。

來源身分與完整性分別驗證：HTTPS/SSH傳輸不等於發行者簽章。正式更新使用專用 Ed25519 key 簽署 release manifest，主機固定 `release/allowed_signers` 的信任公鑰，驗證簽名、manifest 的 commit 及檔案 hash。OpenSSH signer identity 為 `3waServerSetup-release`、namespace 為 `3wa-release@3wa.tw`，公鑰指紋 `SHA256:/5IXRjCkghh4QFZGv27zsQA5MWTtvKJUUDPr/YX4toM`；私鑰不進 Git、安裝包或主機。hash 只證明完整性，不能替代簽章；首次執行下載的安裝器由操作者確認來源，後續更新不得靜默更換信任公鑰。

## 操作流程與目錄

啟動run.sh僅本機唯讀顯示版本與現況，不連網、不git pull。使用者按「檢查更新」才執行有timeout的唯讀遠端查詢，顯示tag、commit、release notes、已測平台、必要依賴／schema及是否不相容；操作者選版後預覽並確認更新。無網路、權限不足或來源驗證失敗時保留舊版可啟動，不偷偷fallback到其他來源。

日後安裝布局：/opt/3waServerSetting/run.sh為穩定launcher，使用current指向releases/<version>-<commit>；版本目錄不可由非管理者寫入，config/與backups/留在安裝根、不得放入版本目錄。nat.sh固定入口可為受控wrapper，呼叫current內的程式；私有port/source政策放config/，不與發行程式混存。現有安裝若把私人規則直接寫入nat.sh，必須偵測並先顯示移轉／保留方案，不能覆蓋；舊VPN／PPP／NAT及公開埠不自動遷移，不覆蓋既有功能或轉成新INPUT候選。

下載到工具自有staging目錄，建置獨立版本venv且不碰current；程式、依賴與runtime相容性分開檢查。核對檔案清單／hash，拒絕archive traversal、絕對路徑、不可信symlink與未宣告檔案；不可把遠端shell／安裝hook直接以root執行。依賴依鎖檔與hash取得，含native產物時必須匹配平台與ABI；離線或缺依賴即失敗，不自動使用未鎖版本。安裝依賴或執行新版本測試本身是額外風險，先提供具體計畫與確認，在隔離／低權限驗證環境進行，不把stage等同安全。

validate包含manifest與來源驗證、Python與鎖定依賴相容、非特權自檢、設定schema只讀解析，以及明確保證不套用主機變更的smoke。執行任何來自新版本的程式都在內容驗證後；未審閱的遠端程式不以root跑。stage與validate全部成功後，取得更新鎖並確認沒有進行中的設定交易，重新檢查current與config指紋，再於同檔案系統原子切換current symlink；只承諾該指標切換，不宣稱整個更新跨所有檔案原子。切換前後journal記錄revision，不包含秘密。斷電後以journal及有效指標判定完成／待回復，不依賴單一寫入步驟。

保留上一個已知可啟動版本；切換後用限定的唯讀啟動自檢確認，失敗回指舊版且回報原因，不自動執行新版本「修復」。失敗staging與成功版本清理需顯示版本／空間，不能刪除current、上一良好版、使用中版本或進行中交易所需版本。穩定launcher自身更新是獨立受審計畫，不能偷偷改變current選擇或繞過驗證。

若以Git checkout部署且dirty，列出變更類型並停止切換，不force-reset、clean、stash或捨棄修改；可選保存差異後另建乾淨release，但需要明確方案，不能把未知主機資料塞進Git。偵測config／backups／主機nat內容的保護不只靠.gitignore，發行／更新manifest須明確排除且套用前比對。

## 更新、遷移與回退邊界

程式更新不自動安裝服務、不執行nat.sh套用、不改OS／DB／磁碟／PHP等設定；新增配方只在下次檢視時顯示可選差異。主機設定schema migration有獨立備份、差異、相容性與批准，禁止程式第一次啟動就靜默改寫。若新舊程式無法共讀schema，更新前阻擋並呈現遷移／回退限制；不能先破壞舊版可讀性才聲稱可回退。

版本回退只切回程式及相容venv，不代表回復已執行的資料庫、LVM／檔案系統擴容、服務／防火牆或帳號修改；這些依各自交易與備份處理。離線INPUT產物仍非可直接apply格式，不因版本更新而獲得部署授權。

## 測試契約與待決

Ubuntu26.04→24.04→22.04逐台測：正常升版／選舊版、離線保留舊版、tag移動／簽章錯／hash錯、下載中斷、滿磁碟、惡意路徑／symlink、dirty checkout、原私有nat與config保持不變、依賴ABI不符、進行中交易阻擋、validate／啟動失敗回指、切換前後斷電、schema不相容、回退不誤稱回復主機。HEAD版本顯示與實際執行revision一致，未測平台必須明示。

本輪第一版採 `v2026.10.25.001`，鎖定的 universal wheels 隨包分發，簽章 key 與更新操作契約見 [Release 規格](release-distribution.md)。發版、主機實測與其他平台的實際完成範圍依驗收記錄標示；文件本身不觸發更新、安裝或系統設定變更。

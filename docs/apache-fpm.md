# Apache event 與 PHP-FPM

Apache 頁新增獨立的 handler 切換。先安裝 Apache、PHP-FPM，再按 `Read handler status`，檢閱 `Review event + FPM`；確認後才停用同版本 mod_php 與 prefork／worker，啟用 event、proxy_fcgi、setenvif 及原生相依模組，連到同版本 FPM socket。

此流程對應 [3wa 的 Apache + PHP-FPM + mpm_event 配方](https://3wa.tw/mypaper/?uid=shadow&mode=view&id=2809)，並加入輸入指紋、原生 alias 備份與失敗還原。使用固定的 Ubuntu `mods-enabled`／`conf-enabled` aliases，不移除 mod_php 套件，不改網站內容。PHP 值的儲存與套用仍在 PHP 頁，會分別管理 CLI、FPM。

切換 MPM 需要重新啟動 Apache。確認前畫面列出所有 alias 與服務變更；執行時先測 FPM 設定、啟動並確認 Unix FastCGI request，再重啟 Apache。FPM 會啟用開機啟動，Apache 原本的開機啟動選擇保留。成功代表 event 模組設定、daemon 與 FPM socket 回應通過；自己的 vhost／網站另以 HTTP 實測。

```sh
/opt/3waServerSetting/run.sh --apache-fpm-status
/opt/3waServerSetting/run.sh --apache-fpm-plan
/opt/3waServerSetting/run.sh --apache-fpm-apply --confirm
# 查看完成後的 native module：
apache2ctl -M
```

需要回復時用本區的 `Restore handler`，或：

```sh
/opt/3waServerSetting/run.sh --apache-fpm-restore --confirm
```

這會還原該次切換的 native aliases 與兩個服務先前狀態；原 Apache 未啟動時會維持未啟動。若切換後另改 vhost、Apache／PHP 設定或服務狀態，還原會拒絕，避免覆蓋人工修改。一般 Apache `Restore last` 只還原一般配方，與 handler 的還原分開。

目前只接受 Ubuntu 26.04 單一版本、單一原生 FPM pool／socket、原生 Apache unit 及 include 布局。自訂 PHP handler、外部 include、多版本與特殊服務環境須另行整合；程式不猜目標。若切換及自動還原都失敗，會保留 `RECOVERY_REQUIRED` 私有交易，阻止下一次變更，供依原狀態處理。

長動作會顯示階段與計時。新功能的乾淨 VM 驗收紀錄另見 [Ubuntu 26.04 新機驗收](ubuntu26-fresh-validation.md)。

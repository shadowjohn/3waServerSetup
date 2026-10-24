# Firewall Offline Planner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 保留預設拒絕與port/protocol/source白名單精神，產出去識別IPv4 INPUT候選規則，絕不套用。

**Architecture:** nat.sh僅呼叫標準庫Python離線驗證／產生器。JSON設定不執行shell；輸出僅自有chain，未掛INPUT、不宣稱已保護主機。原檔路由／VPN／NAT另列遷移阻擋，不改寫或刪除現存功能。

**Tech Stack:** POSIX shell launcher、Python3標準庫、unittest。

**Spec:** docs/design.md 與 docs/firewall-offline.md。

## Global Constraints

- 不執行iptables/nft/sysctl、防火牆套用、套件安裝或push。
- 不保存原始主機IP／憑證；範本僅文件保留網段。
- IPv4 INPUT only；IPv6/FORWARD/NAT/容器不聲稱涵蓋。
- 主TUI入口仍為run.sh；nat.sh是子工具。

## Review Focus

- 未知JSON key／protocol／port／非標準CIDR：拒絕，不放寬。
- 缺SSH救援來源或全世界救援：拒絕。
- shell injection、IPv6與不合法set名稱：拒絕。
- 所有一般允許及ESTABLISHED之前執行黑名單；loopback／具體SSH救援為明列例外。
- 產物不含flush他人chain、掛接INPUT、FORWARD、NAT或sysctl。

### Task 1: Offline firewall candidate

**Files:** Create nat.sh, firewall_plan.py, config/firewall.example.json, tests/test_firewall_plan.py, docs/firewall-offline.md。

**Interfaces:** load_config(path)->dict；render(config)->str；CLI --config PATH [--validate|--render]，無apply。

- [x] 寫unittest測候選順序／預設DROP、公開80/443與限制來源、嚴格輸入與缺救援拒絕、禁止全域修改。
- [x] 執行python -m unittest discover -s tests -p test_firewall_plan.py，確認缺module的預期失敗。
- [x] 實作上述純函式與明確無apply的launcher，範本不自動放開FTP passive或VPN。
- [x] 同一命令確認全部PASS，驗證CLI輸出與錯誤exit；bash -n通過（受限signal pipe失敗後僅提升唯讀語法檢查，未執行內容）。
- [x] 更新設計原檔審查結果與未覆蓋的NAT／FORWARD行為，reviewer完成；不commit。

### Task 2: SQLite3獨立RTree smoke

**Files:** Modify tests/sqlite-smoke.php、tests/README.md、docs/design.md。

**Interfaces:** 獨立SQLite3 RTree結果；失敗／缺失不阻止PDO與SpatiaLite。

- [x] 加入SQLite3 memory建表／insert／overlap正反例與獨立結果。
- [x] PHP lint及本機四項PASS；-n缺能力exit2，尚未Ubuntu／FPM實測。
- [x] 更新說明並review。

最後驗證：Python全部5個test通過；review後新增source／展開上限測試先觀察FAIL，再修正為PASS。PHP lint及四項實際SQL PASS，legacy exit0；無driver exit2。僅離線，Linux parser／Ubuntu／FPM／實際規則掛接未測。

本輪收斂（最新授權）：第一版不管理VPN／PPP／NAT，只保留HTTP80／HTTPS443／SSH22／MariaDB3306／FTP21＋可調PASV／SMB445預設項，全部顯式停用；FTP20僅說明，NetBIOS另選。ping／MQTT專用項目已移除，TCP／UDP自訂仍可用。新增enabled與限定來源／public互斥驗證，12項擴充中的延後項移除後，目前全部10個unittest通過；含範本範圍、預設停用、自訂UDP公開僅該port、來源與範圍負例。沒有主機防火牆套用或GUI實作。

計畫依已明確授權本輪執行；只交付離線產生器，實際掛接／救援／持久化仍待後續核准與VM驗證，不將計畫核准視為主機操作批准。

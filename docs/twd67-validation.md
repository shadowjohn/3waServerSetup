# TWD67：EPSG:3827／3828 轉換比對

依使用者選擇，先比較現代 GDAL／PROJ 與控制點，再決定轉換參數。本輪尚未選定或套用修正，不修改套件的 `proj.db`，也不把舊版 `pcs.csv` 修補方式直接套到現代系統。

## 本機觀測

2026-10-26 在 Ubuntu 26.04 測試 VM，以一般帳號進行唯讀盤點：GDAL／ogr2ogr 3.12.2、PROJ 9.7.1，EPSG 資料庫 v12.029（2025-10-03）。`PROJ_NETWORK=OFF`，沒有 PROJ_DATA／PROJ_LIB 覆寫或個人 proj.db。`/usr/share/proj/proj.db` 前後 SHA256 同為 `5de5ca091a5e6be8222c71a89bc5f4ad673c3bd975684a4a5ab07e1fd1eb0e83`，`dpkg -V proj-data` 沒有差異。

| 路線 | 目前觀測的 operation |
|---|---|
| 3828 → 4326 | Ballpark，accuracy unknown |
| 3827 → 4326 | Ballpark，accuracy unknown |
| 3828 → 3826 | EPSG:15487，E +828.589m、N −206.915m，標示 accuracy 7m |
| 3827 → 3825 | Ballpark，accuracy unknown |

EPSG:3828 為 121° 分帶，3827 為 119° 分帶。需要分別確認投影參數、datum operation、使用區域與輸出軸序，不能用另一個分帶的成功結果替代驗證，也不能用來回轉換一致當作絕對精度證明。坐標系統與成果年份定義見 [NLSC 平面控制](https://www.nlsc.gov.tw/cp.aspx?n=1482)。

## 舊配方與資料來源

[羽山原文 1773](https://3wa.tw/mypaper/?uid=shadow&mode=view&id=1773) 修補舊 GDAL 的 3828 設定；[現有 3wa 轉換頁](https://3wa.tw/demo/php/map/lat_lng_change/) 的 3828 使用同一組 `towgs84`。兩者可作既有應用相容性的基準，但本輪不將網站輸出當作獨立測量控制點。

舊 GDAL 的 SetTOWGS84 與 Proj4js 實際都以角秒解讀旋轉、ppm 解讀 scale。原文沒有說明參數推導與來源單位，故本輪不自行把數值當作 radians／比例因子重新換算，也不把它擴寫成已認證的 3827 參數。

[OSGeo 2009-12-07 共同點資料](https://wiki.osgeo.org/w/index.php?oldid=43649&title=Taiwan_datums%2FTest_points) 有本島與澎湖的 TWD67／TWD97 座標，可作工程差異比較；資料未標原始成果單與觀測 epoch，不能據此宣稱測量級精度。只選本島及澎湖點，排除頁面明示為軟體轉換的金門、馬祖資料。不將這些數值重新標為 TWD97[2010] 或 TWD97[2020]。

官方 [NLSC 坐標系統轉換程式](https://www.nlsc.gov.tw/cp.aspx?n=14018) 使用坐標差網格與雙線性內插，其公開說明涵蓋 TWD97／[2010]／[2020]；不能用這段說明推定存在一組適用全部 TWD67 地區的七參數。決定參數前，仍需核對同一點、同成果系統與年份的可靠坐標對。

## 比較方法

以本島北／中／南與澎湖三點，分區列出各路線與公開 TWD97 座標的平面殘差、RMSE 及最大值。直接使用 `ogr2ogr` 的 `/vsistdin/`、`/vsistdout/` 及 `gdaltransform`，不寫入 VM 資料檔或資料庫。舊七參數僅列為比較案例，不寫入系統設定。

GDAL 3.11 起可用 `-ct_opt ALLOW_BALLPARK=NO` 明確拒絕 ballpark；此選項用於確認「沒有可接受的 operation」時的拒絕行為，拒絕結果不視為坐標驗收成功。選項與版本限制見 [ogr2ogr 官方文件](https://gdal.org/en/stable/programs/ogr2ogr.html#cmdoption-ogr2ogr-ct_opt)。

## 六點工程比較結果

實際執行上述工具後的平面 RMSE（m）：

| 分區／每區三點 | 預設轉 4326，再投影到 TWD97 | 預設直接轉同區 TWD97 | 舊七參數轉 4326，再投影到 TWD97 |
|---|---:|---:|---:|
| 本島，3828／121° | 851.972 | 2.731 | 3.155 |
| 澎湖，3827／119° | 847.618 | 847.618 | 5.752 |

舊七參數的本島最大殘差為 4.528m、澎湖為 8.268m。119° 案例是把相同七參數與 119° 中央經線組合的明示假設，並非網站現行 3827 定義，也不是已核定的澎湖參數。4326→TWD97 這一段同樣未宣稱有獨立 datum 精度；上述數字衡量的是整條工程路線對公開成對 TWD97 座標的殘差。

兩區的 `ALLOW_BALLPARK=NO` 都 exit 1、沒有輸出 features，符合沒有可用非 ballpark 路線時拒絕的預期。`proj.db` 前後 hash 一致；明確 VM 檔案／設定寫入為 0。ignored 證據：`.root-dev/twd67-comparison-20261026T140746Z-b4f03641e4fb.json`。

結果支持「省略 datum 修正會造成約 850m 偏移」的實務觀察，但這批資料的 epoch／原始成果單不足，尚未據此挑選或安裝全域參數。官方 WRA 報告的另一批配對表也因 epoch、局部六參數擬合與表格版本差異，沒有升格為獨立精度驗收依據。

## 既有 3wa_geo 脈絡

另以唯讀方式核對 `3wa_geo` 的 2026-09-13 history 與 `geo_transform.php`：當時修的是 3827 舊 TM2 計算約 0.44m 的投影差異，使用 119°、a=6378160、1/f=298.25 的精確投影路徑；註解明示沿用地理坐標而未補 datum shift。3828 另有 3WA 七參數。其 GDAL 一致性 fixture 不能替代獨立測量控制點。

「洗全台地址」專案的 2026-09-10 修正則是金門來源 EPSG:3825 誤標 EPSG:3826，造成約 2° 偏移。這三種情況分別涉及投影計算、datum operation 與來源 CRS 標記，本輪不把它們合併為同一個修正。

# 冷門城鎮：建成區搜尋

在座標序列管理中選擇「Pikmin 冷門城鎮・建成區搜尋」。
`less_known_inhabited.json` 由產生器完成資料檢查後寫入；`report.json` 保留每個中心的依據與未能確認的城鎮。

## 選點與範圍

- 每個 GeoNames 城鎮使用自己的地點作為搜尋錨點。同一大都會區的不同城鎮不再全部共用聚落總中心。
- 讀取 ESA WorldCover 2021 v200 的 10 公尺建成區分類，以 100 公尺分析格彙整。批次模式再做局部投影的面積平均重採樣，邊界屬近似值。
- 中心必須在建成區比例至少 25% 的分析格；周圍 500 公尺鄰域也需要形成連續建成區。優先選城鎮地點附近的密集建築群。
- 每個掃描畫面至少有 15% 的面積落在選定建成區；排除孤立農舍、遠方片段與資料不足的格子。
- 城鎮附近 3–8 公里是資料搜尋上限，實際掃描範圍由建成區遮罩決定，不會補成整個長方形。
- 資料不足或找不到足夠建成區的城鎮保留於報告，不再用人口換算出大片搜尋半徑。
- 四向格距依產生時指定手機的校正資料及各城鎮緯度換算。更換裝置或地圖縮放後應重新產生。

WorldCover 的「建成區」也包含道路、工業用地等，不能證明當下有住戶或遊戲目標；本序列結合 GeoNames 居住地點和人口資料，用來提高掃描集中於城鎮建築群的程度。

## 重建

安裝專案的 `routes` 額外套件後執行：

```powershell
uv sync --extra routes
uv run --extra routes python scripts/build_inhabited_city_routes.py --device-serial YOUR_DEVICE_SERIAL --workers 6
```

可用 `--city-id geonames-3025509 --output-dir scratch/inhabited-preview` 重建單一城鎮。
公開影像只讀取需要的區塊，快取保存在 `data/reference/inhabited_routes`；重新執行會沿用已完成的結果，資料取得失敗會重試。預設有下載失敗時不發布不完整序列。

## 資料來源

- Zanaga et al. (2022), **ESA WorldCover 10 m 2021 v200**：<https://doi.org/10.5281/zenodo.7254221>，CC BY 4.0。
- © ESA WorldCover project 2021 / Contains modified Copernicus Sentinel data (2021) processed by ESA WorldCover consortium。
- GeoNames `cities15000`：<https://download.geonames.org/export/dump/>。

既有全球序列的國家與城市選擇由來源序列繼承。本產生器不改變其國家篩選規則。

`YOUR_DEVICE_SERIAL` 請換成自己的 ADB 裝置序號。裝置校正資料只供本機計算使用；匯出的路線與報告不附帶裝置序號、校正原始座標或校正時間。

# 完成的視覺模型

此目錄隨 Git push / clone 保存完成權重。`DEFAULT.txt` 指向目前預設辨識模型，`RESIDENT.txt` 指向不含活動菇的常駐模型；兩者使用此目錄內相對路徑。

每個 run 包含 `weights/best.pt` 與 `.artifact.json`（類別、完成狀態、SHA-256）。訓練成功時先驗證、發布權重，再清理該輪背景與 dataset；月度流程須兩階段都成功。未完成 checkpoint、來源截圖、dataset、裝置設定不提交。

目前發布：
- `resident_2026-09_20260912_002014`：13 類常駐模型。
- `activity_2026-09_20260912_115412`：14 類活動模型。
- `crystal_water_correction_20260913`：14 類水晶／水菇修正模型，預設辨識使用此版。

執行期清理不刪此目錄；同名 run 的不同權重拒絕覆寫。之後訓練產生的新模型會出現在 Git 待提交清單，仍需正常 commit / push。詳細政策見 `docs/DATA_MANAGEMENT.md`。

# 公開版本與本機資料

公開儲存庫包含程式碼、依賴鎖定檔、範例設定、完成的視覺模型與來自公開地理資料的城市路線。

以下資料由 `.gitignore` 排除，應保留在自己的電腦：

- `runtime/`：Discord Bot Token／Webhook、裝置序號、校正位置、使用者設定及執行紀錄。
- `routes/imported/`、`routes/_monthly/`：自行匯入的座標與本機活動路線。
- `data/`、`models/`、`output/`、`outputs/`、`scratch/`：來源截圖、訓練資料、未發布模型、辨識結果與診斷資料。例外：`models/published/` 中的完成權重、metadata 與模型指標會提交。
- Android 簽章金鑰、`signing.properties`、`local.properties`、`.env`、憑證、備份與安裝產物。
- `tests/fixtures/restart_controls/*.png`：可能辨識出地點的私人地圖截圖；未提供時，五項相關測試會略過。

已生成的公開城市路線保留搜尋座標與資料來源，但移除裝置序號、原始校正位置與校正時間。路線產生器也不再把完整裝置校正資料寫進匯出檔案。

首次 clone 已有可用辨識模型；要重新訓練仍需提供素材，建置 Android 則需設定簽章。既有 release 工具會核對維護者憑證，未設定對應本機簽章時會拒絕建置；公開儲存庫不包含該私鑰。

提交前先檢查 `git status --short` 與實際暫存內容；不要以 `git add -f` 加入上述資料。`.gitignore` 不會移除已提交的資料，因此新增資料來源或公開既有歷史前仍需重新檢查。

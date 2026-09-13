# 專案目錄維護規則

## 目錄責任

| 路徑 | 內容 | 是否可重建 | Git 建議 |
| --- | --- | --- | --- |
| `frontend/` | Electron main/preload、React/TypeScript UI、hooks、services 與手寫 CSS | 否 | 提交；忽略 `node_modules/`、`dist/`、`release/` |
| `bloompilot/` | 正式 Python 程式碼 | 否 | 提交 |
| `tests/` | 單元測試 | 否 | 提交 |
| `scripts/` | 安裝、環境與裝置診斷腳本 | 否 | 提交 |
| `data/source/backgrounds/` | 依城市分類的原始背景 | 否 | 不提交或使用 Git LFS |
| `data/source/targets/` | 透明辨識目標與參考素材 | 否 | 不提交或使用 Git LFS |
| `data/generated/` | 合成圖片、標註、預覽與 NPY cache | 是 | 不提交 |
| `models/published/` | 完成模型、SHA-256、預設與常駐指標 | 自動發布、執行期不刪除 | 提交 |
| `models/pretrained/` | 下載或提供的起始權重 | 可重新取得 | 不提交或使用 Git LFS |
| `outputs/training/` | checkpoint、指標與訓練圖表 | 否 | 不提交；另行備份重要 run |
| `outputs/recognition/` | 自動巡圖命中原圖、標框圖與 JSON 紀錄 | 否 | 不提交；按需要備份 |
| `runtime/` | 使用者設定、蒐集檢查點、程序狀態與控制訊號 | 部分；不能整包刪除 | 不提交；備份設定與尚未匯出的素材 |
| `vendor/` | 下載的第三方可執行工具 | 是 | 不提交 |

## 放檔原則

- 新城市背景放到 `data/source/backgrounds/<城市>/`，不要放在專案根目錄。
- 新 target 放到 `data/source/targets/`；不參與合成的參考圖放到其 `source/` 子目錄。
- 新的預訓練模型放到 `models/pretrained/`，並在 `config.yaml` 的 `training.model` 指定。
- 程式產生的資料只進入 `data/generated/`、`outputs/` 或 `runtime/`。
- scrcpy 由安裝腳本管理在 `vendor/scrcpy/`，不要手動散放 DLL 或 EXE。
- 正式邏輯放在 `bloompilot/`，一次性維護與診斷工具放在 `scripts/`，測試放在 `tests/`。
- 城市蒐集工具位於 `scripts/collect_1000_city_swipes.py`，搭配同目錄的 `zoom_pikmin_map_out.ps1`；移動的是程式碼，素材及檢查點仍留在指定輸出路徑。
- 桌面顯示邏輯放在 `frontend/src/`；renderer 不直接操作 Python、ADB 或 Node API。

## 路徑管理

固定的應用程式路徑集中定義於 `bloompilot/paths.py`。資料來源、生成資料、訓練輸出與起始模型以 `config.yaml` 為準，並相對於設定檔所在目錄解析。新增路徑時應優先延伸這兩處，不要在 GUI 或 worker 中加入新的字串拼接路徑。

## 手機自動化輸出

- 背景蒐集寫入 `data/source/backgrounds/<城市>/`，以現有最大 `_bg_<編號>_` 接續命名，拒絕覆寫。
- 自動巡圖辨識寫入 `outputs/recognition/recognition_<時間>/`，命中原圖與標框圖分開存放，並以 `detections.jsonl`、`summary.json` 保存可機器讀取的結果。
- 座標序列辨識寫入 `outputs/recognition/geo_sequence_<時間>/`；每筆命中除了 target、信心與框座標，還會保存當下的 `lat`、`lng`、序列索引與節點資訊，可選擇同步收藏到 Android 端。
- 掃描以角色為長方形中心逐圈向外涵蓋所有二維格點，每圈開始與任務結束時都透過遊戲定位按鈕校正；長方形半徑與新區域載入等待時間由 `config.yaml` 的 `phone_automation` 控制。
- 座標序列辨識由電腦透過 ADB USB 本機 socket 控制 Android 的定位核心，依節點與真實公尺間距產生 GPS 格點；桌面 UI 將它與螢幕拖曳巡圖集中在同一個「手機辨識」入口。
- `runtime/phone_automation/settings.json` 是共用的持久設定檔；資料集、訓練、背景蒐集及辨識頁各自保存所屬參數。檔案缺少或損壞時自動回到 `config.yaml` 預設值。
- `runtime/phone_automation/device_scales.json` 依 Android serial 保存地圖兩軸的經緯度／像素比例尺；垂直注入位置往返驗證成功後，會以北向 2D 地圖的等比例特性推算水平值並原子更新。
- `routes/imported/*.json` 保存桌面端新增、編輯與匯入的自訂座標序列，並與內建 `routes/**/*.json` 共用同一份清單。舊版 `settings.json` 的 `geo_sequences` 只作為一次性遷移來源。
- 各功能保存的設定仍以深層覆寫方式套用，不直接重寫 `config.yaml`，因此原始註解與預設值會保留；GUI、training worker 與命令列都透過同一個 `load_config` 取得有效設定。

## 整合模組

- `frontend/electron/main.cjs`：Electron 視窗、動態埠與 Python 後端生命週期。
- `frontend/electron/preload.cjs`：限制過的原生檔案選取／開啟 context bridge。
- `frontend/src/components/`：桌面頁面、Leaflet 地圖、控制面板、側欄及狀態列。
- `frontend/src/hooks/`：WebSocket、裝置 session 與背景工作狀態。
- `frontend/src/services/api.ts`：REST API client 與 Electron 動態埠解析。
- `bloompilot/web/server.py`：FastAPI routes、WebSocket 與安全 shutdown。
- `bloompilot/web/runtime.py`：DeviceSession、JobManager 與所有 Python 工作 adapter。
- `bloompilot/web/events.py`：跨 thread 的 WebSocket event fan-out。
- `bloompilot/device_control.py`：ADB port forwarding、配對與版本化 JSON-lines 指令客戶端。
- `bloompilot/geo_scan.py`：不依賴 GUI 或 YOLO 的地理格點規劃與掃描迴圈。
- `bloompilot/geo_recognition.py`：串接裝置控制、ADB 截圖與 YOLO 命中紀錄。
- `bloompilot/geo_recognition.py` 的 sequence runner：一次載入模型後依序執行多個 USB 中心座標及各自附近格點。
- `bloompilot/phone_tools.py`：ADB/scrcpy 工具發現、USB 裝置枚舉與指令輔助。
- `bloompilot/phone_stream.py`：低延遲 USB H.264 串流與只保留最新影格的通道。
- `bloompilot/detection.py`：共用模型選取與辨識框繪製。
- `bloompilot/training_output.py`：訓練輸出清理與 GPU 資訊解析。
- Python 核心不依賴桌面 UI；舊 Tkinter 介面已移除，桌面操作統一由 `frontend/` 提供。

## 產物狀態與指標

- 統一索引位於 `runtime/artifacts/catalog.json`，由 `python -m bloompilot.artifacts refresh` 重建。
- Dataset ID 格式為 `dataset:<目錄名>`。
- Training run ID 格式為 `run:<目錄名>`。
- 模型 ID 分為 `model:pretrained:<檔名>` 與 `model:trained:<run>:<best|last>`。
- `LATEST.txt` 與 `LATEST_RUN.txt` 只儲存管理根目錄內的相對路徑；發布模型另有可攜的 `models/published/DEFAULT.txt` 與 `RESIDENT.txt`。
- Dataset 正在產生或被 training run 參照時會標為受保護。
- Running、paused 與 resumable training run 會標為受保護。
- `training.model` 指定的預訓練模型受保護；快取清理只允許處理 dataset 內的 `.npy` 與 `.cache`。

目前模型保留規則由 `config.yaml` 的 `artifacts.auto_prune_completed_weights` 控制。啟用時：

- 保留 `training.model` 指定的預訓練 `yolo26s.pt`。
- 每個 completed run 保留原位置的 `weights/best.pt`，不建立額外的 latest `.pt`。
- Completed run 的 `epoch*.pt` 與 `last.pt` 會清除。
- Running、paused、stopped、resumable 或無明確狀態的 run 不會自動刪除權重。

手機辨識的預設模型保存在 `runtime/phone_automation/settings.json` 的 `recognition_default_model`，只記錄專案相對路徑，不複製 `.pt`。沒有本機設定時使用已提交的 DEFAULT 指標。一般訓練成功發布模型後清理該輪 dataset 與已使用背景；月度流程等兩階段成功再清理。已發布模型受保護。

## 素材選取生成

資料集 GUI 可個別多選背景與 target，也可按城市批次加選。後端使用素材根目錄內的相對路徑驗證選取內容，拒絕絕對路徑、`..` 或不存在的項目。生成後，完整選取清單會寫入 `metadata.json` 的 `source_selection.backgrounds` 與 `source_selection.targets`。

命令列可重複使用選項完成同樣操作：

```powershell
python -m bloompilot.dataset_builder `
  --background "paris/paris_bg_000_center.png" `
  --target "Large_Red.png"
```

每次資料集產生完成與訓練完成時都會自動刷新 catalog。訓練暫停或安全停止時，run 的 `.artifact.json` 也會同步記錄狀態，讓續訓判斷不再只依賴 epoch 數。

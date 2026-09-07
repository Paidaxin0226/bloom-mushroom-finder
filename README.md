# BloomPilot

BloomPilot 是一套以 Electron + React/TypeScript 為桌面介面、FastAPI + WebSocket 為本機橋接層、Python 為自動化與 YOLO 核心的 Windows／Android 整合工具。

同一個桌面視窗內包含 Android 定位與步數控制、座標序列辨識、ADB 畫面擷取、每月活動菇模型流程及自動搜尋辨識。桌面介面統一使用 Electron；Python 核心不依賴 Tkinter。

側欄的「手機辨識」包含「自動搜尋辨識」與「座標序列辨識」。每次搜尋前仍會檢查所選手機是否已有比例尺註冊，未註冊才自動量測；命中後則不再用比例尺推估座標，而是依序開啟 2D 地圖目標、蘑菇詳情與「在地圖應用程式開啟」，從 Android 地圖 Intent 直接取得 `precise_coordinate`。擷取租約有效時 LocWarp 只回傳經緯度，不執行瞬移、導航或收藏，完成或失敗後立即解除；若蘑菇已有遊戲內路線也不會修改它。「即時命中」除了在畫面保留最近結果，也可保存並啟用 Discord Webhook，同步播報目標、信心、人數與可點開的精確座標。座標序列使用和其他辨識工作一致的左右欄介面，完整地圖只在選取節點座標時以彈窗開啟；序列可保存、編輯、排序及匯入／匯出 JSON。執行時會依序透過 USB 前往每個經緯度，等待節點指定時間（預設 10 秒），完成該節點附近網格搜尋後才前往下一點。原本只處理一個座標的單一中心模式已移除，可先用裝置控制移動，再執行自動搜尋辨識。

即時命中支援 **Discord Bot Token／Webhook**，會用清楚的 embed 播報人數、信心、座標及目標裁切圖片。Bot 只需貼上 Token，即可取得可用頻道；多個頻道時再從清單選擇。詳見 [Discord 播報設定](docs/DISCORD.md)。

精確座標擷取會先檢查目標是否靠近邊界、被裁切或落在控制區域；邊角目標最多以兩次短距離地圖平移移入中央，等待畫面穩定後重新辨識，並比對地圖實際位移、目標種類與外觀。只有能唯一確認的目標才會點擊。完成或失敗後會核對並恢復原搜尋位置，其餘目標也會重新辨識，不沿用平移前的點擊位置。擷取過程不改變 GPS 定位或縮放，精確座標仍來自地圖 Intent。

搜尋位置恢復會依每次拖曳後的實際地圖位移校正拖曳倍率，並處理手機忽略過短手勢的情況，避免來回拖過頭或停在小幅誤差。局部校正最多六次，每次都重新核對畫面；若需重新準備遊戲與搜尋格，重建後另有完整的校正與核對機會。地圖準備與回復會等待道路等靜態特徵停止移動，蘑菇和花朵動畫或短暫的未知轉場影格不會觸發誤判。

每個目標的擷取流程設有 45 秒處理期限（正在執行的 ADB／USB 呼叫另受各自逾時限制），失敗結果會在目前搜尋格內保留，避免反覆嘗試。失敗截圖與原因保存在本次辨識輸出的 `coordinate_failures/`。若無法回到地圖，會使用既有遊戲恢復流程並重建目前搜尋格；無法核對恢復位置或解除座標擷取狀態時，停止本次辨識並回報錯誤。

長時間辨識的 USB 心跳使用輕量 `ping`，座標擷取的開始、輪詢與結束也只回傳當次租約資料，不會在手機忙於地圖動畫時重複組合完整裝置狀態。Android 端會在 45 秒後回收中斷且無心跳的 ADB socket，避免舊連線耗盡可用控制通道。

手機可用記憶體低於總 RAM 的 5%（最低 256 MiB、最高 384 MiB），且連續兩次取樣確認壓力時，座標序列會先在搜尋安全點重新啟動 LocWarp，再恢復 USB、目前節點座標與搜尋格；Pikmin 仍在 2D 地圖時不會重啟遊戲。其他不使用 LocWarp 座標控制的辨識流程仍使用既有遊戲記憶體恢復。這是預防門檻，無法保證 Android 永不終止 LocWarp。控制服務中斷後，序列會最多嘗試三次重連（每次 8 秒，間隔 2／4 秒）；恢復後在下一個搜尋格檢查點重新注入目前節點座標。Pikmin 若仍在既有 2D 地圖，只重新定位與校正北向並接續目前搜尋格；遊戲也被系統清除時才執行完整重啟。若服務在地圖恢復期間再次中斷，連線、座標與地圖會整組最多重試三輪。介面斷線時每 5 秒檢查既有裝置連線，手動中斷後不會自動連回。桌面程式把工作訊息、記憶體取樣與 USB 失敗原因寫入 `runtime/logs/bloompilot.log`，每份 5 MiB 並保留三份輪替備份。

## 快速開始

第一次或平常使用都可直接雙擊 `啟動BloomPilot控制中心.bat`。啟動器會依需要建立 Python `.venv`、安裝鎖定依賴與前端套件，接著由 Electron 啟動只監聽本機的 FastAPI 後端。

```powershell
# 安裝／修復全部環境
.\安裝環境.bat

# 啟動 Electron 控制中心
.\啟動BloomPilot控制中心.bat

# 只檢查環境、後端 import 與 TypeScript 型別
.\啟動BloomPilot控制中心.bat --check

```

## 架構

```text
Electron main process
├─ 管理 BrowserWindow、Python 後端生命週期與原生檔案對話框
└─ React + TypeScript + Vite renderer
   ├─ components/   頁面、地圖、控制面板、側欄與狀態列
   ├─ hooks/        WebSocket、裝置與工作狀態
   └─ services/     本機 REST API client
             │ HTTP + WebSocket（127.0.0.1 動態埠）
             ▼
FastAPI bridge: bloompilot/web/
             │
             ▼
Python core: ADB、Android 控制、資料集、YOLO、訓練與自動清理
```

所有桌面 UI 都使用手寫 CSS、React function components、hooks、service 函式與 inline SVG；地圖使用 Leaflet。Electron 關閉時會通知 FastAPI 安全停止目前工作並關閉裝置連線。

Python 共用功能依責任拆分：`phone_tools.py` 管理 ADB/scrcpy 工具與裝置枚舉，`phone_stream.py` 處理 USB 串流，`detection.py` 處理模型選取與畫框，`training_output.py` 處理訓練紀錄。一次性城市蒐集與地圖縮放工具放在 `scripts/`，不再混入 `runtime/`。

## 專案結構

```text
BloomPilot/
├─ frontend/                   Electron + React/TypeScript + Vite 桌面 UI
├─ bloompilot/
│  ├─ web/                     FastAPI、WebSocket、工作與裝置 session
│  └─ ...                      自動化、資料、YOLO 與訓練核心
├─ tests/                      Python 單元與 Web API 測試
├─ scripts/                    安裝與診斷工具
├─ data/                       原始素材與生成資料集
├─ models/                     預訓練模型
├─ outputs/                    訓練與辨識輸出
├─ runtime/                    使用者設定與暫態狀態
├─ vendor/                     scrcpy／ADB 工具
├─ android/                    Android companion 與 release 工具
├─ contracts/                  Android／Python 共用控制協定
├─ docs/                       架構與維護文件
├─ config.yaml                 資料、訓練與辨識預設值
├─ pyproject.toml              Python 專案與後端 entry point
├─ uv.lock                     Python 鎖定依賴
└─ 啟動BloomPilot控制中心.bat  Windows 桌面入口
```

## 資料集合成速度

「資料集產生」可直接調整本次的合成數與平行工作數；按「儲存目前值為資料集預設」即可保存，不需要離開功能頁。

- `generation.parallel_workers`：同時處理的背景工作數，接受任何正整數，專案預設 4。無程式上限，實際受 CPU／記憶體限制，不保證等比例加速。
- 更新進度間隔固定等於平行合成工作數，最後一批也會回報，不再提供獨立設定。
- 方框尺寸固定為 256 × 256px，舊設定的 `square_size_pixels`／`progress_interval` 不會覆寫固定行為。

命令列亦可覆寫本次設定：

```powershell
.\.venv\Scripts\python.exe -m bloompilot.dataset_builder --parallel-workers 4
```

## 專用訓練與辨識設定

- 辨識推論尺寸固定 1024；手機地圖比例尺按 Android 序號保存在 `runtime/phone_automation/device_scales.json`。每次搜尋前會檢查裝置註冊資料，新手機會自動準備 2D 地圖、量測並保存比例尺，已註冊手機則直接沿用。訓練的 image size 仍可獨立調整。
- 訓練來源自動掃描專案 `data/generated` 已完成資料集；只讀取生成摘要，不再逐張檢查圖片或標註。Ultralytics 自身的資料載入、標註快取與訓練期間驗證仍保留。
- 手機辨識自動掃描 `outputs/training/*/weights/*.pt`，顯示 run 名稱與權重檔名，可在辨識頁保存共用預設模型；設定只記錄相對路徑，不複製 `.pt`。不接受外部模型或預訓練基底模型。
- 手機辨識的目標種類與人數可保存為自動搜尋、座標序列共用的篩選預設；「全部目標」是實際全選，會同步勾選所有個別目標。
- 安全停止等待目前 epoch 保存 checkpoint。在停止生效前可按「取消安全停止」繼續本次訓練；已停止時請按「開始／繼續訓練」由 checkpoint 續訓。
- 每月活動菇工作集中在「每月活動菇模型」單一頁面；合成數、平行工作數、epochs、image size 與 batch 沿用專案設定。

## 每月活動菇模型流程

「每月活動菇模型」會從 `data/source/backgrounds` 提取目前資料集的 13 個城鎮，建立不出現在一般辨識清單中的固定背景路線，並依月份隔離來源與產物狀態。第一次開啟時會把目前預設辨識模型複製為系統管理的常駐模型。

1. 選擇活動月份後，由 USB 依序前往固定的 13 個城鎮與 756 個搜尋位置擷取背景，不需要手動選擇路線。
2. 在縮圖清單逐張檢查；若畫面含有任何蘑菇目標就刪除。刪除或重新蒐集後，必須再次按「背景審核完成」。
3. 加入當月活動菇透明 PNG。檔名會成為 YOLO 類別名稱，不能和常駐菇類重複。
4. 按「開始完整訓練流程」後，程式會將原始乾淨背景與當月核准背景合併，並自動排除檔名標記為 `_target_` 的畫面。先與既有菇類合成資料強化常駐模型，再混入本月活動菇訓練當月活動版。
5. 當月活動版成功成為預設辨識模型後，程式會自動刪除可重建的合成資料集、暫存素材、上個月的活動背景及已被取代的月度模型。原始乾淨背景與目前月份已核准原圖會保留。

活動版完成後會設為辨識預設，但不會寫回常駐模型。下個月份仍從常駐模型重做上述流程，因此不會把上個月已失效的活動菇類帶入新模型。

一般座標序列辨識只顯示 `Pikmin 冷門城鎮・建成區搜尋`；其他既有全球、熱門、冷門分片與舊自訂路線已從選單移除。
- 固定值集中在 `bloompilot/project_defaults.py`，介面顯示對應 `frontend/src/projectDefaults.ts`。

## Android USB 整合

1. 使用 `android/tools/build-android-release.ps1` 建置並安裝簽名 release APK。
2. 手機允許 USB 偵錯；Android App 啟動時會自動開啟「電腦 USB 控制」。
3. Android 系統授權這台電腦進行 USB 偵錯後，BloomPilot 啟動時會自動連接上次使用的手機；首次使用且只有一台可用裝置時也會直接連接。多台裝置且沒有使用紀錄時才需要手動選擇。
4. 「裝置控制」會鏡像 Android 的定位、步數、Apps 與連線四頁；手機地圖長按選單在桌面改用滑鼠右鍵。也可到「手機辨識」建立自動搜尋／座標序列辨識。

手機辨識進入安全暫停後，會自動開啟「BloomPilot 手機滑鼠控制」鏡像視窗。左鍵點擊等同觸控、拖曳等同滑動，也可直接用電腦鍵盤輸入；若手動關閉視窗，可按「重新開啟手機控制」。繼續或停止辨識時視窗會自動關閉，繼續前仍會回注暫停前的搜尋位置並重啟遊戲。

電腦透過 ADB dynamic forward 連接 Android local-abstract socket，不會開放 LAN 控制埠。定位、路線、收藏、步數、Hook、診斷、Relay、浮動控制與可攜式備份直接讀寫手機狀態，不會在桌面建立第二套資料。命中資料會保存類別、信心、框座標與當下經緯度，也可同步加入 Android 收藏。

完整桌面流程見 `docs/CONTROL_CENTER.md`；整合協定與失敗處理見 `docs/INTEGRATION.md`。

## 開發與驗證

公開儲存庫只包含原始碼、公開城市路線與範例設定。Android 簽章私鑰、Discord 憑證、裝置校正與使用者設定、匯入座標、手機截圖、訓練素材及模型均留在本機，不隨 Git 發布。首次使用請自行提供訓練素材與模型，並依 `android/signing/README.md` 設定簽章；公開原始碼不附可直接安裝的 APK。詳見 [公開版本與本機資料](docs/PUBLICATION.md)。

```powershell
# 後端
.\.venv\Scripts\python.exe -m bloompilot.web.server --port 8765

# 前端開發伺服器
cd frontend
npm run dev

# Vite HMR + Electron 開發模式
npm run start:dev

# 型別與 production build
npm run typecheck
npm run build

# Python 測試
cd ..
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Android 只應散布 release 工具產生、並通過憑證指紋驗證的 APK；不要散布 `app-debug.apk`。

## Disclaimer

BloomPilot 是獨立、非官方的個人研究與自動化專案，與 Nintendo、Niantic 或 The Pikmin Company 無關。使用者需自行遵守所使用軟體及服務的條款與政策。

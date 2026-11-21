# Bloom Mushroom Finder integration architecture

Bloom Mushroom Finder 將桌面顯示、Python 運算與 Android 生命週期工作留在最適合的 runtime，並透過本機 API 與版本化裝置協定整合成單一操作流程。

```text
Electron main
├─ 建立 BrowserWindow
├─ 配發 loopback 動態埠
└─ 啟動／停止 Python backend
        │
        ├── React renderer ── HTTP + WebSocket ── FastAPI bridge
        │                                        ├─ DeviceSession
        │                                        ├─ JobManager
        │                                        └─ EventHub
        │                                               │
        │                                               ├─ dataset / training / YOLO
        │                                               └─ ADB / scrcpy capture
        │
        └──────────────── ADB forward ──────────────────┐
                                                        ▼
Android companion
├─ 使用者啟用的 USB control foreground service
├─ RemoteCommandExecutor
├─ mock-location movement engine
├─ step injection
└─ bookmarks、routes 與 status
```

## Desktop bridge

- FastAPI 只監聽 `127.0.0.1`，Electron 每次啟動會選擇可用動態埠。
- renderer 的查詢使用 REST；裝置、工作、進度、log、預覽與 detections 使用 WebSocket 即時推送。每批 detections 也會依本機設定排入 Discord 背景佇列，支援 Webhook 與 Bot Token；只使用 Discord 官方 HTTPS 端點，傳送不阻塞辨識執行緒。Bot 透過 REST 取得頻道並檢查角色與頻道覆寫權限。命中以最多 5 筆一批建立 embed，`crop_data_url` 解碼為 multipart 圖片附件，透過 `attachment://` 顯示在對應目標內；無效圖片保留文字播報。舊 `discord_webhook` 設定區段繼續沿用，缺少 `mode` 時預設為 `webhook`。操作方式見 [Discord 播報設定](DISCORD.md)。
- `JobManager` 保證訓練、YOLO、手機掃描等重工作互斥，取消由 cooperative stop event 傳遞。
- Electron 關閉時先呼叫 `/api/system/shutdown`，讓後端停止工作、移除 ADB forward 並關閉連線；逾時才終止子程序。
- renderer 不直接取得 Node 或檔案系統權限；原生目錄／模型選擇與開啟路徑只經 preload context bridge 暴露的有限函式。

## USB connection

Android 只監聽 local-abstract socket，存取權沿用系統 ADB USB 偵錯授權，不再建立額外 PIN。Windows `DeviceSession` 先透過受 `android.permission.DUMP` 保護的 ADB 指令啟動手機端服務，再建立動態 `adb forward`、連到 `127.0.0.1`、完成協定握手，斷線時移除 forward；手機端同時驗證 socket peer UID 僅能是 shell 或 root。

桌面狀態查詢會在上一筆完成後等候 2 秒再送出，避免手機回覆較慢時累積請求；操作後直接以該指令附帶的狀態更新介面。座標序列每 15 秒檢查一次心跳，只在通道閒置且最近 15 秒沒有收到回覆時送出，忙碌時直接略過。心跳保留 3 秒期限，同一次連線故障只提示一次，確認收到回覆後才重設；序列控制指令仍沿用自動重連，結束作業不啟動長時間重連。

請求與回應限制為 256 KiB；每條 message 都驗證 protocol version 與 request id。支援指令定義在 `contracts/device-control.v1.schema.json`。

## Coordinate-sequence recognition

座標序列辨識使用真實地理 waypoint，而不是從畫面 swipe 推估 GPS：

1. 以中心座標與公尺間距建立由內向外的矩形格點。
2. Android 瞬移或導航至 waypoint。
3. 等待移動與畫面載入完成。
4. 透過 ADB 擷取 PNG，使用選定 YOLO 模型推論。
5. 保存類別、信心、框、經緯度、模型與圖片。
6. 可將命中座標加入 Android 收藏。
7. 完成、取消或失敗時停止移動，並依設定回到序列執行前的位置。

舊畫面拖曳核心仍供背景蒐集與 viewport-only 搜尋使用。

每次搜尋前會以 Android serial 檢查裝置比例尺註冊，只有未註冊裝置才執行一次量測。正式命中座標不再使用比例尺推估：桌面端點擊 YOLO 框，辨識蘑菇名稱卡片、詳情頁及目的地 action sheet，選擇在地圖應用程式開啟；Android companion 以短期 `beginCoordinateCapture` 租約攔截地圖 Intent，透過 `coordinateCaptureStatus` 回傳 `precise_coordinate`，最後呼叫 `endCoordinateCapture`。此分支的優先級高於種花、地圖連結瞬移及 Google Maps 轉送，整段流程不會改變 mock location、收藏或導航；租約亦有逾時保護。若已有遊戲內路線，流程保留路線並略過該筆座標，而不是取消路線。

桌面端不再提供單一中心辨識，座標模式只把完整序列交給同一個 `geo_sequence_recognition` 工作。模型只載入一次；每個節點先以 USB 傳送其經緯度，等待節點設定時間，再建立該中心的附近格點。進度以整個序列的總搜尋點數計算，輸出紀錄同時保存 `sequence_index`、`waypoint_id`、節點名稱與中心座標。

## Resource coordination

- 模型訓練與 YOLO inference 不同時占用 GPU。
- 座標序列辨識在工作期間獨占 Android movement、ADB capture 與 YOLO inference。
- 背景蒐集、自動搜尋辨識與座標序列辨識互斥。
- 沒有手機工作時，已配對 USB command channel 可保持連線。

## Verification

```powershell
cd E:loom-mushroom-finder
.\.venv\Scripts\python.exe -m unittest discover -s tests -v

cd frontend
npm run typecheck
npm run build

cd ..\android
.\gradlew.bat :app:testDebugUnitTest --no-daemon
```

Android release 驗證使用 `android/signing/README.md` 所述簽章；只散布 `android/tools/build-android-release.ps1` 產生的簽名 release。

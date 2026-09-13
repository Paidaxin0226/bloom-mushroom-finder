# 手機管理與背景整輪實機驗證

## 使用者要求與範圍

- 重新蒐集目前設定的完整背景路線；若整輪失敗，分析並修正後從零重跑，不能混用前輪照片。
- 明確要求刪除本月既有背景：已刪除 `data/monthly_activity/2026-09/backgrounds` 的 285 張照片，合計 696,935,742 bytes，確認剩餘 0 張並撤銷審核。
- 目前正式路線為 `_monthly/current_background_towns.json`，13 城鎮、682 格，每格等待 4 秒、手勢 800 ms。本次未縮減路線或範圍。
- 每次實機嘗試使用 `runtime/monthly_activity/real_device_validation/2026-09/attempt_NNN/capture` 的新空白目錄，成功後再放入本月背景。

## 實機觀察到的問題

最初提供的 London 紀錄顯示可用記憶體只剩 217 MB，Pikmin 佔用 2443 MB，接著 LocWarp USB 服務消失。這能確認記憶體壓力與連線失敗相鄰，不能單憑該段紀錄斷言是哪個程序被系統終止。恢復流程已修正：一般重連耗盡後先釋放遊戲記憶體，再重啟 LocWarp，恢復當前 GPS 與地圖；服務啟動後會等待實際 socket 就緒。命令本身遭拒絕不會被誤判成 USB 斷線。

記憶體策略另分開追蹤遊戲重啟時間與 LocWarp 恢復時間，避免頻繁恢復 LocWarp 反而延後遊戲的預防性重啟。嚴重低記憶體且遊戲佔用偏高時，優先處理遊戲；恢復次數仍有上限並尊重停止要求。

**第 2 輪 Sydney 在 606/682 時實際重現三次 USB 重連失敗。** 此時可用記憶體 195 MB、Pikmin PSS 2017 MB。Android `exit-info` 記錄 LocWarp 在手機時間 18:09:28 至 18:10:18 間連續 6 次以 `reason=3 (LOW_MEMORY)` 終止，RSS 約 64–87 MB。這次的連線消失有直接的系統證據，原因是低記憶體終止控制程序。一般重連耗盡後，新流程於手機時間 18:10:23 停止 Pikmin（`FORCE STOP`，RSS 1.8 GB），隨後重啟 LocWarp、連上 USB 並恢復遊戲地圖。手機時間與電腦相差 5 小時，對應電腦約 23:09–23:11；兩份原始紀錄保存在 `attempt_002/usb-recovery-locwarp-exits.txt` 和 `usb-recovery-pikmin-exits.txt`。

1. **充電保持亮屏設定未生效。** MI 9 經 USB 連接電腦，但 Android 回報 `mPlugType=1`（AC）、`mStayOnWhilePluggedInSetting=2`（只涵蓋 USB）、`mStayOn=false`，螢幕逾時為 600,000 ms。原 `svc power stayon usb` 無法在這個充電來源保持亮屏。已改為 `stayon true`，並讓快速地圖準備、遊戲重啟、USB 地圖恢復共用喚醒流程。
2. **快速準備路徑未先喚醒螢幕。** 遊戲已在地圖但熄屏時，原流程可能把黑畫面判為需要完整遊戲重啟。現在先喚醒再檢查地圖；安全鎖仍需使用者在手機上解鎖。
3. **控制服務連線不能代表手機電源狀態。** 新增獨立 ADB 偵測，回報在線、熄屏、待機、鎖定、未授權與離線狀態。ADB 消失無法可靠區分關機或拔線，因此不會斷言手機已關機。離線會清除舊的移動狀態與即時座標顯示。
4. **低記憶體警報容易誤解。** 第 1 輪在恢復冷卻中出現 `9/2`、`10/2` 等訊息；內部是連續取樣次數，並非嘗試重啟失敗。已限制顯示進度到門檻值，並顯示冷卻剩餘時間。
5. **喚醒與查詢的回應競態。** 舊的熄屏查詢可能晚於喚醒結果抵達；前端現在以同一手機的檢查時間拒絕過期結果。

## 嘗試紀錄

| 嘗試 | 結果 | 說明 |
| --- | --- | --- |
| 1 | 為修正而中止，122/682 | 從 20:45:59 開始；低記憶體 LocWarp／USB／座標／地圖恢復後能繼續拍攝。發現保持亮屏未生效，主動停止，並非自然崩潰。 |
| 2 | 完整成功，682/682 | 21:11:18 從空白目錄開始，23:26:45 完成拍攝，23:31:42 完成逐張影像驗證。包含充電保持亮屏與快速準備喚醒修正；實機確認同為 AC 充電時，`mStayOn=true`、設定值為 15。 |

進度與各輪證據：`runtime/monthly_activity/real_device_validation/2026-09/attempt_NNN/status.json`、`bloompilot.log`、`route.json`、手機退出原因紀錄及截圖。

第 2 輪共經歷 14 次低記憶體 LocWarp 重啟、3 次定期遊戲重啟，以及 1 次一般 USB 三連敗後釋放遊戲記憶體的完整恢復。每次恢復均成功接續；沒有整輪終止，沒有混用第 1 輪照片。

## 最終入庫結果

23:32:32 已將第 2 輪照片移入 `data/monthly_activity/2026-09/backgrounds`，共 682 張、1,673,644,582 bytes。已比對凍結路線中的每一個城鎮、搜尋格與檔名，沒有漏格或多餘檔案；SHA-256 完全相同的重複照片為 0 組。入庫後重新點算檔案數與總容量一致，manifest 已標示完成。

| 城鎮 | 張數 | 城鎮 | 張數 |
| --- | ---: | --- | ---: |
| Antalya | 88 | Bangkok | 25 |
| Berlin | 121 | Hong Kong | 20 |
| Las Vegas | 21 | London | 121 |
| Osaka | 25 | Paris | 121 |
| Seoul | 30 | Singapore | 25 |
| Sydney | 30 | Taipei | 25 |
| Tokyo | 30 | 合計 | 682 |

這次通過的是完整收集、圖片解碼、地圖畫面與搜尋格驗證。照片仍保留人工背景審核流程，沒有自動宣告每張均不含蘑菇，也沒有自動啟動訓練。

## 已完成的驗證

- 手機管理加入後完整 Python 測試 385 項：通過 364、略過 21；保持亮屏修正後另跑記憶體／恢復 40 項，全數通過。
- 前端測試與 TypeScript 型別檢查、正式版建置通過。
- 隔離瀏覽器驗證：熄屏仍在線、喚醒後安全鎖提示、離線清除舊狀態、未授權提示、手動斷線不自動重連，無前端執行錯誤。
- 匯入程序隔離驗證：正式路線 13 城鎮、682 格全部入庫，清單與 manifest 一致；目的地已有照片時拒絕覆蓋並保留原檔。
- 實機新模組查詢：MI 9 在線、亮屏、未鎖定、100% 且接電；不存在的測試序號回報 missing，螢幕／鎖定／電量為未知。
- 整輪 682 張通過逐張解碼與 2D 地圖分類，13 個城鎮及全部搜尋格核對通過，已放回本月背景。
- 收集結束後實機熄屏／喚醒測試通過：23:32:48 熄屏回報 `adb_state=device`、`screen_state=off`；23:32:50 喚醒後回報 `device/on`。最後確認手機在線、亮屏、未鎖定、電量 100%、已接電。未實際關機；離線僅能判定 ADB 無法連上，不能從 USB 區分關機與拔線。
- 入庫腳本及熄屏測試腳本語法檢查通過，`git diff --check` 通過。

測試紀錄：`scratch/phone-management-all-python-tests.log`、`scratch/phone-power-recovery-tests.log`、`scratch/phone-management-browser-results.json`。

最終證據：`attempt_002/verified-backgrounds.json`、`publication.json`、`phone-power-validation.json`。重現用腳本為 `scripts/verify_monthly_background_collection.py`、`scripts/publish_verified_monthly_backgrounds.py` 與 `scripts/verify_phone_power_state.py`。

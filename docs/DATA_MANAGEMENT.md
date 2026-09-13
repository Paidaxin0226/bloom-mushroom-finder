# 資料、產物與 Git 管理

Git 保存程式、設定範本、文件、內建路線與必要的回歸測試圖片。`.gitignore` 排除的資料仍在本機；忽略規則不會刪檔，也不是備份。重新 clone 不會取得訓練素材、模型、裝置設定或使用者路線。

## 保存與刪除規則

| 位置 | 用途 | 何時刪除／保留 |
| --- | --- | --- |
| `data/source/` | 原始背景、透明目標素材 | 一般產物清理不刪除，應另行備份。 |
| `data/generated/` | 合成圖片、標註、metadata | ArtifactManager 登錄狀態與訓練引用；一般刪除介面保護建置中或被訓練引用的資料集。月度流程完成後另有專用清理，會刪除該流程的合成資料集。 |
| `data/generated/**/images/*.npy` | YOLO 圖片快取 | 有對應原圖才可清除。`training.cache: disk` 會在之後訓練時重建，磁碟需求可能是 PNG 的數倍。改用 `cache: false` 可省空間，但增加訓練時圖片解碼成本。 |
| `data/monthly_activity/` | 月度背景、活動素材與中間資料 | 月度流程成功完成後刪除合成用中間資料及其他月份工作區；保留當月原圖。需要歷史月份時，必須在下次完整流程前備份。 |
| `outputs/training/` | 權重、訓練設定及紀錄 | `auto_prune_completed_weights: true` 會保留 completed run 的 `best.pt`，清掉其他權重與未被設定使用的預訓練模型；未知或未完成 run 不做這項清理。 |
| 月度舊模型 | 被新常駐／活動模型替換的權重 | 月度流程會移除指定舊來源與舊活動模型；批次清理僅涵蓋具有合法月份及 resident/event 標記的 completed run，保留本輪指定模型及無關實驗。舊 best.pt 並非永久備份。 |
| `outputs/recognition/` | 命中原圖、標註圖、精確座標與摘要 | 目前沒有依天數自動清理；本次保留。 |
| `runtime/` | 本機設定、裝置校正、log、診斷與工作狀態 | 不應整個刪除：其中含必要設定。log 有輪替，但診斷圖片沒有统一期限。 |
| `routes/imported/` | 使用者匯入路線 | 本機保存、Git 忽略，需另備份。 |
| `.venv/`、`frontend/node_modules/`、建置目錄 | 執行環境與可重建產物 | Git 忽略；為保持本機可啟動，本次保留。 |

## 盤點與清理

在專案根目錄使用 PowerShell：

```powershell
.\.venv\Scripts\python.exe -m bloompilot.artifacts list
.\.venv\Scripts\python.exe -m bloompilot.artifacts verify
.\.venv\Scripts\python.exe -m bloompilot.artifacts clean-cache
# 上一行是預覽；下面才會刪除可重建快取。請先停止訓練。
.\.venv\Scripts\python.exe -m bloompilot.artifacts clean-cache --apply
.\.venv\Scripts\python.exe -m bloompilot.artifacts prune-models
```

`prune-models` 預設只預覽。不要用 `git clean -xfd` 整理這個專案，它會連被忽略的原始素材、模型、設定一起刪除。產物驗證只檢查基本結構，不代表標註品質或模型辨識效果合格。

## 2026-09-13 清理紀錄

- 清理前 `data` 約 320.55 GiB；移除生成資料集 33,124 個快取（233.87 GiB），另移除實驗資料 704 個快取（4.96 GiB），合計約 238.83 GiB；清理後約 81.72 GiB。
- 保留原圖、標註、兩套既有生成資料集、模型、辨識成果及裝置設定。未執行權重刪除。
- 新增快取保護：訓練 starting/running/stopping 時拒絕清理；不刪沒有原圖的 NPY，不沿符號連結／目錄 junction 掃描到管理目錄外。
- 修正月度批次模型清理原本涵蓋所有 completed run 的問題，現在只清理有月度標記的模型。
- 兩套生成資料集與目前三個模型的基本產物驗證通過。舊 `yolo26s_20260826_132655` 已標記 completed，但原有 `best.pt` 已不存在；本次沒有刪除該權重，也沒有虛構修復或移除歷史紀錄。
- Git 索引沒有已追蹤但應忽略的檔案；候選內容約 60 MiB，主體是內建路線。程式模板與回歸圖片仍為必要內容。候選內容與 HEAD 憑證掃描皆未發現洩漏。
- 三個根目錄測試暫存資料夾與 `runtime/push-audit/contents` 掃描副本的遞迴刪除遭執行政策拒絕，仍保留且均由 Git 忽略，不會進入提交。掃描副本約 60 MiB。
- 完整驗收發現並修正 YOLO 載入後灰階模板維度改變造成回顧按鈕失效，以及路線測試仍使用舊輸入 callback 的問題。最終 Python 425 項：404 通過、21 略過（可選依賴或本機素材等）；前端 29 項通過，正式版建置與 staged diff 檢查通過。未執行 Android 建置。

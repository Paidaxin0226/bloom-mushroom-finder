# 水晶菇水面誤判：負樣本微調

水面應以空 YOLO 標註表示「沒有目標」，不能標成 `Large_Water`；該類別指遊戲中的水菇。原本每張合成圖至少放一隻菇，背景像素雖然參與訓練，卻缺少整張都沒有菇的負樣本。原模型在合成驗證集的高分沒有涵蓋這次錯誤。

## 資料與分組

本次人工檢視 2026-09-12 座標失敗紀錄中的五張原始畫面，確認沒有待辨識的蘑菇。三張美國／印度畫面用於訓練；丹麥的兩張畫面只用於驗證。相同地點沒有跨越此次訓練及驗證分組。

- 訓練共 536 張：96 張負樣本（由三張原始畫面各重複 32 次以增加權重）、336 張新合成正樣本、104 張原常駐資料集的正樣本。
- 驗證共 166 張：兩張原始空背景、112 張合成正樣本、52 張原資料集的正樣本；共 164 隻已標註蘑菇。
- 保留完整 14 類，包括 `Event_2026_09`。舊資料的 13 類 ID 依名稱重映射，不能直接沿用原數字。
- 正樣本使用原透明素材及 256 像素標註框，部分水晶素材放在原先誤判水池的位置。
- 另外確認兩張實際遊戲畫面中已知的水菇／火菇是否仍能辨識。

重複的負樣本不是 96 個獨立場景；兩張丹麥畫面也不足以代表所有地形。原資料集取出的驗證圖只作保留既有能力的檢查，不能宣稱是全新的真實世界測試。

## 訓練與量測

從 `outputs/training/activity_2026-09_20260912_115412/weights/best.pt` 微調八輪；AdamW、初始學習率 0.0001、影像長邊 1024、batch 3。關閉色調、旋轉、縮放、翻轉、mosaic 等會改變目標外觀的增強，保留 8% 平移。

比較時直接使用模型輸出，不經 HSV 水面篩選；固定信心門檻 0.45、正樣本匹配 IoU 0.5。訓練前，五張原始水面全有水晶誤報，其中兩張留出畫面各誤報一次；164 個正樣本及兩個已知實機目標全部找到。

實驗輸出先放在 `outputs/experiments/crystal_water_correction_20260913`，驗證通過後才可加入正式模型清單。

## 結果與採用權重

共跑完八輪，採用第 1 輪的 `epoch0.pt`。最後一輪雖然也排除了水面，卻在兩張正樣本上新增同一隻菇的重複框；比較各輪後，第一輪即已滿足修正條件，完整重驗也沒有新增各類別的誤報或漏判。

| 固定門檻、未使用水面篩選 | 原模型 | 採用模型 |
| --- | ---: | ---: |
| 五張原始水面中的誤報數 | 5 | 0 |
| 其中兩張未參與訓練的水面誤報數 | 2 | 0 |
| 已標註正樣本辨識成功數 | 164 / 164 | 164 / 164 |
| 其中水晶菇 | 12 / 12 | 12 / 12 |
| 已知實機水菇／火菇 | 2 / 2 | 2 / 2 |
| 完整驗證集多於標註的框數 | 3 | 1 |

最後一列仍有一個原模型也會產生的 `Large_Poison` 候選，位於舊合成資料背景邊緣；本次沒有宣稱所有類別、所有背景都已零誤報。採用過程使用這組驗證圖選擇權重，因此這是訓練留出的驗證集，不是完全未碰過的最終測試集。

正式權重為 `outputs/training/crystal_water_correction_20260913/weights/best.pt`，預設辨識選擇切到這個路徑。原模型保留。此次數據證明改善來自模型權重；先前的 HSV 篩選仍保留作額外保護。

## 可重現紀錄

- 執行程式：`scratch/crystal_training/experiment.py`
- 資料來源、雜湊與每張圖片的用途：`data/experiments/crystal_water_20260913/manifest.json`
- 訓練前比較：`scratch/crystal_training/baseline.json`
- 訓練後比較：`scratch/crystal_training/candidate.json`
- 各輪診斷：`scratch/crystal_training/checkpoint_comparison.json`
- 最後一輪的比較：`scratch/crystal_training/epoch7.json`
- 採用與預設模型變更：`scratch/crystal_training/published.json`
- 訓練紀錄：`scratch/crystal_training/train.log`

```powershell
.venv/Scripts/python.exe scratch/crystal_training/experiment.py prepare
.venv/Scripts/python.exe scratch/crystal_training/experiment.py baseline
.venv/Scripts/python.exe scratch/crystal_training/experiment.py train
.venv/Scripts/python.exe scratch/crystal_training/experiment.py evaluate --model outputs/experiments/crystal_water_correction_20260913/weights/epoch0.pt --tag epoch0
```

`prepare` 與 `train` 遇到既有輸出會停止，避免覆寫實驗。重跑須使用新輸出路徑。完成後的評估仍可重跑。

## 產生器修正

`target_count_probabilities` 現在接受數量 `0`，會保留原背景、輸出空標註檔，並在 metadata 記錄 `negative_image_count`。現有預設比例沒有被整批改成負樣本；只有確認沒有真菇的背景才能使用零目標設定，否則會把漏標的真菇教成背景。

資料產生器、資料驗證及訓練工作程序共 26 項測試通過。

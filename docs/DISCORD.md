# Discord 即時命中播報

在「手機辨識」的「即時命中」卡設定。自動搜尋與座標序列共用本機播報設定。

## Discord 機器人

1. 在 Discord Developer Portal 建立機器人並邀請到自己的伺服器。
2. 在播報頻道允許機器人「檢視頻道、傳送訊息、嵌入連結、附加檔案」。不需要開啟 Message Content Intent。
3. 播報方式選「Discord 機器人 · Bot Token」，貼上 Bot Token，按「連線並取得頻道」。
4. 唯一可用頻道會自動選取；有多個頻道時，從伺服器／分類／頻道清單選擇。無需手動輸入頻道 ID。
5. 開啟播報，按「儲存播報設定」。也可按「傳送測試播報」檢查連線；測試本身不會儲存變更。

若只有一個可用頻道，貼上 Token 後直接儲存啟用或傳送測試，也會自動取得並選取頻道。機器人必須先加入伺服器；Token 無法替代 Discord 的邀請與頻道授權。多個目的地不會隨機選擇或向全部頻道廣播。

Token 以密碼欄位輸入，儲存在本機設定檔。重新輸入 Token 會清除先前的頻道選擇，避免沿用其他機器人的頻道。

## Webhook

原有 Webhook 設定可繼續使用。選擇「Discord Webhook」、貼入 Discord 官方 HTTPS Webhook 網址、開啟並儲存即可。切換方式會保留另一種方式的設定，但每次只透過所選方式播報。

## 命中訊息

- 目標名稱置於標題；常見蘑菇類別會顯示中文名稱與對應顏色。
- 人數與辨識信心分欄顯示；沒有辨識結果時標記為未辨識，不當成 0 人。
- 座標清楚區分精確座標、估算座標與掃描中心，並提供 Google 地圖連結。精確擷取失敗時不以掃描中心冒充目標位置。
- 每個目標的實際裁切圖上傳為附件，直接顯示在對應的 embed 內。無法使用的圖片會顯示提示並保留文字通知。
- 多筆命中分批播報，每批最多 5 筆，避免超過 embed 長度與附件限制。
- 網路或權限失敗會回報到工作訊息，不中斷辨識；Discord 短暫限流會在背景有限次重試。

串接依循 Discord 官方的 [訊息 API](https://docs.discord.com/developers/resources/message)、[附件上傳規格](https://docs.discord.com/developers/reference#uploading-files) 與 [頻道權限](https://docs.discord.com/developers/topics/permissions)。

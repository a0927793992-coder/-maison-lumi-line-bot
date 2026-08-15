# Maison Lumi LINE +1 自動登記系統

第一版流程：

1. 群組內傳商品照片。
2. 客人用 LINE「回覆」該照片並輸入 `+1`、`+2`…
3. 第一筆喊單會自動建立商品編號 A001、A002…
4. 後續同張照片的喊單累加到同一商品。
5. 回覆照片輸入「取消」或「刪單」可取消自己的該商品喊單。

## 安全
請勿把 LINE Channel Secret 或 Channel Access Token 寫進 GitHub。部署時使用環境變數：

- `LINE_CHANNEL_SECRET`
- `LINE_CHANNEL_ACCESS_TOKEN`

## Webhook
部署完成後，把 LINE Webhook URL 設為：

`https://你的服務網址/webhook`

> 目前使用 SQLite，適合第一版測試。正式大量使用前建議改成 PostgreSQL/Supabase，以避免部署平台重啟造成資料遺失。

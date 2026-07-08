# 選用防護：Cloudflare Worker 來源網址檢查（進階，非必要）

## 這是什麼、為什麼需要它

GitHub Pages 只會把檔案原封不動送出去，不會檢查「誰在要」「要幾次」。
如果有人寫程式直接抓你的 `data/stocks.json`，GitHub Pages 沒辦法擋，
只能透過在 GitHub Pages 前面加一層有邏輯判斷能力的服務（例如 Cloudflare
Worker）來做這件事，這份檔案就是做這個用的。

**如果你目前用的是預設網址（`xxx.github.io/MOUP-V29/`），這份檔案先不用管**，
等你之後幫網站接上自訂網域，再回來看下面步驟。

## 使用前提

1. 已經有自己的網域（例如在 GoDaddy、Namecheap 買的）
2. 願意把該網域的 DNS 交給 Cloudflare 管理（免費方案就夠用）

## 設定步驟

1. 到 [Cloudflare](https://dash.cloudflare.com) 註冊帳號，把你的網域加進去，
   照畫面指示把網域註冊商那邊的 Nameserver 改成 Cloudflare 提供的兩組
2. 在 Cloudflare 後台左側選單找 **Workers 和 Pages** → **建立** → **建立 Worker**
3. 把 `origin-guard.js` 的內容整個貼進去，取代預設範例程式碼
4. 程式碼裡把這一行：
   ```js
   const ALLOWED_ORIGIN = "https://your-domain.example.com";
   ```
   改成你真正的網址（結尾不要加斜線）
5. 存檔部署後，到 Worker 設定裡的 **觸發條件 (Triggers)** → **新增路由 (Add Route)**，
   路由填：
   ```
   your-domain.example.com/data/*
   ```
   （只攔截 `/data/` 底下的路徑，首頁跟其他檔案完全不受影響）
6. 存檔即可生效，通常幾分鐘內全球會生效

## 這個防護能擋住什麼、擋不住什麼

- ✅ 能擋：一般人隨手寫的爬蟲腳本（fetch/requests 直接打 API，會帶著自己的
  Origin 或完全沒帶 Referer 但頻率異常）
- ✅ 能擋：短時間內大量重複請求（設定為 1 分鐘 30 次）
- ❌ 擋不住：真的有心偽造 Referer/Origin 標頭來假裝是你的網站的人
  （沒有任何前端防護能百分之百擋住這種情況，這是網頁架構的天生限制）

這道防護的目的是拉高門檻、擋掉大多數隨手濫用的情況，而不是做到絕對無法被繞過。

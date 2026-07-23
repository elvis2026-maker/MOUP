# 個股分析後端：Cloudflare Worker（Gemini API，3組金鑰備援）

## ⚠ V46 更新，請重新部署 Worker

`cloudflare-worker-optional/ai-analyze-worker.js` 這份程式碼有更新，**請把新版整份
重新貼上 Cloudflare Worker 並部署**，否則以下兩個修正不會生效：

1. **個股分析改為全市場**：不再限定今日電子股掃描清單，清單外的個股也會由
   Gemini 根據其代號／名稱做全市場分析（不再只回「不在今日掃描範圍內」）。
2. **新增 `GET /market-pulse` 路由**：修好「市場脈動」全部顯示 `--` 的問題——
   原因是瀏覽器直接呼叫 Yahoo Finance 的 `v7/finance/quote` 已經失效（Yahoo 改版
   後需要 crumb 驗證、也不再開放跨網域瀏覽器請求），現在改由 Worker 在伺服器端
   代抓（改打不需 crumb 的 `v8/finance/chart` 端點）再回傳給前端。

不需要新增任何機密變數，沿用原本的 3 組 `MO*_GEMINI_API_KEY` 即可。

---

## 現況

`index.html` 裡的 `AI_ENDPOINT` 已經填好：

```
https://elvis-moup-api.elvis-liu2027.workers.dev
```

對應的 Worker 原始碼在 `cloudflare-worker-optional/ai-analyze-worker.js`，
**你只需要把這份程式碼部署到 Cloudflare、並設定好 3 組 API Key，就完成串接**，
不用再改 `index.html`。

---

## 這支 Worker 做的事

1. 接收前端傳來的股票資料（代號、名稱、評分、MA20、法人籌碼、乖離率⋯）
2. 組成 prompt，呼叫 Google Gemini API
3. **3 組 Key 輪替**：`MO_GEMINI_API_KEY` 失敗（額度用完／被限流／出錯）就自動換
   `MO2_GEMINI_API_KEY`，再失敗換 `MO3_GEMINI_API_KEY`
4. **2 個模型備援**：3 組 Key 用主模型 `gemini-3.5-flash` 都失敗的話，整組
   Key 再用 `gemini-3.1-flash-lite` 試一輪（共最多 6 次嘗試）
5. 全部都失敗才回傳錯誤——這時前端會自動退回原本內建的規則式分析，
   使用者畫面不會出現錯誤或空白

---

## 部署步驟

### 1. 建立 Worker

1. 登入 [Cloudflare Dashboard](https://dash.cloudflare.com)
2. **Workers 和 Pages** → **建立應用程式** → **建立 Worker**
3. 名稱填 `elvis-moup-api`（要跟網址 `elvis-moup-api.elvis-liu2027.workers.dev`
   一致，`elvis-liu2027` 是你 Cloudflare 帳號的 workers.dev 子網域，通常在帳號
   設定裡就能看到／設定）
4. 建立後點 **編輯程式碼**，把 `cloudflare-worker-optional/ai-analyze-worker.js`
   整份內容貼上去，取代預設範例
5. **部署 (Deploy)**

### 2. 申請 3 組 Gemini API Key

到 [Google AI Studio](https://aistudio.google.com/apikey) 申請 API Key。
若要用 3 組真正獨立的備援額度，建議用 3 個不同的 Google 帳號（或同帳號下的
3 個不同專案）各申請一組，這樣其中一組額度用完時，另外兩組才幫得上忙。

### 3. 把 3 組 Key 存進 Worker 的機密變數

1. Worker 頁面 → **設定 (Settings)** → **變數與機密 (Variables and Secrets)**
2. 新增 3 個「機密 (Secret)」，名稱要完全一致：

   | 變數名稱 | 值 |
   |---|---|
   | `MO_GEMINI_API_KEY` | 第一組 Key |
   | `MO2_GEMINI_API_KEY` | 第二組 Key |
   | `MO3_GEMINI_API_KEY` | 第三組 Key |

3. 存檔（Worker 會自動重新部署套用新變數）

### 4. 測試

部署完成後，直接到網站的「**個股分析**」分頁，輸入今天有掃到的股票代號
（例如 `2377`），按「開始分析」。如果 Worker 設定正確，會看到帶有機率、
理由的分析卡片；如果 Worker 還沒設定好或暫時失敗，會自動退回規則式分析
（一樣能用，只是理由是用既有資料組出來的，不是 Gemini 生成的）。

---

## 安全性補充

範例程式碼裡 `Access-Control-Allow-Origin` 設成 `"*"`，方便先測試。正式上線
穩定後，建議改成只允許你自己的網站呼叫：

```javascript
"Access-Control-Allow-Origin": "https://elvis2026-maker.github.io",
```

這樣其他網站就沒辦法盜用你的 Worker 去消耗你的 Gemini 額度。

---

## 費用與額度

- Cloudflare Worker 免費方案：每天 10 萬次請求，一般用量用不完
- Gemini API：依模型與用量計費／有免費額度，實際額度與價格請以
  [Google AI Studio](https://aistudio.google.com/) 帳號內顯示為準
- 目前沒有做快取，同一支股票被重複查詢會每次都呼叫一次 Gemini；如果之後
  流量變大，可以考慮在 Worker 裡加上「同股票 10 分鐘內查過就不重打 API」
  的簡單快取邏輯

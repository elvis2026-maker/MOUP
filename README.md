# MOUP — Taiwan Stock Momentum Intelligence

台股強勢股篩選網站（不限權證，股票／零股操作也適用）。GitHub Actions 排程抓資料 → 寫入 `data/*.json` →
GitHub Pages 純靜態網頁讀取顯示。

## 架構說明

- `scripts/fetch_twse.py`：主要選股邏輯，排程呼叫 FinMind API，輸出 `data/stocks.json`
- `scripts/fetch_live.py`：把 `stocks.json` 整理成「隔夜精選速覽」，輸出 `data/live.json`
- `.github/workflows/fetch-data.yml`、`fetch-live.yml`：排程觸發上面兩支腳本
- `index.html`：純前端頁面，用相對路徑 `fetch('data/stocks.json')` 讀資料，
  跟 `data/` 資料夾放在同一個網站下即可運作

FinMind 的 `FINMIND_TOKEN_1~4` 存放在 GitHub Repo 的 **Settings → Secrets and
variables → Actions** 裡，只有排程執行時的 GitHub Actions 環境讀得到，
**不會出現在任何前端程式碼或瀏覽器看得到的地方**，這部分從一開始就是安全的。

## 版本紀錄

- **V38（目前版本）**：LOGO 改為置頂置中、融合背景樣式。① 去除浮動固定
  定位與白底卡片邊框，改成頁面最上方（HERO 區塊之前）置中顯示的一般版面
  元素，手機版不會再遮到下方標題；② LOGO 圖檔改用去背透明 PNG
  （`assets/logo.png`，用 flood-fill 去除原圖白色背景），直接融入頁面
  底色；③ 尺寸調回適中大小（桌機 92px，手機 72px）。
- V37：LOGO 徽章改版。① 由圓形裁切改為圓角方形，用
  `object-fit:contain` 完整顯示整張 LOGO（含盾牌圖案與「艾維斯資訊 IT 顧問」
  文字），不再被裁掉；② 位置從右上角移到左上角；③ 尺寸放大約 2.5 倍
  （寬度 46px → 118px，手機版 88px），文字清楚可讀。
- V36：隔夜精選速覽頁移除說明小字「這個工具的定位是盤後選股分析，
  不提供盤中即時資料（FinMind 免費版盤中無資料，付費版才有，目前未串接）」。
- V35：右上角新增品牌 LOGO。固定在頁面右上角的圓形徽章
  （`assets/logo.jpg`，艾維斯資訊 ELVIS 盾牌箭頭圖標），點擊會開新分頁
  前往 https://elvis2026-maker.github.io/MIS/ ；手機版（<400px）自動縮小尺寸。
- V34：底部銘牌改版。① 銘牌樣式從淺色鐵牌改成深色膠囊造型，
  加上金屬光澤掃光動畫（CSS ::before + keyframes，約每 4.5 秒掃過一次）；
  ② 移除銘牌旁的藍色「V33」版次徽章與 footer 中重複的「MOUP V33」文字，
  避免版號重複顯示、視覺上突兀；③ 銘牌加上超連結，點擊會開新分頁前往
  https://elvis2026-maker.github.io/MIS/ 。
- V33：版權銘牌位置調整。① 頁首銘牌（✦ 程式設計 Elvis Liu｜版權所有）
  移到頁面最下方、footer 之後；② 移除 footer 文字中與銘牌重複的
  「程式設計 Elvis Liu｜版權所有」字樣，只保留「禁止未經授權轉載或作為資料來源使用」。
- V32：品牌通用化 + 功能改名，讓不玩權證的人也不會覺得這工具跟自己無關。
  ① 標題與品牌文字從「權證標的精選」全面改成「強勢股掃描｜隔日續航力分析」，
  不再限定只有操作認購權證的人才看得懂／用得到；② 卡片提示語（`genHint`）
  改成先給通用的進場建議，權證槓桿倍數等資訊變成「如果你想用權證操作」的附加選項，
  不再預設每個人都是為了買權證才看這個網站；③「盤中參考卡」正式更名為
  「隔夜精選速覽」——原本的名字會誤導人以為有即時報價，但內容其實一直都是
  昨日盤後數據，改名更準確反映這個功能的真實定位（開盤前複習用，不是盤中報價）；
  ④ README 架構說明同步更新用詞。
- V31：加上禁用右鍵選單與常見開發者工具快捷鍵（F12、
  Ctrl+Shift+I/J/C、Ctrl+U）的基本嚇阻措施。**老實提醒**：這只能擋隨手
  複製的人，懂技術的人能用瀏覽器工具列手動開啟開發者工具、瀏覽器擴充功能、
  或直接用 `curl`／`wget` 之類的指令列工具繞過，不是真正的存取控制，也
  沒辦法讓網頁「不能被查看原始碼」——瀏覽器本來就得先收到完整的
  HTML/CSS/JS 才能顯示網頁，這是技術上的天生限制，連大公司的網站也做
  不到真正阻止。這個功能單純是為了拉高隨手複製的門檻。
- V30：加上資料與網站的保護措施，防止整站或原始資料被複製濫用：
  ① 三支輸出 JSON（`stocks.json` 空/非空兩種情況、`live.json`）都新增
  `_meta` 欄位，內含版權聲明與使用限制文字，即使檔案被整包複製走，
  聲明文字也會跟著走，作為主張權利的依據；前端只讀取既有的 `stocks` /
  `ref_cards` 等特定欄位，新增的 `_meta` 不會影響任何既有功能。
  ② 新增 `robots.txt`，擋掉遵守規範的搜尋引擎爬蟲索引 `data/` 底下的原始
  資料檔案。③ 新增 `cloudflare-worker-optional/` 資料夾，內含一支選用的
  Cloudflare Worker，可在你之後接上自訂網域時，對 `data/*.json` 加上「來源
  網址檢查」與「頻率限制」——這是 GitHub Pages 本身（純靜態主機、沒有伺服器
  端邏輯）做不到的事，詳細設定步驟見該資料夾內的 README。④ Footer 版次同步
  更新，並補強版權聲明文字。

## 重要提醒：關於「防止整站被下載複製」的技術限制

網頁本身（HTML/CSS/JS）依技術限制無法做到完全禁止查看或下載——瀏覽器本來
就得先收到這些程式碼才能顯示網頁，這點連大公司的網站也做不到。真正該注意的
是**這個 GitHub Repo 目前是公開（Public）還是私人（Private）**：

- 如果 Repo 是 **Public**：任何人都能直接在 GitHub 上 `git clone` 整個專案，
  包含所有程式碼、Workflow 設定、`data/*.json` 資料，前端層級的任何防護在
  這種情況下都沒有意義，因為原始碼本來就是公開的。
- 如果想要真正降低「整包被複製」的風險，**最有效的做法是把 Repo 設為
  Private**（Settings → General → Danger Zone → Change visibility）。
  需注意：GitHub 的免費方案下，Private Repo 也可以用 GitHub Pages
  發布網站（Pages 本身仍是公開網址，訪客照常看得到網站，但看不到你的
  Repo 原始碼、Git 歷史、Workflow 內容），這樣兼顧「網站照常公開給使用者
  看」跟「原始碼不公開」兩件事。

建議你確認一下這個 Repo 目前的公開狀態，如果是 Public 而你希望原始碼不被
直接複製，改成 Private 會是效果最直接的一步。

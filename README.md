# 權證標的精選系統

每日自動篩選台股續漲機率高的股票，供權證操作參考。

## 目錄結構

```
├── index.html                        # 前端主頁
├── data/
│   └── stocks.json                   # 每日自動產生的選股資料
├── scripts/
│   └── fetch_twse.py                 # Python 資料抓取腳本
└── .github/
    └── workflows/
        └── fetch-data.yml            # GitHub Actions 排程設定
```

## 部署步驟

### 1. 建立 GitHub Repository
```
New repo → 名稱隨意（建議 warrant-scanner）→ Public
```

### 2. 上傳檔案
把以下檔案推上去（保持目錄結構）：
- `index.html`
- `data/stocks.json`（初始 mock 資料）
- `scripts/fetch_twse.py`
- `.github/workflows/fetch-data.yml`

### 3. 開啟 GitHub Pages
```
Settings → Pages → Source: Deploy from branch → main → / (root)
```

### 4. 確認 Actions 權限
```
Settings → Actions → General
→ Workflow permissions → Read and write permissions ✅
```

### 5. 測試手動觸發
```
Actions → 每日盤後抓資料 → Run workflow
```

## 排程說明

| 時間（台灣）| 動作 |
|---|---|
| 15:00 | 第一次抓取（TWSE 資料通常 14:45 後更新）|
| 15:30 | 備份抓取（確保資料完整）|
| 每日週一~週五 | 自動執行 |

## 評分系統說明

| 項目 | 滿分 | 說明 |
|---|---|---|
| 量價結構 | 40 | 漲幅、收盤位置、量能倍數 |
| 技術分析 | 30 | 均線多頭排列、突破高點 |
| 籌碼面 | 30 | 三大法人買超、融資狀況 |

### 續漲機率對應分數

| 分數 | 機率等級 |
|---|---|
| 85~100 | 高（75~85%）|
| 70~84  | 中高（60~75%）|
| 55~69  | 中（45~60%）|
| 0~54   | 偏低 |

## 注意事項

- TWSE 公開 API 有時會因伺服器維護而短暫無法存取
- 遇到假日或休市日，腳本會自動嘗試最近的交易日
- 所有資料及評分僅供參考，不構成投資建議
